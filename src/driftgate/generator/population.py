"""Seeded synthetic population: executions, bursts, flaky pipelines, repos, change timeline, labels.

`generate(seed)` is pure: same seed and anchor give byte-identical output. Randomness comes only from
`random.Random` instances derived from the seed; there is no clock and no I/O here.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from driftgate.domain import Execution, Node

from .catalog import load_catalog
from .faults import FAULTS, FaultSpec
from .logs import common_params, render_failed_step
from .pipelines import PIPELINES, STAGES, TF_BAD_VERSION, TF_GOOD_VERSION, PipelineDef
from .repos import RepoFault, base_files, inject

SCHEMA_VERSION = 1
DEFAULT_SEED = 20260908
ANCHOR = datetime(2026, 9, 8, tzinfo=UTC)
DAYS = 30
FAILURE_RATE = 0.20
CLASS_SHARE = {"user": 0.60, "platform": 0.25, "transient": 0.15}
FLAKY_FAIL_PROB = 0.10
HELD_OUT_COUNT = 9

# Scenarios per fault family (bursts are handled separately: one representative each).
SCENARIO_QUOTA = {
    "user_lockfile_mismatch": 3,
    "user_provider_pin": 2,
    "user_undefined_variable": 2,
    "user_pip_missing_dist": 2,
    "user_script_failure": 2,
    "transient_throttling": 3,
    "transient_registry_rate_limit": 2,
    "transient_image_pull": 3,
    "transient_flaky_canary": 3,
    "platform_state_lock": 2,
    "platform_oom_killed": 1,
    "platform_expired_token": 1,
    "platform_throttle_masks_lock": 2,
    "governance_approval_rejected": 2,
}


@dataclass(frozen=True)
class BurstSpec:
    burst_id: str
    fault_id: str
    dimension: str  # which shared ref correlates the burst
    value: str
    cause_kind: str
    cause_ref: str
    cause_message: str
    day: int


BURSTS = (
    BurstSpec(
        "burst-template",
        "platform_template_bump",
        "template",
        "terraform-plan",
        "template_release",
        "terraform-plan",
        f"Release {TF_BAD_VERSION}",
        5,
    ),
    BurstSpec(
        "burst-connector-token",
        "platform_expired_token",
        "connector",
        "aws-prod",
        "connector_change",
        "aws-prod",
        "Rotate credentials",
        11,
    ),
    BurstSpec(
        "burst-pool-memory",
        "platform_oom_killed",
        "runner_pool",
        "pool-small",
        "runner_pool_change",
        "pool-small",
        "Lower per-job memory limit",
        17,
    ),
    BurstSpec(
        "burst-connector-throttle",
        "transient_throttling",
        "connector",
        "aws-shared",
        "config_change",
        "aws-shared",
        "Raise CI job concurrency limit",
        23,
    ),
)

DOC_MESSAGES = (
    "docs: update README",
    "chore: bump dev dependency",
    "ci: adjust cache key",
    "refactor: rename internal helper",
    "docs: fix typo in comments",
)
FAULT_COMMIT_MESSAGES = {
    "user_lockfile_mismatch": "feat: add dayjs for date formatting",
    "user_provider_pin": "chore: bump aws provider constraint",
    "user_undefined_variable": "feat: add cost center tag",
    "user_pip_missing_dist": "chore: tidy requirements",
    "platform_template_bump": f"chore: bump terraform-plan template to {TF_BAD_VERSION}",
}


@dataclass(eq=False)
class Slot:
    pipeline: PipelineDef
    start: datetime
    status: str = "success"  # success | failed | approval_rejected
    fault_id: str | None = None
    burst_id: str | None = None
    retry_of: Slot | None = None
    template_version: str | None = None
    exec_id: str = ""


@dataclass
class Change:
    at: datetime
    kind: str
    ref: str
    actor: str
    message: str
    files: list[str] = field(default_factory=list)
    sha: str = ""
    change_id: str = ""


@dataclass
class Dataset:
    files: dict[str, str]  # relative path -> text; everything the writer emits
    scenarios: list[dict[str, Any]]
    stats: dict[str, Any]


def _rng(seed: int, label: str) -> random.Random:
    return random.Random(f"{seed}:{label}")


def _iso(dt: datetime) -> str:
    return f"{dt:%Y-%m-%dT%H:%M:%SZ}"


def _sha(*parts: object) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()  # noqa: S324 - id, not security


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False) + "\n"


def _eligible(eco: str, klass: str) -> list[FaultSpec]:
    return [f for f in FAULTS.values() if f.classification == klass and f.weight > 0 and eco in f.steps]


def _pick_fault(rng: random.Random, eco: str, klass: str) -> FaultSpec:
    pool = _eligible(eco, klass)
    return rng.choices(pool, weights=[f.weight for f in pool])[0]


def _schedule(seed: int, anchor: datetime, days: int) -> list[Slot]:
    slots: list[Slot] = []
    for p in PIPELINES:
        r = _rng(seed, f"sched:{p.name}")
        for day in range(days):
            secs = sorted(r.uniform(0, 86400 - 1) for _ in range(p.runs_per_day))
            slots += [Slot(p, anchor + timedelta(days=day, seconds=int(s))) for s in secs]
    return slots


def _members(b: BurstSpec) -> list[PipelineDef]:
    key = {"template": "template", "connector": "connector", "runner_pool": "runner_pool"}[b.dimension]
    return [p for p in PIPELINES if getattr(p, key) == b.value]


def generate(seed: int = DEFAULT_SEED, anchor: datetime = ANCHOR, days: int = DAYS) -> Dataset:
    catalog = load_catalog()
    slots = _schedule(seed, anchor, days)

    # ---- bursts: correlated failures inside a 20-minute window sharing a ref ----
    burst_windows: dict[str, datetime] = {}
    burst_slots: dict[str, list[Slot]] = {}
    changes: list[Change] = []
    cause_keys: dict[str, Change] = {}
    br = _rng(seed, "bursts")
    for b in BURSTS:
        ws = anchor + timedelta(days=b.day + br.randint(-1, 1), hours=br.randint(9, 16), minutes=br.randint(0, 39))
        burst_windows[b.burst_id] = ws
        members = _members(b)
        if b.dimension == "template":
            bump_at, rollback_at = ws - timedelta(minutes=5), ws + timedelta(minutes=45)
            slots = [s for s in slots if not (s.pipeline in members and bump_at <= s.start <= rollback_at)]
            changes.append(
                Change(rollback_at, "template_release", b.cause_ref, "platform-bot", f"Revert to {TF_GOOD_VERSION}")
            )
            cause_at = bump_at
        else:
            cause_at = ws - timedelta(minutes=br.randint(6, 14))
        cause = Change(cause_at, b.cause_kind, b.cause_ref, "platform-bot", b.cause_message)
        changes.append(cause)
        cause_keys[b.burst_id] = cause
        # A decoy infrastructure change shortly before each burst, with no causal link.
        changes.append(
            Change(
                ws - timedelta(minutes=br.randint(15, 30)),
                "runner_pool_change",
                "pool-large",
                "platform-bot",
                "Scale up pool capacity",
            )
        )
        burst_slots[b.burst_id] = []
        for p in members:
            s = Slot(
                p,
                ws + timedelta(seconds=int(br.uniform(0, 19 * 60))),
                status="failed",
                fault_id=b.fault_id,
                burst_id=b.burst_id,
                template_version=TF_BAD_VERSION if b.dimension == "template" else None,
            )
            burst_slots[b.burst_id].append(s)
            slots.append(s)

    # ---- two genuinely flaky pipelines: fail, then pass on retry ----
    fr = _rng(seed, "flaky")
    flaky_retries: list[Slot] = []
    flaky_fails: list[Slot] = []
    for s in list(slots):
        if s.pipeline.flaky and s.burst_id is None and fr.random() < FLAKY_FAIL_PROB:
            s.status, s.fault_id = "failed", "transient_flaky_canary"
            flaky_fails.append(s)
            flaky_retries.append(Slot(s.pipeline, s.start + timedelta(seconds=fr.randint(180, 480)), retry_of=s))
    slots += flaky_retries

    # ---- governance rejections and wrong-first-fix cases (all on terraform pipelines) ----
    sr = _rng(seed, "special")
    tf_plain = [s for s in slots if s.pipeline.eco == "tf" and s.status == "success" and s.burst_id is None]
    sr.shuffle(tf_plain)
    for s in tf_plain[:3]:
        s.status, s.fault_id = "approval_rejected", "governance_approval_rejected"
    wff_slots: list[Slot] = []
    for name in ("infra-observability", "infra-db"):
        cand = [s for s in tf_plain[3:] if s.pipeline.name == name and s.fault_id is None]
        s = sr.choice(cand)
        s.status, s.fault_id = "failed", "platform_throttle_masks_lock"
        wff_slots.append(s)

    # ---- background failures, filled by quota so class shares land on target ----
    est_total = len(slots) + 10
    total_failed_target = round(FAILURE_RATE * est_total)
    used = {"user": 0, "platform": 0, "transient": 0}
    for s in slots:
        if s.status == "failed" and s.fault_id:
            used[FAULTS[s.fault_id].classification] += 1
    bg_rng = _rng(seed, "background")
    candidates = [
        s for s in slots if s.status == "success" and s.retry_of is None and s.burst_id is None and not s.pipeline.flaky
    ]
    bg_rng.shuffle(candidates)
    pos = 0
    bg_failed: list[Slot] = []
    for klass in ("user", "platform", "transient"):
        quota = max(0, round(CLASS_SHARE[klass] * total_failed_target) - used[klass])
        taken = 0
        while taken < quota and pos < len(candidates):
            s = candidates[pos]
            pos += 1
            if not _eligible(s.pipeline.eco, klass):
                continue
            fault = _pick_fault(bg_rng, s.pipeline.eco, klass)
            s.status, s.fault_id = "failed", fault.fault_id
            bg_failed.append(s)
            taken += 1
    # Transient failures are often followed by a manual re-run that passes (this is what builds flake precedent).
    retry_rng = _rng(seed, "retries")
    for s in bg_failed:
        assert s.fault_id is not None
        if FAULTS[s.fault_id].classification == "transient" and retry_rng.random() < (
            0.75 if s.fault_id == "transient_image_pull" else 0.5
        ):
            slots.append(Slot(s.pipeline, s.start + timedelta(seconds=retry_rng.randint(180, 600)), retry_of=s))

    # ---- stable ids ----
    slots.sort(key=lambda s: (s.start, s.pipeline.name, s.status))
    for i, s in enumerate(slots, start=1):
        s.exec_id = f"ex-{i:06d}"

    executions: list[dict[str, Any]] = []
    files: dict[str, str] = {}
    log_index: dict[str, Any] = {}
    followups: dict[str, Any] = {}
    failed_info: dict[str, dict[str, Any]] = {}  # exec_id -> per-failure working data
    bases = {p.name: base_files(p) for p in PIPELINES}

    for s in slots:
        node_ids = _IdGen(s.exec_id)
        fail_rng = _rng(seed, f"fail:{s.exec_id}")
        refs = s.pipeline.refs(s.template_version)
        refs["commit"] = "main"
        if s.retry_of is not None:
            refs["retry_of"] = s.retry_of.exec_id
        spec = FAULTS[s.fault_id] if s.fault_id else None
        summary = ""
        step_status_failed: str | None = None
        if spec is not None:
            step_status_failed = spec.steps[s.pipeline.eco]
            rf: RepoFault | None = None
            params = common_params(fail_rng, s.pipeline.name, s.pipeline.eco, s.start, spec)
            if spec.repo_fault:
                rf = inject(spec.fault_id, s.pipeline, bases[s.pipeline.name])
                params.update(rf.params)
                sha = _sha(seed, s.pipeline.name, spec.fault_id, s.exec_id)
                refs["commit"] = sha
                for path, text in rf.faulty.items():
                    files[f"repos/{s.pipeline.name}/{sha}/{path}"] = text
                commit_at = s.start - timedelta(seconds=fail_rng.randint(120, 1200))
                ch = Change(
                    commit_at,
                    "commit",
                    s.pipeline.name,
                    "dev-" + fail_rng.choice(["ana", "raj", "li", "sam"]),
                    FAULT_COMMIT_MESSAGES[spec.fault_id],
                    files=sorted(p for p in rf.faulty if rf.faulty[p] != bases[s.pipeline.name].get(p)),
                    sha=sha,
                )
                changes.append(ch)
                changes.append(
                    Change(
                        commit_at - timedelta(minutes=fail_rng.randint(10, 90)),
                        "commit",
                        s.pipeline.name,
                        "dev-" + fail_rng.choice(["kim", "oz"]),
                        fail_rng.choice(DOC_MESSAGES),
                        files=["README.md"] if "README.md" in bases[s.pipeline.name] else [],
                        sha=_sha(seed, "decoy", s.exec_id),
                    )
                )
                cause_keys[f"exec:{s.exec_id}"] = ch
            info: dict[str, Any] = {"spec": spec, "rf": rf, "params": params}
            if spec.fault_id != "governance_approval_rejected":
                log = render_failed_step(
                    catalog,
                    spec,
                    s.pipeline.eco,
                    params,
                    s.start,
                    fail_rng,
                    ansi=s.pipeline.eco == "tf" and fail_rng.random() < 0.5,
                )
                summary = log.summary
                path = f"logs/{s.exec_id}/{node_ids.peek_step(s.pipeline.eco, step_status_failed)}.log"
                files[path] = log.text
                log_index[path] = log.entries
                info["log_path"] = path
            failed_info[s.exec_id] = info
        root = _tree(s, node_ids, step_status_failed, summary)
        duration = fail_rng.randint(60, 360) if spec else fail_rng.randint(120, 540)
        executions.append(asdict(_execution(s, root, duration, refs)))

    # ---- benign change timeline: commits to each repo plus unrelated platform events ----
    cr = _rng(seed, "changes")
    for p in PIPELINES:
        for _ in range(5):
            at = anchor + timedelta(seconds=cr.randint(0, days * 86400 - 1))
            changes.append(
                Change(
                    at,
                    "commit",
                    p.name,
                    "dev-" + cr.choice(["ana", "raj", "li"]),
                    cr.choice(DOC_MESSAGES),
                    files=["README.md"] if "README.md" in bases[p.name] else [],
                    sha=_sha(seed, "benign", p.name, at),
                )
            )
    for kind, ref, msg in (
        ("template_release", "node-build", "Release v1.4.1"),
        ("template_release", "py-job", "Release v3.3.0"),
        ("connector_change", "ghcr-conn", "Update connector description"),
        ("runner_pool_change", "pool-medium", "Add node label"),
        ("config_change", "aws-data", "Tag cleanup"),
        ("template_release", "canary", "Release v1.0.1"),
    ):
        changes.append(
            Change(anchor + timedelta(seconds=cr.randint(0, days * 86400 - 1)), kind, ref, "platform-bot", msg)
        )
    changes.sort(key=lambda c: (c.at, c.kind, c.ref, c.message))
    for i, c in enumerate(changes, start=1):
        c.change_id = f"chg-{i:04d}"
    files["changes.json"] = _dump(
        [
            {
                "change_id": c.change_id,
                "at": _iso(c.at),
                "kind": c.kind,
                "ref": c.ref,
                "actor": c.actor,
                "message": c.message,
                "files": c.files,
                "sha": c.sha,
            }
            for c in changes
        ]
    )

    # ---- followups: simulated world response to remediation (used by the synthetic target) ----
    slot_by_id = {s.exec_id: s for s in slots}
    for exec_id, info in failed_info.items():
        spec = info["spec"]
        rf = info["rf"]
        # Uniform structure: every failure has both keys, so presence/absence reveals nothing.
        entry: dict[str, Any] = {"rerun": {"status": "failed", "log": None}, "fix_applied": None}
        if spec.classification == "transient":
            entry["rerun"] = {"status": "success", "log": None}
        elif spec.fault_id == "platform_throttle_masks_lock":
            s = slot_by_id[exec_id]
            lock_log = render_failed_step(
                catalog,
                FAULTS["platform_state_lock"],
                "tf",
                info["params"],
                s.start,
                _rng(seed, f"rerun:{exec_id}"),
                catalog_ids=("tf_state_lock",),
            )
            rerun_path = f"world/rerun_logs/{exec_id}.log"
            files[rerun_path] = lock_log.text
            log_index[rerun_path] = lock_log.entries
            entry["rerun"] = {"status": "failed", "log": rerun_path}
        else:
            entry["rerun"] = {"status": "failed", "log": info.get("log_path")}
        entry["fix_applied"] = {
            "status": "success",
            "accepted_files": (
                {path: hashlib.sha256(rf.fixed[path].encode()).hexdigest() for path in rf.fix_paths()}
                if rf is not None
                else {}
            ),
        }
        followups[exec_id] = entry
    files["world/followups.json"] = _dump(followups)
    files["ground_truth/catalog_use.json"] = _dump(log_index)
    files["executions.json"] = _dump(executions)
    for p in PIPELINES:
        for path, text in bases[p.name].items():
            files[f"repos/{p.name}/main/{path}"] = text

    labels = _labels(slots, failed_info, burst_slots, cause_keys, changes, wff_slots)
    scenarios = _scenarios(seed, labels, slots)
    for sc in scenarios:
        labels["failures"][sc["execution_id"]]["scenario_id"] = sc["scenario_id"]
    files["ground_truth/labels.json"] = _dump(labels)
    files["ground_truth/scenarios.json"] = _dump(
        {"scenarios": scenarios, "held_out_scenarios": [sc["scenario_id"] for sc in scenarios if sc["held_out"]]}
    )

    n_total = len(executions)
    n_fail = sum(1 for e in executions if e["status"] == "failed")
    n_ok = sum(1 for e in executions if e["status"] == "success")
    stats = {
        "executions": n_total,
        "success": n_ok,
        "failed": n_fail,
        "approval_rejected": n_total - n_ok - n_fail,
        "failures_by_class": _count(labels["failures"].values(), "true_classification"),
        "bursts": len(BURSTS),
        "changes": len(changes),
    }
    files["manifest.json"] = _dump(
        {
            "schema_version": SCHEMA_VERSION,
            "seed": seed,
            "anchor": _iso(anchor),
            "days": days,
            "stats": stats,
            "pipelines": [{k: v for k, v in asdict(p).items() if k != "flaky"} for p in PIPELINES],
            "files": {
                "executions": "executions.json",
                "changes": "changes.json",
                "logs": "logs/",
                "repos": "repos/",
            },
        }
    )
    return Dataset(files=files, scenarios=scenarios, stats=stats)


def _count(rows: Any, key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return dict(sorted(out.items()))


class _IdGen:
    """Deterministic node ids: <exec>-n<k>, assigned in tree order (root = 0)."""

    def __init__(self, exec_id: str) -> None:
        self.exec_id = exec_id
        self.k = 0

    def next(self) -> str:
        nid = f"{self.exec_id}-n{self.k}"
        self.k += 1
        return nid

    def peek_step(self, eco: str, step: str) -> str:
        k = 1  # root is 0
        for _stage, steps in STAGES[eco]:
            k += 1  # the stage node
            for name in steps:
                if name == step:
                    return f"{self.exec_id}-n{k}"
                k += 1
        raise KeyError(step)


def _tree(s: Slot, ids: _IdGen, failed_step: str | None, summary: str) -> Any:
    ok = "success" if s.status == "success" else "failed"
    root_id = ids.next()
    root = Node(root_id, s.pipeline.name, ok)
    failed_seen = False
    for stage_name, steps in STAGES[s.pipeline.eco]:
        stage = Node(ids.next(), stage_name, "success", parent_id=root_id)
        for name in steps:
            if failed_seen:
                status = "skipped"
            elif name == failed_step:
                failed_seen = True
                status = "rejected" if s.status == "approval_rejected" else "failed"
            else:
                status = "success"
            stage.children.append(
                Node(
                    ids.next(),
                    name,
                    status,
                    parent_id=stage.node_id,
                    error_summary=summary if status == "failed" else "",
                )
            )
        statuses = {c.status for c in stage.children}
        stage.status = (
            "failed"
            if "failed" in statuses
            else "rejected"
            if "rejected" in statuses
            else ("skipped" if statuses == {"skipped"} else "success")
        )
        root.children.append(stage)
    if s.status == "approval_rejected":
        root.status = "rejected"
    return root


def _execution(s: Slot, root: Any, duration: int, refs: dict[str, str]) -> Any:
    return Execution(
        execution_id=s.exec_id,
        pipeline=s.pipeline.name,
        status=s.status,
        started_at=_iso(s.start),
        finished_at=_iso(s.start + timedelta(seconds=duration)),
        root=root,
        refs=refs,
    )


def _related_refs(p: PipelineDef) -> set[str]:
    return {p.name, p.connector, p.runner_pool, p.template}


def _labels(
    slots: list[Slot],
    failed_info: dict[str, dict[str, Any]],
    burst_slots: dict[str, list[Slot]],
    cause_keys: dict[str, Change],
    changes: list[Change],
    wff_slots: list[Slot],
) -> dict[str, Any]:
    by_burst = {b.burst_id: b for b in BURSTS}
    slot_by_id = {s.exec_id: s for s in slots}
    retried_ok = {s.retry_of.exec_id for s in slots if s.retry_of is not None and s.status == "success"}
    failures: dict[str, Any] = {}
    for exec_id, info in failed_info.items():
        s = slot_by_id[exec_id]
        spec: FaultSpec = info["spec"]
        rf: RepoFault | None = info["rf"]
        burst = by_burst.get(s.burst_id or "")
        fleet = burst is not None
        precedent = any(
            o.exec_id in retried_ok and o.fault_id == spec.fault_id and o.pipeline is s.pipeline and o.start < s.start
            for o in slots
        )
        disposition = spec.disposition
        tier, action = spec.tier, spec.action
        if spec.tier0_path == "flake_precedent" and not precedent:
            disposition, tier, action = "escalate", None, None
        if fleet:
            disposition = "escalate"  # fleet-wide blast radius always escalates (invariant 5)
        causal: list[str] = []
        if burst is not None:
            causal.append(cause_keys[burst.burst_id].change_id)
        if f"exec:{exec_id}" in cause_keys:
            causal.append(cause_keys[f"exec:{exec_id}"].change_id)
        related = _related_refs(s.pipeline)
        decoys = [
            c.change_id
            for c in changes
            if c.change_id not in causal and s.start - timedelta(hours=6) <= c.at <= s.start and c.ref in related
        ]
        failures[exec_id] = {
            "execution_id": exec_id,
            "pipeline": s.pipeline.name,
            "fault_id": spec.fault_id,
            "true_layer": spec.layer,
            "true_classification": spec.classification,
            "disposition": disposition,
            "correct_tier": tier,
            "correct_action": action,
            "tier0_path": spec.tier0_path,
            "precedent_exists": precedent,
            "fleet_wide": fleet,
            "burst_id": s.burst_id,
            "blast_radius": {
                "executions_affected": len(burst_slots[s.burst_id]) if burst else 0,
                "shared_dimension": f"{burst.dimension}:{burst.value}" if burst else "",
            },
            "causal_change_ids": causal,
            "decoy_change_ids": decoys,
            "fix_diff": rf.fix_diff() if rf else None,
            "fix_paths": rf.fix_paths() if rf else [],
            "first_fix_fails": s in wff_slots,
            "error_catalog_ids": list(spec.catalog_ids),
            "flaky_pipeline": s.pipeline.flaky,
            "scenario_id": None,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "flaky_pipelines": sorted(p.name for p in PIPELINES if p.flaky),
        "bursts": [
            {
                "burst_id": b.burst_id,
                "fault_id": b.fault_id,
                "shared_dimension": f"{b.dimension}:{b.value}",
                "execution_ids": sorted(s.exec_id for s in burst_slots[b.burst_id]),
            }
            for b in BURSTS
        ],
        "failures": failures,
    }


def _scenarios(seed: int, labels: dict[str, Any], slots: list[Slot]) -> list[dict[str, Any]]:
    r = _rng(seed, "scenarios")
    failures = labels["failures"]
    picked: list[tuple[str, str]] = []  # (execution_id, family)
    for fault_id, quota in SCENARIO_QUOTA.items():
        pool = sorted(e for e, f in failures.items() if f["fault_id"] == fault_id and not f["fleet_wide"])
        picked += [(e, fault_id) for e in r.sample(pool, min(quota, len(pool)))]
    for b in labels["bursts"]:
        picked.append((r.choice(b["execution_ids"]), b["burst_id"]))
    start_of = {s.exec_id: s.start for s in slots}
    picked.sort(key=lambda x: start_of[x[0]])
    scenarios = [
        {"scenario_id": f"sc-{i:02d}", "execution_id": e, "held_out": False} for i, (e, _) in enumerate(picked, start=1)
    ]
    families: dict[str, list[int]] = {}
    for idx, (_, fam) in enumerate(picked):
        families.setdefault(fam, []).append(idx)
    multi = sorted(f for f, idxs in families.items() if len(idxs) >= 2)
    r.shuffle(multi)
    held = 0
    for fam in multi:
        if held >= HELD_OUT_COUNT:
            break
        scenarios[families[fam][-1]]["held_out"] = True
        held += 1
    # Second pass if fewer families than HELD_OUT_COUNT: take earlier instances too.
    for fam in multi:
        for idx in families[fam][:-1]:
            if held >= HELD_OUT_COUNT:
                break
            if not scenarios[idx]["held_out"]:
                scenarios[idx]["held_out"] = True
                held += 1
    return scenarios


__all__ = ["DEFAULT_SEED", "Dataset", "generate"]
