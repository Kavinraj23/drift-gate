"""End-to-end run of every synthetic scenario through the orchestrator, offline.

`python -m driftgate.eval.e2e --data data [--mode fake|replay|auto] [--fixtures DIR]`

Modes (there is no live mode; nothing here can reach the network or read an API key):
- `fake`   scripted model from `eval/scripted_agent.py` for every scenario (the default).
- `replay` the gateway in replay mode over recorded fixtures; a scenario without fixtures is reported as such.
- `auto`   replay where fixtures exist, otherwise the scripted fake. Rows say which one ran: a scenario that fell
           back is labelled `fake (no fixture)` and the header says the table is mixed.

Each scenario gets a fresh orchestrator, rate limiter, audit log and kill switch, so scenarios are independent.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from driftgate.adapters.synthetic import SyntheticSource
from driftgate.audit import AuditEntry
from driftgate.domain import RemediationTarget
from driftgate.eval.ground_truth import FailureLabel, GroundTruth, load_ground_truth
from driftgate.eval.metrics import Case, RecoveryBreakdown, recovery_breakdown, recovery_text, remediation_success_rate
from driftgate.eval.scripted_agent import scripted_model, two_round_factory
from driftgate.eval.tier3_scripts import revising_model_factory, scripted_reviewer, scripted_reviewer_factory
from driftgate.llm.budget import InvestigationBudget
from driftgate.llm.config import GatewayConfig
from driftgate.llm.errors import FixtureMissing
from driftgate.llm.gateway import Gateway
from driftgate.llm.replay import FixtureStore
from driftgate.llm.types import ModelClient, ModelRequest, ModelResponse
from driftgate.orchestrator import (
    AWAITING_APPROVAL,
    CLOSED,
    ESCALATED,
    EXECUTED,
    PR_PROPOSED,
    Orchestrator,
    Outcome,
    build_synthetic_orchestrator,
    make_synthetic_target,
)
from driftgate.tier3 import ReviewerHook
from driftgate.verify import install_verification

MODES = ("fake", "replay", "auto")
OK, MISMATCH, NO_FIXTURE = "ok", "MISMATCH", "no fixture"
DEFAULT_FIXTURES = Path("fixtures") / "replay"
_KILL_SWITCH_ON = {"global": True, "tiers": {"0": True, "1": True, "2": True, "3": True}}
_EXPECTED_KIND = {0: EXECUTED, 3: PR_PROPOSED}


class StepClock:
    """Deterministic clock: every read advances one second, so audit timestamps order the run."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        self._t += 1.0
        return self._t


@dataclass(frozen=True)
class Row:
    scenario_id: str
    held_out: bool
    execution_id: str
    fault_id: str
    expected: str
    actual: str
    status: str
    model: str
    model_calls: int
    tool_calls: int
    tokens: int
    note: str = ""
    rounds: int = 1
    recovery: str = ""
    pr: str = ""  # Tier 3 outcome: the PR that opened with the reviewer's verdict, or what stopped it


@dataclass
class ScenarioResult:
    row: Row
    outcome: Outcome | None
    audit: list[AuditEntry] = field(default_factory=list)
    label: FailureLabel | None = None


def expected_text(label: FailureLabel) -> str:
    if label.disposition == "remediate":
        return f"T{label.correct_tier} {label.correct_action}"
    return "close" if label.disposition == "close" else "escalate"


def actual_text(outcome: Outcome) -> str:
    rem = outcome.report.remediation
    what = f"T{rem.tier} {rem.action}" if rem else ""
    if outcome.kind == ESCALATED:
        return f"escalated ({outcome.reason[:48]})" + (f" after {outcome.rounds} rounds" if outcome.rounds > 1 else "")
    if outcome.kind == CLOSED:
        return "closed (prefilter)"
    return f"{outcome.kind} {what}" + (" (verified)" if outcome.verified else "")


def pr_text(outcome: Outcome, scripted_reviewer: bool = False) -> str:
    """Tier 3 column: `opened (approve)` / `opened (approve after revision)` or `stopped (<layer>: <verdict>)`."""
    rem = outcome.report.remediation
    if rem is None or rem.tier != 3:
        return "-"
    verdict = outcome.report.review.verdict if outcome.report.review else "no review"
    verdict += ", scripted reviewer" if scripted_reviewer else ""
    if outcome.kind == PR_PROPOSED:
        after = " after revision" if outcome.revisions else ""
        return f"opened ({verdict}{after})"
    return f"stopped ({verdict}: {outcome.reason[:40]})"


def judge(label: FailureLabel, outcome: Outcome) -> tuple[str, str]:
    """Compare an outcome with the label. Returns (status, note)."""
    rem = outcome.report.remediation
    if label.disposition == "close":
        return (OK, "") if outcome.kind == CLOSED else (MISMATCH, "expected the pre-filter to close it")
    if label.disposition == "escalate":
        if outcome.kind == ESCALATED:
            return OK, ""
        if outcome.kind == AWAITING_APPROVAL:
            return OK, "approval-gated (Tier 1/2 is stubbed); counted as human-required"
        return MISMATCH, "expected a human-required outcome"
    if (
        outcome.kind == _EXPECTED_KIND.get(-1 if label.correct_tier is None else label.correct_tier)
        and rem
        and rem.action == label.correct_action
    ):
        return OK, ""
    return MISMATCH, f"expected {expected_text(label)}"


ModelFactory = Callable[[str, InvestigationBudget], ModelClient]
REVISE_PREFIX = "revise:"  # `revise:<kind>`: the first diff is the seeded bad `kind`, the revision is correct


def _config_model_factory(gateway: Gateway, role: str = "investigator") -> ModelFactory:
    return lambda _eid, budget: gateway.bind(budget, role=role)


class _ReplayOrScripted:
    """Reviewer client for replay runs: recorded fixtures when they exist, the scripted reviewer otherwise.

    Fixtures recorded before the reviewer existed (or for a case the human has not recorded a review of) have no
    reviewer exchanges; those reviews are scripted and the Tier 3 column says so.
    """

    def __init__(self, replay: ModelClient, scripted: ModelClient, fallbacks: list[bool]) -> None:
        self._replay, self._scripted, self._fallbacks = replay, scripted, fallbacks

    def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            return self._replay.complete(request)
        except FixtureMissing:
            self._fallbacks.append(True)
            return self._scripted.complete(request)


def _replay_reviewer_factory(
    gateway: Gateway, label: FailureLabel, source: SyntheticSource, mode: str, fallbacks: list[bool]
) -> ModelFactory:
    return lambda eid, budget: _ReplayOrScripted(
        gateway.bind(budget, role="reviewer"), scripted_reviewer(label, source, mode), fallbacks
    )


def _scripted_factory(label: FailureLabel, source: SyntheticSource, variant: str) -> ModelFactory:
    if variant.startswith(REVISE_PREFIX):
        first = "diff:" + variant.removeprefix(REVISE_PREFIX)
        return revising_model_factory(lambda v: scripted_model(label, source, v), first)
    return lambda _e, _b: scripted_model(label, source, variant)


def run_scenario(
    data_dir: Path,
    label: FailureLabel,
    held_out: bool,
    *,
    mode: str = "fake",
    fixtures_dir: Path | None = None,
    variant: str = "correct",
    reviewer: str = "strict",
) -> ScenarioResult:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    source = SyntheticSource(data_dir)
    if mode == "fake" and label.first_fix_fails and variant == "correct":
        # The tempting first fix (re-run the throttled job) and the revised second round.
        return _run(
            data_dir,
            label,
            held_out,
            two_round_factory(label, source, "overconfident_tier0"),
            "fake",
            scripted_reviewer_factory(label, source, reviewer),
        )
    if mode == "fake":
        return _run(
            data_dir,
            label,
            held_out,
            _scripted_factory(label, source, variant),
            "fake",
            scripted_reviewer_factory(label, source, reviewer),
        )
    gateway = Gateway(GatewayConfig(), mode="replay", fixtures=FixtureStore(fixtures_dir or DEFAULT_FIXTURES))
    fallbacks: list[bool] = []
    try:
        return _run(
            data_dir,
            label,
            held_out,
            _config_model_factory(gateway),
            "replay",
            _replay_reviewer_factory(gateway, label, source, reviewer, fallbacks),
            fallbacks,
        )
    except FixtureMissing:
        if mode == "replay":
            row = Row(
                label.scenario_id or "",
                held_out,
                label.execution_id,
                label.fault_id,
                expected_text(label),
                "no replay fixture",
                NO_FIXTURE,
                "replay",
                0,
                0,
                0,
            )
            return ScenarioResult(row, None)
    return _run(
        data_dir,
        label,
        held_out,
        _scripted_factory(label, source, variant),
        "fake (no fixture)",
        scripted_reviewer_factory(label, source, reviewer),
    )


class NaiveClassifierOrchestrator(Orchestrator):
    """TEST DOUBLE for the wrong-first-fix drills. The real gate (correctly) refuses a Tier 0 re-run of a masked
    state lock: the primary signature, the lock, has no known-transient rule and there is no flake precedent.
    This subclass pretends the deterministic classifier ranked the throttling first, as the baseline does, so the
    first fix reaches the target and the verification loop has something to catch. Only the drills use it."""

    def _gate_facts(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        facts = super()._gate_facts(*args, **kwargs)
        return replace(facts, signature_match=True, known_transient_rule=True)


def _build_orchestrator(
    data_dir: Path,
    model_factory: Callable[[str, InvestigationBudget], ModelClient],
    tmp: str,
    target: RemediationTarget | None,
    naive: bool,
    reviewer_factory: ModelFactory,
    source: SyntheticSource,
) -> Orchestrator:
    kill = Path(tmp) / "kill_switch.json"
    kill.write_text(json.dumps(_KILL_SWITCH_ON), encoding="utf-8")
    common = {
        "audit_path": Path(tmp) / "audit.jsonl",
        "clock": StepClock(),
        "kill_switch_path": kill,
        "tier3_review": ReviewerHook(source, reviewer_factory),
    }
    if not naive:
        return build_synthetic_orchestrator(data_dir, model_factory, target=target, verification=True, **common)
    orch = build_synthetic_orchestrator(data_dir, model_factory, target=target, **common)
    orch.__class__ = NaiveClassifierOrchestrator  # same wiring, one overridden fact source
    install_verification(orch)
    return orch


def _run(
    data_dir: Path,
    label: FailureLabel,
    held_out: bool,
    model_factory: ModelFactory,
    model_name: str,
    reviewer_factory: ModelFactory,
    fallbacks: list[bool] | None = None,
    *,
    target: RemediationTarget | None = None,
    naive: bool = False,
    drill: Drill | None = None,
) -> ScenarioResult:
    with tempfile.TemporaryDirectory(prefix="driftgate-e2e-") as tmp:
        orch = _build_orchestrator(
            data_dir, model_factory, tmp, target, naive, reviewer_factory, SyntheticSource(data_dir)
        )
        outcome = orch.handle(label.execution_id)
        audit = orch.audit.read()
    status, note = judge(label, outcome) if drill is None else drill.judge(label, outcome)
    row = Row(
        drill.drill_id if drill else label.scenario_id or "",
        held_out,
        label.execution_id,
        label.fault_id,
        drill.expected if drill else expected_text(label),
        actual_text(outcome),
        status,
        "none" if outcome.kind == CLOSED else model_name,
        outcome.model_calls,
        outcome.tool_calls,
        outcome.tokens,
        note,
        outcome.rounds,
        recovery_text(label, outcome),
        pr_text(outcome, bool(fallbacks)),
    )
    return ScenarioResult(row, outcome, audit, label)


@dataclass(frozen=True)
class Drill:
    """A two-round scenario built on a labelled execution: a wrong first fix, then a scripted second round.

    Drills exist because the real gate (correctly) stops most wrong first fixes before they run, which would leave
    the verification loop untested end to end. Each drill states what a correct end state is."""

    drill_id: str
    scenario_id: str
    first: str  # scripted first-round variant
    second: str  # scripted re-investigation mode
    expected: str
    check: Callable[[Outcome], bool]
    naive: bool = False  # use the naive-classifier gate-facts double
    verify_fails: bool = False  # the simulated target reports every verification as not verified

    def judge(self, label: FailureLabel, outcome: Outcome) -> tuple[str, str]:
        return (OK, "") if self.check(outcome) else (MISMATCH, f"expected {self.expected}")


def _escalated_after_two_attempts(o: Outcome) -> bool:
    return o.kind == ESCALATED and o.rounds == 2 and len(o.report.prior_attempts) >= 1 and o.first_attempt_failed


DRILLS: tuple[Drill, ...] = (
    Drill(
        "d1-wrong-rerun",
        "sc-11",
        "overconfident_tier0",
        "revise_escalate",
        "re-run fails with the lock error, revised, escalate",
        _escalated_after_two_attempts,
        naive=True,
    ),
    Drill(
        "d2-rerun-twice",
        "sc-29",
        "overconfident_tier0",
        "repeat_tier0",
        "re-run fails, second re-run refused, hard escalate",
        lambda o: _escalated_after_two_attempts(o) and o.gate is not None and o.gate.decision == "refused",
        naive=True,
    ),
    Drill(
        "d3-wrong-file",
        "sc-17",
        "wrong_paths",
        "right_paths",
        "CI red on the wrong file, revised PR green",
        lambda o: o.kind == PR_PROPOSED and o.rounds == 2 and o.verified is True and len(o.report.prior_attempts) == 2,
    ),
    Drill(
        "d4-wrong-file-twice",
        "sc-23",
        "wrong_paths",
        "wrong_paths",
        "CI red twice, hard escalate with both attempts",
        lambda o: _escalated_after_two_attempts(o) and len(o.report.prior_attempts) == 2,
    ),
    Drill(
        "d5-unverified-rerun",
        "sc-08",
        "correct",
        "revise_escalate",
        "re-run executed but not verified, revised, escalate",
        _escalated_after_two_attempts,
        verify_fails=True,
    ),
)


def run_drill(data_dir: Path, drill: Drill, truth: GroundTruth | None = None) -> ScenarioResult:
    truth = truth or load_ground_truth(data_dir)
    label = next(s for s in truth.scenarios() if s.scenario_id == drill.scenario_id)
    source = SyntheticSource(data_dir)
    target = make_synthetic_target(data_dir, verified=not drill.verify_fails)
    factory = two_round_factory(label, source, drill.first, drill.second)
    return _run(
        data_dir,
        label,
        False,
        factory,
        "fake",
        scripted_reviewer_factory(label, source, "lenient"),  # the drill is about CI, not the reviewer
        target=target,
        naive=drill.naive,
        drill=drill,
    )


def run_all(
    data_dir: Path,
    *,
    mode: str = "fake",
    fixtures_dir: Path | None = None,
    truth: GroundTruth | None = None,
    drills: bool = False,
) -> list[ScenarioResult]:
    truth = truth or load_ground_truth(data_dir)
    results = []
    for held_out in (False, True):
        for label in truth.scenarios(held_out=held_out):
            results.append(run_scenario(data_dir, label, held_out, mode=mode, fixtures_dir=fixtures_dir))
    if drills and mode != "replay":  # drills use the scripted fake model; they are not recorded
        results += [run_drill(data_dir, d, truth) for d in DRILLS]
    return results


def loop_metrics(results: list[ScenarioResult]) -> tuple[str, RecoveryBreakdown]:
    cases: list[Case] = [(r.label, r.outcome) for r in results if r.label is not None and r.outcome is not None]
    return str(remediation_success_rate(cases)), recovery_breakdown(cases)


def format_table(rows: list[Row], mode: str, metrics: tuple[str, RecoveryBreakdown] | None = None) -> str:
    models = sorted({r.model for r in rows})
    header = f"End-to-end scenarios (mode={mode}; model source: {', '.join(models)})"
    if any(m.startswith("fake") for m in models):
        header += "\nThe scripted fake model is a test double: it shows the pipeline works, not how a real model does."
    cols = (
        "scenario",
        "set",
        "fault",
        "expected",
        "actual",
        "status",
        "source",
        "model calls",
        "tool calls",
        "tokens",
        "rounds",
        "recovery",
        "tier 3 pr",
    )
    body = [
        (
            r.scenario_id,
            "held-out" if r.held_out else "dev",
            r.fault_id,
            r.expected,
            r.actual,
            r.status,
            r.model,
            str(r.model_calls),
            str(r.tool_calls),
            str(r.tokens),
            str(r.rounds),
            r.recovery,
            r.pr or "-",
        )
        for r in rows
    ]
    widths = [max(len(c), *(len(b[i]) for b in body)) for i, c in enumerate(cols)]
    lines = [header, "", "  ".join(c.ljust(w) for c, w in zip(cols, widths, strict=True))]
    lines.append("  ".join("-" * w for w in widths))
    lines += ["  ".join(v.ljust(w) for v, w in zip(b, widths, strict=True)) for b in body]
    counts = {s: sum(r.status == s for r in rows) for s in (OK, MISMATCH, NO_FIXTURE)}
    lines += [
        "",
        f"{len(rows)} scenarios: {counts[OK]} ok, {counts[MISMATCH]} mismatch, {counts[NO_FIXTURE]} without fixture",
        f"model calls {sum(r.model_calls for r in rows)}, tool calls {sum(r.tool_calls for r in rows)}, "
        f"tokens {sum(r.tokens for r in rows)}",
    ]
    if metrics is not None:
        success, b = metrics
        lines += [
            f"remediation success (verified / attempted): {success}",
            "after a failed first attempt (re-investigation):",
            f"  recovered by verified fix:           {b.fixed}   <- the recovery rate",
            f"  correct escalation after attempt:    {b.correct_escalation}",
            f"  not recovered:                       {b.not_recovered}",
            f"wrong first fix stopped by the gate before it ran: {b.gate_blocked}",
        ]
    notes = sorted({f"{r.status}: {r.note}" for r in rows if r.note and r.status != OK})
    lines += [f"note - {n}" for n in notes]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--mode", choices=MODES, default="fake")
    ap.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    args = ap.parse_args(argv)
    results = run_all(args.data, mode=args.mode, fixtures_dir=args.fixtures, drills=True)
    rows = [r.row for r in results]
    print(format_table(rows, args.mode, loop_metrics(results)))
    return 0 if all(r.status == OK for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
