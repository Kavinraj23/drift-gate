"""Builds the synthetic population: ~30 days of backdated executions across the 12
pipelines in pipelines.py, with correlated bursts, flaky retries, and a change
timeline (PRD.md SS11).

Deterministic given a seed. anchor_end defaults to a fixed constant (not "now") so a
checked-in seeded dataset doesn't drift every time it's regenerated.
"""
from __future__ import annotations

import itertools
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from drift_gate.domain import (
    Classification,
    Execution,
    ExecutionStatus,
    Gate,
    Node,
    NodeStatus,
    Remediation,
    Tier,
)
from drift_gate.errors import BY_FAULT_ID as ERROR_BY_FAULT_ID
from drift_gate.generator import faults as faults_mod
from drift_gate.generator.changes import ChangeEvent, build_change_timeline
from drift_gate.generator.ground_truth import GroundTruth
from drift_gate.generator.pipelines import PIPELINES, PipelineDef

DEFAULT_ANCHOR_END = datetime(2026, 9, 1, 0, 0, 0)
WINDOW_DAYS = 30
SUCCESS_RATE = 0.80
GOVERNANCE_RATE = 0.08
FAILURE_WEIGHTS = {"user": 60, "platform": 25, "transient": 15}
FLAKE_RETRY_PASS_RATE = 0.7
N_BURSTS_RANGE = (3, 4)
BURST_SPAN_MINUTES = 20


@dataclass
class Burst:
    id: str
    started_at: datetime
    kind: str  # template_bump | credential_rotation
    fault_id: str
    template_ref: object | None
    connector_ref: str | None
    pipeline_ids: list[str]


@dataclass
class Population:
    pipelines: tuple[PipelineDef, ...]
    executions: list[Execution]
    events: list[ChangeEvent]
    ground_truth: list[GroundTruth]
    fixture_repo_path: Path


def _step_durations(rng: random.Random, n: int) -> list[timedelta]:
    return [timedelta(seconds=rng.randint(15, 90)) for _ in range(n)]


def _build_nodes(
    rng: random.Random, chain: tuple[str, ...], fail_step_type: str | None, started_at: datetime
) -> tuple[list[Node], datetime]:
    """fail_step_type=None means the execution succeeds end to end."""
    if fail_step_type == "parse":
        ended = started_at + timedelta(seconds=rng.randint(2, 10))
        return [
            Node("n0", "parse", "parse", NodeStatus.FAILED, None, started_at, ended)
        ], ended

    idx = chain.index(fail_step_type) if fail_step_type in chain else (
        len(chain) - 1 if fail_step_type is not None else None
    )
    durations = _step_durations(rng, len(chain))
    nodes: list[Node] = []
    t = started_at
    parent_id = None
    for i, step in enumerate(chain):
        node_start = t
        node_end = t + durations[i]
        status = NodeStatus.FAILED if idx is not None and i == idx else NodeStatus.SUCCESS
        nodes.append(Node(f"n{i}", step, step, status, parent_id, node_start, node_end))
        parent_id = f"n{i}"
        t = node_end
        if status == NodeStatus.FAILED:
            # remaining steps in the chain never ran; stop here
            break
    return nodes, t


def _remediation_from_profile(
    profile: faults_mod.FaultProfile, execution_id: str
) -> Remediation | None:
    if profile.tier is None:
        return None
    return Remediation(
        tier=profile.tier,
        action=profile.action,
        rationale=profile.rationale,
        reversible=profile.reversible,
        gate=profile.gate,
        dry_run={},
        context={"execution_id": execution_id},
    )


def _pick_fault_for_pipeline(
    rng: random.Random, pipeline: PipelineDef, pool: tuple[str, ...]
) -> str:
    eligible = [
        fid for fid in pool
        if ERROR_BY_FAULT_ID[fid].step_type in pipeline.step_chain
        or ERROR_BY_FAULT_ID[fid].step_type == "parse"
    ]
    return rng.choice(eligible or list(pool))


def _make_execution(
    exec_id: str, pipeline: PipelineDef, started_at: datetime, rng: random.Random,
    fail_fault_id: str | None,
) -> Execution:
    fail_step_type = ERROR_BY_FAULT_ID[fail_fault_id].step_type if fail_fault_id else None
    nodes, ended_at = _build_nodes(rng, pipeline.step_chain, fail_step_type, started_at)
    status = ExecutionStatus.FAILED if fail_fault_id else ExecutionStatus.SUCCESS
    return Execution(
        id=exec_id,
        pipeline_id=pipeline.id,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        connector_ref=pipeline.connector_ref,
        template_ref=pipeline.template_ref,
        runner_pool=pipeline.runner_pool,
        infra_ref=pipeline.infra_ref,
        trigger=rng.choice(["schedule", "manual", "webhook"]),
        nodes=tuple(nodes),
    )


def _classify_failure(rng: random.Random) -> str:
    if rng.random() < GOVERNANCE_RATE:
        return "governance"
    return rng.choices(
        list(FAILURE_WEIGHTS.keys()), weights=list(FAILURE_WEIGHTS.values())
    )[0]


_POOLS = {
    "user": faults_mod.USER_FAULTS,
    "platform": faults_mod.PLATFORM_FAULTS,
    "transient": faults_mod.TRANSIENT_FAULTS,
    "governance": faults_mod.GOVERNANCE_FAULTS,
}


def _plan_bursts(rng: random.Random, window_start: datetime, window_end: datetime) -> list[Burst]:
    by_template: dict[object, list[str]] = {}
    by_connector: dict[str, list[str]] = {}
    for p in PIPELINES:
        by_template.setdefault(p.template_ref, []).append(p.id)
        by_connector.setdefault(p.connector_ref, []).append(p.id)

    template_groups = [(k, v) for k, v in by_template.items() if len(v) >= 2]
    connector_groups = [(k, v) for k, v in by_connector.items() if len(v) >= 2]

    n_bursts = rng.randint(*N_BURSTS_RANGE)
    bursts: list[Burst] = []
    span_seconds = int((window_end - window_start).total_seconds())
    for i in range(n_bursts):
        started_at = window_start + timedelta(seconds=rng.randint(0, span_seconds))
        if i % 2 == 0 and template_groups:
            ref, pids = rng.choice(template_groups)
            bursts.append(Burst(
                id=f"burst-{i}", started_at=started_at, kind="template_bump",
                fault_id="lockfile_mismatch", template_ref=ref, connector_ref=None,
                pipeline_ids=rng.sample(pids, k=min(len(pids), rng.randint(2, len(pids)))),
            ))
        elif connector_groups:
            ref, pids = rng.choice(connector_groups)
            bursts.append(Burst(
                id=f"burst-{i}", started_at=started_at, kind="credential_rotation",
                fault_id="expired_credential", template_ref=None, connector_ref=ref,
                pipeline_ids=rng.sample(pids, k=min(len(pids), rng.randint(2, len(pids)))),
            ))
    return bursts


def build_population(seed: int = 42, anchor_end: datetime = DEFAULT_ANCHOR_END,
                      out_dir: Path = Path("data/synthetic")) -> Population:
    rng = random.Random(seed)
    window_end = anchor_end
    window_start = window_end - timedelta(days=WINDOW_DAYS)

    executions: list[Execution] = []
    ground_truth: list[GroundTruth] = []
    exec_counter = itertools.count()

    def next_id(pipeline_id: str, at: datetime) -> str:
        return f"exec_{at.strftime('%Y%m%d_%H%M%S')}_{pipeline_id}_{next(exec_counter):04d}"

    # --- baseline population ---
    day = window_start
    while day < window_end:
        for pipeline in PIPELINES:
            n = rng.randint(1, 4)
            for _ in range(n):
                offset = timedelta(seconds=rng.randint(0, 24 * 3600 - 1))
                started_at = day + offset
                if started_at >= window_end:
                    continue
                if rng.random() < SUCCESS_RATE:
                    exec_id = next_id(pipeline.id, started_at)
                    executions.append(_make_execution(exec_id, pipeline, started_at, rng, None))
                    continue

                failure_type = _classify_failure(rng)
                fault_id = _pick_fault_for_pipeline(rng, pipeline, _POOLS[failure_type])
                exec_id = next_id(pipeline.id, started_at)
                executions.append(_make_execution(exec_id, pipeline, started_at, rng, fault_id))
                profile = faults_mod.BY_FAULT_ID[fault_id]

                is_flake = False
                remediation = _remediation_from_profile(profile, exec_id)
                if pipeline.flaky and rng.random() < FLAKE_RETRY_PASS_RATE:
                    is_flake = True
                    remediation = Remediation(
                        tier=Tier.TIER_0, action="retry_execution",
                        rationale="fingerprint has a recent fail-then-pass history",
                        reversible=True, gate=Gate.AUTO, dry_run={},
                        context={"execution_id": exec_id},
                    )
                    retry_at = started_at + timedelta(minutes=rng.randint(1, 3))
                    retry_id = next_id(pipeline.id, retry_at)
                    executions.append(_make_execution(retry_id, pipeline, retry_at, rng, None))

                ground_truth.append(GroundTruth(
                    execution_id=exec_id,
                    injected_fault_id=fault_id,
                    correct_classification=profile.classification,
                    correct_remediation=remediation,
                    is_duplicate_of=None,
                    is_flake=is_flake,
                ))
        day += timedelta(days=1)

    # --- correlated bursts ---
    bursts = _plan_bursts(rng, window_start, window_end)
    for burst in bursts:
        primary_id: str | None = None
        span_seconds = BURST_SPAN_MINUTES * 60
        for pid in burst.pipeline_ids:
            pipeline = next(p for p in PIPELINES if p.id == pid)
            at = burst.started_at + timedelta(seconds=rng.randint(0, span_seconds))
            exec_id = next_id(pipeline.id, at)
            executions.append(_make_execution(exec_id, pipeline, at, rng, burst.fault_id))
            profile = faults_mod.BY_FAULT_ID[burst.fault_id]
            if primary_id is None:
                primary_id = exec_id
            ground_truth.append(GroundTruth(
                execution_id=exec_id,
                injected_fault_id=burst.fault_id,
                # fleet correlation flips ownership user->platform (PRD.md SS8 step 3)
                correct_classification=Classification.PLATFORM,
                correct_remediation=_remediation_from_profile(profile, exec_id),
                is_duplicate_of=None if exec_id == primary_id else primary_id,
                is_flake=False,
            ))

    events = build_change_timeline(rng, window_start, window_end, bursts, out_dir)

    executions.sort(key=lambda e: e.started_at)
    ground_truth.sort(key=lambda g: g.execution_id)

    return Population(
        pipelines=PIPELINES,
        executions=executions,
        events=events,
        ground_truth=ground_truth,
        fixture_repo_path=out_dir / "fixture_repo",
    )
