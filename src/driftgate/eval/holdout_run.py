"""Run the agent on the stored real-log holdout and write run records. CLI: `python tasks.py run-holdout`.

What this is
- For each case it builds a `HoldoutSource` from ONE stored log plus its source metadata (nothing from the label
  except a digest of it, kept in the run record), runs the real Orchestrator and Investigator on it, and records what
  the agent PROPOSED and what the gate DECIDED. Nothing is executed: the target is `ProposalOnlyTarget`, which has no
  capability at all (`execute`, `verify` and `rollback` raise). No `RemediationTarget` implementation from `adapters/`
  is imported, and nothing here can reach GitHub (invariants 1, 4, 12).
- The gate runs on deterministic facts EXACTLY as the production derivation produces them (no invented resource set
  total). With no history, Tier 0 can qualify only through a Tier 0 known-transient rule (plus a deterministic
  signature match); Tier 2 never qualifies on confidence. A lone stored job is 100% of its resource set, so the abort
  ceiling may refuse a Tier 0 re-run: that is the honest gate outcome. To keep the agent's behaviour visible, each case
  records TWO views: `action_after_gates` (the headline; goes into runs.json) and `proposal_before_gates` (what the
  agent proposed, plus the gate's decision and reason code; sidecar only, and NOT a result).
- Tier 3 is scored on the proposal: the PR path with `allow_unreviewed_tier3` (no repository content is stored, so
  there is nothing to diff-check or review against). A Tier 3 proposal counts as `pr_fix` only if it carries a
  non-empty diff and paths; the diff itself is never judged. This is stated in every report.

Models (this module never builds a gateway for a real model and never reads the environment)
- `fake`   a deterministic, label-blind plumbing double (`plumbing_model`). It reproduces the deterministic baseline's
           decisions through the real tools, gate and mapping, so it shows the pipeline works, not how a model does.
- `replay` the gateway in replay mode over `fixtures/replay-holdout/`. A case without a fixture is reported as such.
- `auto`   replay where a fixture exists, otherwise the fake; the report says which cases used which.
Recording against the real API is `record-holdout` (human only, in `eval/live.py` + `eval/holdout_record.py`).

Hard refusals (labels before runs): the holdout must pass its structural checks, `labeled_at` must be strictly in the
past for every case, and `labels.json` must have no uncommitted changes (an unknown git state also refuses).

The test split is sealed: it is run through `Holdout.test_aggregate`, nothing per case is printed for it (only counts),
and the only per-case artefacts are the run records the schema requires and a cost/tool-count sidecar.
`holdout/runs_meta.json` and `fixtures/replay-holdout/` hold per-case data for test cases (cost, proposals, recorded
model responses): do not open them.

Fake-mode (the scripted double) runs NEVER write run records: they are plumbing checks. Run records come only from the
real model (`record-holdout` live, or replay of the fixtures it wrote), each with a provenance entry in the sidecar.
Each case's record is written (atomically) as soon as that case finishes, so a crash does not lose earlier ones.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from driftgate.agents.fingerprint import prompt_fingerprint
from driftgate.agents.investigator import SUBMIT_REPORT
from driftgate.audit import AuditLog
from driftgate.domain import DryRunResult, ExecutionResult, Remediation, VerificationResult
from driftgate.eval.e2e import StepClock
from driftgate.eval.holdout import (
    DEFAULT_DIR,
    Holdout,
    HoldoutCase,
    HoldoutInvalid,
    RunRecord,
    label_digest,
    validate,
)
from driftgate.eval.holdout_eval import GitStatus, git_labels_status
from driftgate.eval.holdout_meta import (
    META_FILE,
    META_VERSION,
    MODE_FAKE,
    MODE_REPLAY,
    RUNS_FILE,
    provenance_for,
    record_digest,
)
from driftgate.eval.holdout_source import TIME_BASIS_FETCH, TIME_BASIS_LOG, HoldoutInput, HoldoutSource
from driftgate.gates import DEFAULT_KILL_SWITCH_PATH, GateFacts, KillSwitch, RateLimiter, SafetyGate
from driftgate.llm.budget import InvestigationBudget
from driftgate.llm.config import GatewayConfig, InvestigationLimits
from driftgate.llm.errors import FixtureMissing
from driftgate.llm.fake import FakeModel
from driftgate.llm.gateway import Gateway
from driftgate.llm.replay import FixtureStore, load_manifest
from driftgate.llm.types import ModelClient, ModelRequest, ModelResponse, ToolCall, Usage
from driftgate.orchestrator import CLOSED, Orchestrator, Outcome
from driftgate.policy import DEFAULT_POLICY_PATH, load_policy
from driftgate.retrieval import HistoryIndex

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FIXTURES = ROOT / "fixtures" / "replay-holdout"

#: `--tools`: which optional tools the agent is offered. The default is none (the MVP 1 tool set).
TOOL_SETS: dict[str, frozenset[str]] = {
    "none": frozenset(),
    "similar_history": frozenset({"similar_history"}),
    "changes_in_window": frozenset({"changes_in_window"}),
    "both": frozenset({"similar_history", "changes_in_window"}),
}
#: Real holdout cases have no history and no change timeline, so the optional tools could only return empties: offering
#: them would measure nothing and could be mistaken for a tools-on result. They are REFUSED for holdout runs; the N2/N3
#: on/off comparison stays on the synthetic data (docs/design/N1-runner.md). The flag exists so the recorded value is
#: explicit ("none") and so a request for a tool set fails loudly instead of silently running without it.
TOOLS_BACKING = "none"
TOOLS_REFUSAL = (
    "--tools {tools} is refused for holdout runs: stored real logs have no history index and no change timeline, so "
    "similar_history and changes_in_window would return only empty results and the on/off comparison would be "
    "meaningless. The N2/N3 comparison stays on the synthetic data. Use --tools none."
)

REPLAY, FAKE = MODE_REPLAY, MODE_FAKE
MODES = ("auto", "fake", "replay")
RERUN_ACTIONS = ("rerun_failed_job", "rerun_workflow")
FAKE_MODEL_NAME = "fake:deterministic-plumbing-double"
TARGET_DESCRIPTION = "proposal-only: execute, verify and rollback raise; nothing is executed"
GATE_NOTE = (
    "gate inputs are the production derivation, unmodified; a lone stored job is 100% of its resource set, so the "
    "abort ceiling may refuse a Tier 0 re-run (honest gate outcome). The headline is the action AFTER the gates; the "
    "proposal BEFORE the gates is a separate, ungated view"
)
SEALED_NOTE = (
    "note: holdout/runs_meta.json and fixtures/replay-holdout/ hold per-case data for test-split cases (cost, "
    "proposals before gates, recorded model responses). Do not open them; read the aggregate output only."
)
SCORING_NOTE = (
    "Tier 3 scored on the proposal (non-empty diff and paths); no repository content is stored, so no diff check "
    "and no reviewer. Nothing is executed."
)

META_NOTES = {"target": TARGET_DESCRIPTION, "gate_inputs": GATE_NOTE, "scoring": SCORING_NOTE, "sealed": SEALED_NOTE}

Clock = Callable[[], datetime]
ModelFactory = Callable[[str, InvestigationBudget], ModelClient]


class RunRefused(Exception):
    """The runner will not start. The reasons contain no label content and, for the test split, only opaque ids."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


# ---------------------------------------------------------------------------------------------------------------
# The target: records, never acts.
# ---------------------------------------------------------------------------------------------------------------
class ProposalOnlyTarget:
    """A `RemediationTarget` with no capability. `dry_run` declines, so the orchestrator never reaches `execute`;
    calling `execute`, `verify` or `rollback` is a bug and raises. The gate's decision is what gets scored."""

    def __init__(self) -> None:
        self.dry_runs: list[str] = []

    def dry_run(self, action: Remediation) -> DryRunResult:
        self.dry_runs.append(action.action)
        return DryRunResult(False, "holdout run: proposals are scored, never executed")

    def execute(self, action: Remediation) -> ExecutionResult:
        raise AssertionError("holdout runs never execute a remediation")

    def verify(self, action: Remediation) -> VerificationResult:
        raise AssertionError("holdout runs never verify a remediation")

    def rollback(self, action: Remediation) -> ExecutionResult:
        raise AssertionError("holdout runs never roll back a remediation")


class FactsRecordingOrchestrator(Orchestrator):
    """The real orchestrator, unmodified in behaviour: it only remembers the last facts it handed the gate, so the
    report can say whether the abort ceiling alone refused a proposal. Nothing is changed or lifted."""

    last_facts: GateFacts | None = None

    def _gate_facts(self, execution_id, proposal, analysis, fleet, fingerprint, report, gate_results, proof=None):  # type: ignore[no-untyped-def]
        facts = super()._gate_facts(execution_id, proposal, analysis, fleet, fingerprint, report, gate_results, proof)
        self.last_facts = facts
        return facts


# ---------------------------------------------------------------------------------------------------------------
# The label-blind plumbing double.
# ---------------------------------------------------------------------------------------------------------------
def _results(request: ModelRequest) -> dict[str, dict[str, Any]]:
    names: dict[str, str] = {}
    out: dict[str, dict[str, Any]] = {}
    for m in request.messages:
        if not isinstance(m["content"], list):
            continue
        for block in m["content"]:
            if block.get("type") == "tool_use":
                names[block["id"]] = block["name"]
            elif block.get("type") == "tool_result" and not block.get("is_error"):
                out[block["tool_use_id"]] = {
                    "tool": names.get(block["tool_use_id"], "?"),
                    "data": json.loads(block["content"]),
                }
    return out


def plumbing_model(execution_id: str) -> FakeModel:
    """A deterministic scripted model that reads ONLY tool results (never a label): get_execution,
    classify_signature, get_step_logs, then a report built from the signature. It proposes a Tier 0 re-run exactly
    when the signature is deterministic AND has an authored Tier 0 rule; otherwise it proposes nothing. That is the
    deterministic baseline's decision, run through the real pipeline. A test double, not a model."""

    def turn1(_r: ModelRequest) -> ModelResponse:
        calls = [
            ToolCall("t1", "get_execution", {"execution_id": execution_id}),
            ToolCall("t2", "classify_signature", {"execution_id": execution_id}),
        ]
        return ModelResponse(tool_calls=calls, usage=Usage(input_tokens=900, output_tokens=140))

    def turn2(_r: ModelRequest) -> ModelResponse:
        call = ToolCall("t3", "get_step_logs", {"execution_id": execution_id})
        return ModelResponse(tool_calls=[call], usage=Usage(input_tokens=1600, output_tokens=140))

    def turn3(request: ModelRequest) -> ModelResponse:
        sig = _results(request)["t2"]["data"]
        deterministic = bool(sig.get("deterministic_match"))
        acted = deterministic and bool(sig.get("tier0_rule_exists"))
        layer = str(sig.get("layer") or "L4")
        report: dict[str, Any] = {
            "classification": "transient" if acted else str(sig.get("classification") or "unknown"),
            "layer": layer,
            "confidence": 0.5,
            "hypothesis": f"signature {sig.get('signature')} matched in the stored failed step",
            "evidence": [
                {
                    "source": "t2",
                    "finding": f"signature {sig.get('signature')}; deterministic={deterministic}; "
                    f"Tier 0 rule={sig.get('tier0_rule_exists')}",
                    "supports": "classification",
                }
            ],
        }
        if acted:
            report["remediation"] = {
                "tier": 0,
                "action": "rerun_failed_job",
                "rationale": "deterministic signature with an authored known-transient rule",
                "reversible": True,
            }
        else:
            report["escalation_reason"] = (
                "no deterministic signature with a Tier 0 rule; this double never authors a fix"
            )
        return ModelResponse(
            tool_calls=[ToolCall("t4", SUBMIT_REPORT, report)], usage=Usage(input_tokens=2600, output_tokens=220)
        )

    return FakeModel([turn1, turn2, turn3])


# ---------------------------------------------------------------------------------------------------------------
# Loader: the ONLY place a label-bearing case is turned into agent-visible input.
# ---------------------------------------------------------------------------------------------------------------
_LOG_TS = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.[0-9]+)?Z", re.ASCII)


def _log_times(log: str) -> tuple[str, str] | None:
    stamps = [m.group(1) for line in log.splitlines() if (m := _LOG_TS.match(line))]
    return (stamps[0] + "Z", stamps[-1] + "Z") if stamps else None


def _fetched_step_name(root: Path, case_id: str) -> str | None:
    """The failed step's name from `fetched/<id>.json` (written by holdout-fetch; metadata only, no label). Optional."""
    path = root / "fetched" / f"{case_id}.json"
    try:
        name = json.loads(path.read_text(encoding="utf-8"))["step"]["name"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return name if isinstance(name, str) and name else None


def inputs_for(case: HoldoutCase, log: str, root: Path) -> HoldoutInput:
    """Project a case onto the agent-visible fields. This function reads `case.id` and `case.raw["source"]` and
    nothing else from the case: not the layer, classification, action, rationale or split."""
    src = case.raw["source"]
    times = _log_times(log)
    if times is not None:
        started, finished, basis = times[0], times[1], TIME_BASIS_LOG
    else:
        started = finished = f"{src['fetch_date']}T00:00:00Z"
        basis = TIME_BASIS_FETCH
    return HoldoutInput(
        case_id=case.id,
        log_text=log,
        repo=src["repo"],
        run_id=int(src["run_id"]),
        job=src["job"],
        step_name=_fetched_step_name(root, case.id) or src["job"],
        commit_sha=src["commit_sha"],
        started_at=started,
        finished_at=finished,
        time_basis=basis,
    )


# ---------------------------------------------------------------------------------------------------------------
# One case.
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CaseRun:
    case_id: str
    layer: str
    classification: str
    action: dict[str, str]  # {"kind": rerun|pr_fix|escalate[, "fix": name]}
    outcome_kind: str
    gate_decision: str  # allowed | downgraded | refused | none
    reason: str
    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    dry_runs: tuple[str, ...]  # what the proposal-only target was asked to dry-run (never executed)
    proposal: dict[str, Any]  # the ungated view: what the agent proposed and what the gate said (not the headline)


def map_outcome(outcome: Outcome) -> tuple[str, str, dict[str, str], str]:
    """(layer, classification, holdout action, note) from what the agent proposed and the gate decided."""
    report = outcome.report
    layer, classification = report.layer, report.classification
    rem = report.remediation
    gate = outcome.gate
    if outcome.kind == CLOSED or gate is None or not gate.allowed or rem is None:
        return layer, classification, {"kind": "escalate"}, outcome.reason or "no remediation"
    if rem.tier == 0 and rem.action in RERUN_ACTIONS:
        return layer, classification, {"kind": "rerun"}, f"Tier 0 {rem.action} allowed by the gate"
    if rem.tier == 3:
        inv = outcome.investigation
        proposal = inv.proposal if inv is not None else None
        if proposal is not None and proposal.diff.strip() and proposal.paths:
            return layer, classification, {"kind": "pr_fix", "fix": rem.action}, f"Tier 3 {rem.action} proposed"
        return layer, classification, {"kind": "escalate"}, "Tier 3 proposal had no diff or no paths"
    return (
        layer,
        classification,
        {"kind": "escalate"},
        f"Tier {rem.tier} {rem.action}: needs a human, not a holdout action",
    )


#: Gate reason text (gates.py) -> a short, stable code. First match wins.
_REASON_CODES: tuple[tuple[str, str], ...] = (
    ("100% of the resource set", "abort_ceiling_set_100pct"),
    ("empty proposed set", "empty_set"),
    ("kill switch", "kill_switch"),
    ("rate limit", "rate_limit"),
    ("blast radius", "fleet_blast_radius"),
    ("more than one shared resource", "shared_resources"),
    ("resource set size unknown", "set_size_unknown"),
    ("second failure", "prior_tier0_failed"),
    ("no deterministic signature match", "no_signature_match"),
    ("no flake precedent", "no_tier0_eligibility"),
    ("does not match tier", "gate_label_mismatch"),
)
ABORT_CEILING_CODE = "abort_ceiling_set_100pct"


def reason_code(reason: str) -> str:
    return next((code for needle, code in _REASON_CODES if needle in reason), "other")


def _ceiling_was_the_only_blocker(gate: Any, facts: GateFacts | None) -> bool:
    """True when the gate refused on the 100%-of-set ceiling AND every other deterministic condition of the proposal's
    OWN tier was met, judged from the gate's own facts. Nothing is re-decided or lifted.
    - Tier 0: deterministic signature match, a known-transient rule or flake precedent, no prior failed retry, blast
      radius within the ceiling, at most one shared resource. (The kill switch is checked before the ceiling, so a
      ceiling refusal already passed it.)
    - Tier 1: its eligibility facts are not derivable from one stored log, so it is never called a ceiling-only case.
    - Tier 2: lock holder provably dead, death proof, dry run ok, plus the common blast/shared checks. On a holdout
      source these facts are unset, so this is false there.
    - Tier 3: the gate does not apply the ceiling to a PR; never a ceiling case."""
    if facts is None or gate is None or gate.decision != "refused" or reason_code(gate.reason) != ABORT_CEILING_CODE:
        return False
    common = (
        facts.blast_radius.executions_affected <= (gate.abort_ceiling or 0) and len(set(facts.shared_resources)) <= 1
    )
    tier = getattr(gate, "proposed_tier", None)
    if tier == 0:
        return bool(
            facts.signature_match
            and (facts.flake_precedent or facts.known_transient_rule)
            and not facts.prior_tier0_failed
            and common
        )
    if tier == 2:
        return bool(facts.lock_holder == "dead" and facts.holder_death_proof and facts.dry_run_ok and common)
    return False  # tier 1, tier 3, or an unknown tier: fail closed


def proposal_view(outcome: Outcome, facts: GateFacts | None) -> dict[str, Any]:
    """What the agent PROPOSED (before any gate) in the holdout vocabulary, with the gate's decision and reason code.
    This is an ungated view for understanding the agent. It is never the headline and never scored as a result."""
    rem = outcome.report.remediation
    gate = outcome.gate
    view: dict[str, Any] = {"kind": "escalate", "tier": None, "action": None}
    if rem is not None:
        view["tier"], view["action"] = rem.tier, rem.action
        if rem.tier == 0 and rem.action in RERUN_ACTIONS:
            view["kind"] = "rerun"
        elif rem.tier == 3:
            inv = outcome.investigation
            proposal = inv.proposal if inv is not None else None
            if proposal is not None and proposal.diff.strip() and proposal.paths:
                view["kind"], view["fix"] = "pr_fix", rem.action
    view["gate_decision"] = gate.decision if gate is not None else "none"
    view["gate_reason"] = gate.reason if gate is not None else ""
    view["gate_reason_code"] = reason_code(gate.reason) if gate is not None else "none"
    view["refused_by_abort_ceiling_only"] = _ceiling_was_the_only_blocker(gate, facts)
    return view


def build_orchestrator(
    source: HoldoutSource,
    target: ProposalOnlyTarget,
    model_factory: ModelFactory,
    tmp: str,
    *,
    tools: str = "none",
    kill_switch_path: Path | str = DEFAULT_KILL_SWITCH_PATH,
    policy_path: Path | str = DEFAULT_POLICY_PATH,
) -> FactsRecordingOrchestrator:
    clock = StepClock()
    enabled = TOOL_SETS[tools]
    gate = SafetyGate(KillSwitch(kill_switch_path), RateLimiter(clock), load_policy(policy_path).abort_ceiling)
    orch = FactsRecordingOrchestrator(
        source,
        target,
        gate,
        AuditLog(Path(tmp) / "audit.jsonl", clock),
        model_factory,
        limits=InvestigationLimits(),
        # No reviewer and no repository content: Tier 3 is scored on the proposal (see module docstring).
        allow_unreviewed_tier3=True,
        history=HistoryIndex(":memory:") if "similar_history" in enabled else None,
        optional_tools=enabled,
    )
    return orch


def run_case(
    inp: HoldoutInput,
    model_factory: ModelFactory,
    *,
    tools: str = "none",
    kill_switch_path: Path | str = DEFAULT_KILL_SWITCH_PATH,
    policy_path: Path | str = DEFAULT_POLICY_PATH,
) -> CaseRun:
    """Run the pipeline on one case. Raises `FixtureMissing` when a replay fixture is absent."""
    source, target = HoldoutSource(inp), ProposalOnlyTarget()
    with tempfile.TemporaryDirectory(prefix="driftgate-holdout-") as tmp:
        orch = build_orchestrator(
            source,
            target,
            model_factory,
            tmp,
            tools=tools,
            kill_switch_path=kill_switch_path,
            policy_path=policy_path,
        )
        outcome = orch.handle(inp.case_id)
        facts = orch.last_facts
    layer, classification, action, note = map_outcome(outcome)
    run = outcome.report.run
    return CaseRun(
        case_id=inp.case_id,
        layer=layer,
        classification=classification,
        action=action,
        outcome_kind=outcome.kind,
        gate_decision=outcome.gate.decision if outcome.gate else "none",
        reason=note,
        model_calls=outcome.model_calls,
        tool_calls=outcome.tool_calls,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cost_usd=run.cost_usd,
        dry_runs=tuple(target.dry_runs),
        proposal=proposal_view(outcome, facts),
    )


# ---------------------------------------------------------------------------------------------------------------
# Preconditions.
# ---------------------------------------------------------------------------------------------------------------
def preflight(root: Path, now: datetime, git_status: GitStatus) -> Holdout:
    """Load the holdout or refuse. Refusals: structural problems (schema, hashes, redaction, label timing, run
    consistency), uncommitted or unknowable git state of labels.json, and any case whose `labeled_at` is not strictly
    before `now`. Unmet quotas do not refuse: a partial holdout can be run while it is still being collected."""
    report = validate(root, now=now)
    if report.problems:
        raise RunRefused(list(report.problems))
    if report.counts["total"] == 0:
        raise RunRefused(["no holdout cases are stored"])
    status = git_status(root)
    if status is None:
        raise RunRefused(["cannot determine the git state of labels.json; refusing (fail closed)"])
    if status.strip():
        raise RunRefused(["labels.json has uncommitted changes; commit the labels before any run uses them"])
    try:
        holdout = Holdout.load(root, now=now)
    except HoldoutInvalid as e:
        raise RunRefused(e.problems) from None
    latest = max(
        [c.labeled_at for c in holdout.dev_cases()]
        + [
            holdout.test_aggregate(
                lambda c, _l, _r: c.labeled_at, lambda xs: max(xs, default=datetime.min.replace(tzinfo=UTC))
            )
        ]
    )
    if latest >= now:
        raise RunRefused(["a case was labeled at or after the run time; labels must be written before runs"])
    return holdout


# ---------------------------------------------------------------------------------------------------------------
# Records.
# ---------------------------------------------------------------------------------------------------------------
def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_record(case: HoldoutCase, run: CaseRun, ran_at: datetime) -> dict[str, Any]:
    return {
        "case_id": case.id,
        "system": "agent",
        "ran_at": iso(ran_at),
        "label_sha256": label_digest(case.raw),
        "layer": run.layer,
        "classification": run.classification,
        "action": dict(run.action),
    }


def meta_entry(
    run: CaseRun, record: dict[str, Any], ran_at: datetime, *, mode: str, model: str, tools: str, fixture_id: str | None
) -> dict[str, Any]:
    """Cost, provenance and the ungated proposal for one run, keyed by case id in `runs_meta.json`. `provenance` and
    `record_sha256` are what `eval-holdout` requires before it will use the run record. No label, no log text."""
    return {
        "ran_at": iso(ran_at),
        "mode": mode,
        "model": model,
        "provenance": provenance_for(mode),
        "record_sha256": record_digest(record),
        "tools": tools,
        "tools_backing": TOOLS_BACKING,
        "fixture_id": fixture_id,
        "prompt_fingerprint": prompt_fingerprint(),
        "model_calls": run.model_calls,
        "tool_calls": run.tool_calls,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "cost_usd": round(run.cost_usd, 6),
        "proposal_before_gates": dict(run.proposal),  # ungated view; NOT the headline
    }


def _write_json(path: Path, doc: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_records(root: Path, records: list[dict[str, Any]], meta: dict[str, dict[str, Any]]) -> None:
    """Append run records to runs.json (latest wins per case at load time) and merge the sidecar. The schema of
    runs.json is untouched; extra provenance lives only in runs_meta.json. Both writes are atomic. runs.json goes first:
    a crash between the two leaves a record with no matching sidecar entry, which `eval-holdout` refuses (loudly)
    instead of trusting."""
    if not records:
        return
    runs_path = root / RUNS_FILE
    doc = json.loads(runs_path.read_text(encoding="utf-8")) if runs_path.exists() else {"schema_version": 1, "runs": []}
    doc["runs"] = [*doc["runs"], *records]
    _write_json(runs_path, doc)
    meta_path = root / META_FILE
    sidecar = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    runs = {**sidecar.get("runs", {}), **meta}
    doc_meta = {"schema_version": META_VERSION, "notes": META_NOTES, "runs": dict(sorted(runs.items()))}
    _write_json(meta_path, doc_meta)


# ---------------------------------------------------------------------------------------------------------------
# Running a set of cases.
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class Tally:
    """Aggregate over a split. Safe to print for the sealed test split: counts only, no ids, no reasons."""

    cases: int = 0
    ran: int = 0
    skipped: int = 0
    no_fixture: int = 0
    unusable: int = 0
    actions: Counter[str] = field(default_factory=Counter)
    model_calls: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0


@dataclass
class Session:
    """Mutable state of one runner session: the records and sidecar entries to write, and the stop reason."""

    records: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    stopped: str = ""
    #: Where a finished case's record goes (once per case, as soon as it is done). None: nothing is written.
    sink: Callable[[list[dict[str, Any]], dict[str, dict[str, Any]]], None] | None = None
    committed: set[str] = field(default_factory=set)


def commit_case(session: Session, case_id: str) -> None:
    """Write one finished case's record and sidecar entry now (atomically), so a later crash cannot lose them. A case
    whose record was dropped (gateway failure) or never made (fake double) has nothing pending and writes nothing."""
    record = next((r for r in session.records if r["case_id"] == case_id), None)
    if record is None or case_id in session.committed or session.sink is None:
        return
    session.sink([record], {case_id: session.meta[case_id]})
    session.committed.add(case_id)


@dataclass(frozen=True)
class Choice:
    """How a case can be run: the model factory, and the labels the sidecar records."""

    factory: ModelFactory
    mode: str
    model: str
    fixture_id: str | None


Chooser = Callable[[str], list[Choice]]  # case id -> ways to run it, in order of preference
Process = Callable[[HoldoutCase, str, "RunRecord | None"], "tuple[str, CaseRun | None, str]"]


@dataclass(frozen=True)
class RunContext:
    root: Path
    now: datetime
    tools: str = "none"
    wanted: frozenset[str] | None = None
    rerun: bool = False
    kill_switch_path: Path | str = DEFAULT_KILL_SWITCH_PATH
    policy_path: Path | str = DEFAULT_POLICY_PATH


def skip_reason(ctx: RunContext, case: HoldoutCase, prior: RunRecord | None) -> str:
    if ctx.wanted is not None and case.id not in ctx.wanted:
        return "not selected"
    if prior is not None and not ctx.rerun:
        return "already has a run record (use --rerun)"
    return ""


def process_case(
    ctx: RunContext, session: Session, choose: Chooser, case: HoldoutCase, log: str, prior: RunRecord | None
) -> tuple[str, CaseRun | None, str]:
    """Returns (status, run, detail). status: ran | skipped | no_fixture. Choices are tried in order; a missing or
    stale replay fixture moves on to the next. Bugs are not caught here."""
    if session.stopped:
        return "skipped", None, session.stopped
    if why := skip_reason(ctx, case, prior):
        return "skipped", None, why
    inp = inputs_for(case, log, ctx.root)
    for choice in choose(case.id):
        try:
            run = run_case(
                inp,
                choice.factory,
                tools=ctx.tools,
                kill_switch_path=ctx.kill_switch_path,
                policy_path=ctx.policy_path,
            )
        except FixtureMissing:
            continue
        if choice.mode == FAKE:  # a scripted double is a plumbing check: it NEVER produces a run record
            return "ran", run, f"not recorded ({choice.model}; fake runs never write run records)"
        record = run_record(case, run, ctx.now)
        session.records.append(record)
        session.meta[case.id] = meta_entry(
            run, record, ctx.now, mode=choice.mode, model=choice.model, tools=ctx.tools, fixture_id=choice.fixture_id
        )
        return "ran", run, "recorded"
    return "no_fixture", None, "no replay fixture (missing or stale)"


def tally_add(t: Tally, status: str, run: CaseRun | None) -> None:
    t.cases += 1
    if status == "ran" and run is not None:
        t.ran += 1
        t.actions[run.action["kind"]] += 1
        t.model_calls += run.model_calls
        t.tool_calls += run.tool_calls
        t.cost_usd += run.cost_usd
    elif status == "no_fixture":
        t.no_fixture += 1
    elif status == "unusable":
        t.unusable += 1
    else:
        t.skipped += 1


def format_tally(name: str, t: Tally) -> str:
    acts = ", ".join(f"{k} {t.actions[k]}" for k in ("rerun", "pr_fix", "escalate") if t.actions[k]) or "none"
    return (
        f"{name}: {t.cases} cases; ran {t.ran}, skipped {t.skipped}, without fixture {t.no_fixture}, unusable "
        f"{t.unusable}; proposed actions: {acts}; model calls {t.model_calls}, tool calls {t.tool_calls}, "
        f"cost ${t.cost_usd:.4f}"
    )


def dev_line(case_id: str, status: str, run: CaseRun | None, detail: str) -> str:
    if run is None:
        return f"{case_id}: {status} ({detail})"
    fix = f" ({run.action['fix']})" if "fix" in run.action else ""
    return (
        f"{case_id}: {run.action['kind']}{fix} | {run.layer}/{run.classification} | gate {run.gate_decision} | "
        f"{run.reason} | {run.model_calls} model calls, {run.tool_calls} tool calls | {detail}"
    )


def iterate(holdout: Holdout, process: Process, *, order: tuple[str, ...], out: TextIO) -> dict[str, Tally]:
    """Run the splits in `order`. Dev cases print one line each. The test split goes through the aggregate door:
    its per-case results are discarded here and only one aggregate line is printed."""
    tallies: dict[str, Tally] = {"dev": Tally(), "test": Tally()}

    def one(case: HoldoutCase, log: str, prior: RunRecord | None) -> tuple[str, CaseRun | None, str]:
        status, run, detail = process(case, log, prior)
        tally_add(tallies[case.split], status, run)
        return status, run, detail

    for split in order:
        if split == "dev":
            for case, (status, run, detail) in holdout.dev_details(one):
                print(dev_line(case.id, status, run, detail), file=out)
        else:
            holdout.test_aggregate(one, len)  # per-case results are dropped by the aggregate
    return tallies


def print_tallies(tallies: dict[str, Tally], order: tuple[str, ...], out: TextIO) -> None:
    for split in order:
        label = "dev" if split == "dev" else "test (sealed: aggregate only)"
        print(format_tally(label, tallies[split]), file=out)


# ---------------------------------------------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------------------------------------------
UNKNOWN_MODEL = "unknown"


def fixture_model(manifest: dict[str, Any], case_id: str) -> str:
    """The investigator model `record-holdout` wrote into the fixtures' manifest for this case, else `unknown`. Never
    the default config's model: replay must not claim a model it cannot show was used."""
    try:
        model = manifest["scenarios"][case_id]["models"]["investigator"]
    except (KeyError, TypeError):
        return UNKNOWN_MODEL
    return model if isinstance(model, str) and model.strip() else UNKNOWN_MODEL


def make_chooser(mode: str, fixtures_dir: Path) -> tuple[Chooser, str]:
    """Per-case model choices for the offline modes, and a one-line description of the model source. Replay uses a
    gateway in replay mode (no network, no key, never the SDK)."""
    cfg = GatewayConfig()
    manifest = load_manifest(fixtures_dir)
    gateway = Gateway(cfg, mode="replay", fixtures=FixtureStore(fixtures_dir, fingerprint=prompt_fingerprint()))

    def replay_factory(_eid: str, budget: InvestigationBudget) -> ModelClient:
        return gateway.bind(budget, role="investigator")

    def fake_factory(eid: str, _budget: InvestigationBudget) -> ModelClient:
        return plumbing_model(eid)

    def choose(case_id: str) -> list[Choice]:
        replay = Choice(replay_factory, REPLAY, fixture_model(manifest, case_id), case_id)
        fake = Choice(fake_factory, FAKE, FAKE_MODEL_NAME, None)
        return {"fake": [fake], "replay": [replay], "auto": [replay, fake]}[mode]

    descriptions = {
        "fake": "scripted deterministic double (label-blind plumbing check; NOT a model)",
        "replay": f"replay fixtures in {fixtures_dir} (a case without a fixture is not run)",
        "auto": f"replay fixtures in {fixtures_dir}; the deterministic plumbing double where a case has none",
    }
    return choose, descriptions[mode]


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python tasks.py run-holdout", description=__doc__)
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--mode", choices=MODES, default="auto")
    ap.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    ap.add_argument("--split", choices=["dev", "test", "all"], default="all", help="dev runs first, then test")
    ap.add_argument("--cases", nargs="*", default=None, help="run only these case ids")
    ap.add_argument("--tools", choices=sorted(TOOL_SETS), default="none")
    ap.add_argument("--write", action="store_true", help="append run records to runs.json (replay-served cases)")
    ap.add_argument("--rerun", action="store_true", help="run cases that already have a run record")
    return ap.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    clock: Clock | None = None,
    git_status: GitStatus | None = None,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    args = parse_args(list(argv if argv is not None else sys.argv[1:]))
    now = (clock or (lambda: datetime.now(UTC)))()
    if args.tools != "none":
        print(TOOLS_REFUSAL.format(tools=args.tools), file=out)
        return 2
    try:
        holdout = preflight(args.dir, now, git_status or git_labels_status)
    except RunRefused as e:
        print("run-holdout: REFUSED", file=out)
        for reason in e.reasons:
            print(f"  {reason}", file=out)
        return 1
    order = {"dev": ("dev",), "test": ("test",), "all": ("dev", "test")}[args.split]
    choose, desc = make_chooser(args.mode, args.fixtures)
    ctx = RunContext(
        root=args.dir,
        now=now,
        tools=args.tools,
        wanted=frozenset(args.cases) if args.cases is not None else None,
        rerun=args.rerun,
    )
    print(f"run-holdout (offline, no API key, nothing executed): model source: {desc}", file=out)
    print("tools offered: standard set only (optional tools are refused for holdout runs)", file=out)
    print(f"target: {TARGET_DESCRIPTION}. Gate: {GATE_NOTE}. {SCORING_NOTE}", file=out)
    print(SEALED_NOTE, file=out)
    session = Session(sink=(lambda recs, meta: append_records(args.dir, recs, meta)) if args.write else None)

    def process(c: HoldoutCase, lg: str, p: RunRecord | None) -> tuple[str, CaseRun | None, str]:
        result = process_case(ctx, session, choose, c, lg, p)
        commit_case(session, c.id)  # written now, not at the end: a crash later cannot lose it
        return result

    tallies = iterate(holdout, process, order=order, out=out)
    print_tallies(tallies, order, out)
    if args.write:
        print(f"wrote {len(session.committed)} run record(s) to {args.dir / RUNS_FILE} (+ {META_FILE})", file=out)
    else:
        print("no files written (pass --write to append run records)", file=out)
    if args.mode != REPLAY:
        print(
            "note: cases served by the fake double are plumbing checks, not agent results; they write no records.",
            file=out,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
