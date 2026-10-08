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
from dataclasses import dataclass, field
from pathlib import Path

from driftgate.adapters.synthetic import SyntheticSource
from driftgate.audit import AuditEntry
from driftgate.eval.ground_truth import FailureLabel, GroundTruth, load_ground_truth
from driftgate.eval.scripted_agent import scripted_model
from driftgate.llm.budget import InvestigationBudget
from driftgate.llm.config import GatewayConfig
from driftgate.llm.errors import FixtureMissing
from driftgate.llm.gateway import Gateway
from driftgate.llm.replay import FixtureStore
from driftgate.llm.types import ModelClient
from driftgate.orchestrator import (
    AWAITING_APPROVAL,
    CLOSED,
    ESCALATED,
    EXECUTED,
    PR_PROPOSED,
    Orchestrator,
    Outcome,
    build_synthetic_orchestrator,
)

MODES = ("fake", "replay", "auto")
OK, CONFLICT, MISMATCH, NO_FIXTURE = "ok", "known label conflict", "MISMATCH", "no fixture"
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


@dataclass
class ScenarioResult:
    row: Row
    outcome: Outcome | None
    audit: list[AuditEntry] = field(default_factory=list)


def expected_text(label: FailureLabel) -> str:
    if label.disposition == "remediate":
        return f"T{label.correct_tier} {label.correct_action}"
    return "close" if label.disposition == "close" else "escalate"


def actual_text(outcome: Outcome) -> str:
    rem = outcome.report.remediation
    what = f"T{rem.tier} {rem.action}" if rem else ""
    if outcome.kind == ESCALATED:
        return f"escalated ({outcome.reason[:48]})"
    if outcome.kind == CLOSED:
        return "closed (prefilter)"
    return f"{outcome.kind} {what}"


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
    gate_reason = outcome.gate.reason if outcome.gate else ""
    if (
        label.tier0_path == "flake_precedent"
        and outcome.kind == ESCALATED
        and "no deterministic signature" in gate_reason
    ):
        return (
            CONFLICT,
            "label says Tier 0 via flake precedent; invariant 3 needs a deterministic signature (BLOCKERS.md)",
        )
    return MISMATCH, f"expected {expected_text(label)}"


def _config_model_factory(gateway: Gateway) -> Callable[[str, InvestigationBudget], ModelClient]:
    return lambda _eid, budget: gateway.bind(budget)


def run_scenario(
    data_dir: Path,
    label: FailureLabel,
    held_out: bool,
    *,
    mode: str = "fake",
    fixtures_dir: Path | None = None,
    variant: str = "correct",
) -> ScenarioResult:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    source = SyntheticSource(data_dir)
    if mode == "fake":
        return _run(data_dir, label, held_out, lambda _e, _b: scripted_model(label, source, variant), "fake")
    gateway = Gateway(GatewayConfig(), mode="replay", fixtures=FixtureStore(fixtures_dir or DEFAULT_FIXTURES))
    try:
        return _run(data_dir, label, held_out, _config_model_factory(gateway), "replay")
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
    return _run(data_dir, label, held_out, lambda _e, _b: scripted_model(label, source, variant), "fake (no fixture)")


def _run(
    data_dir: Path,
    label: FailureLabel,
    held_out: bool,
    model_factory: Callable[[str, InvestigationBudget], ModelClient],
    model_name: str,
) -> ScenarioResult:
    with tempfile.TemporaryDirectory(prefix="driftgate-e2e-") as tmp:
        kill = Path(tmp) / "kill_switch.json"
        kill.write_text(json.dumps(_KILL_SWITCH_ON), encoding="utf-8")
        orch: Orchestrator = build_synthetic_orchestrator(
            data_dir,
            model_factory,
            audit_path=Path(tmp) / "audit.jsonl",
            clock=StepClock(),
            kill_switch_path=kill,
        )
        outcome = orch.handle(label.execution_id)
        audit = orch.audit.read()
    status, note = judge(label, outcome)
    row = Row(
        label.scenario_id or "",
        held_out,
        label.execution_id,
        label.fault_id,
        expected_text(label),
        actual_text(outcome),
        status,
        "none" if outcome.kind == CLOSED else model_name,
        outcome.model_calls,
        outcome.tool_calls,
        outcome.tokens,
        note,
    )
    return ScenarioResult(row, outcome, audit)


def run_all(
    data_dir: Path, *, mode: str = "fake", fixtures_dir: Path | None = None, truth: GroundTruth | None = None
) -> list[ScenarioResult]:
    truth = truth or load_ground_truth(data_dir)
    results = []
    for held_out in (False, True):
        for label in truth.scenarios(held_out=held_out):
            results.append(run_scenario(data_dir, label, held_out, mode=mode, fixtures_dir=fixtures_dir))
    return results


def format_table(rows: list[Row], mode: str) -> str:
    models = sorted({r.model for r in rows})
    header = f"End-to-end scenarios (mode={mode}; model source: {', '.join(models)})"
    if any(m.startswith("fake") for m in models):
        header += "\nThe scripted fake model is a test double: it shows the pipeline works, not how a real model does."
    cols = ("scenario", "set", "fault", "expected", "actual", "status", "source", "model calls", "tool calls", "tokens")
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
        )
        for r in rows
    ]
    widths = [max(len(c), *(len(b[i]) for b in body)) for i, c in enumerate(cols)]
    lines = [header, "", "  ".join(c.ljust(w) for c, w in zip(cols, widths, strict=True))]
    lines.append("  ".join("-" * w for w in widths))
    lines += ["  ".join(v.ljust(w) for v, w in zip(b, widths, strict=True)) for b in body]
    counts = {s: sum(r.status == s for r in rows) for s in (OK, CONFLICT, MISMATCH, NO_FIXTURE)}
    lines += [
        "",
        f"{len(rows)} scenarios: {counts[OK]} ok, {counts[CONFLICT]} known label conflict, "
        f"{counts[MISMATCH]} mismatch, {counts[NO_FIXTURE]} without fixture",
        f"model calls {sum(r.model_calls for r in rows)}, tool calls {sum(r.tool_calls for r in rows)}, "
        f"tokens {sum(r.tokens for r in rows)}",
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
    results = run_all(args.data, mode=args.mode, fixtures_dir=args.fixtures)
    rows = [r.row for r in results]
    print(format_table(rows, args.mode))
    return 0 if all(r.status in (OK, CONFLICT) for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
