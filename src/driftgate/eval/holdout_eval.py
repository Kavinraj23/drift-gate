"""Offline evaluation on the stored real-log holdout, and the CLI behind `holdout-validate` / `eval-holdout`.

`python -m driftgate.eval.holdout_eval validate|eval [--dir holdout]`

Never needs the API key and never touches the network. Two systems are scored against the human labels:
- baseline: the deterministic signature classifier run on the stored log text (`tools.signatures`). It has no fleet or
  flake history for a lone real log, so it can only re-run on a signature with an authored Tier 0 rule (invariant 3),
  and it cannot author a fix, so it escalates everything else.
- agent: read from run records in `holdout/runs.json` (the human-run record/replay path writes them). A case with no
  run record is simply not in the agent column; the agent is never invented offline.

Dev cases get a per-case table. Test cases contribute to aggregate metrics only. Counts sit beside percentages and
this report is a separate real-data column: it is never merged with the synthetic tables.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from driftgate.eval.holdout import (
    DEFAULT_DIR,
    Holdout,
    HoldoutCase,
    HoldoutInvalid,
    RunRecord,
    ValidationReport,
    validate,
)
from driftgate.eval.report import SMALL_N, CaseResult, Metrics, compute_metrics, fmt
from driftgate.tools.signatures import GENERIC_EXIT_ID, UNKNOWN, match_all

RERUN = "rerun_failed_job"
Clock = Callable[[], datetime]


def _correct_action(case: HoldoutCase) -> str | None:
    return {"rerun": RERUN, "pr_fix": case.fix}.get(case.action_kind)


def _result(case: HoldoutCase, *, acted: bool, action: str, layer: str, classification: str) -> CaseResult:
    return CaseResult(
        scenario_id=case.id,
        held_out=case.split == "test",
        disposition="escalate" if case.escalation else "remediate",
        correct_action=_correct_action(case),
        true_layer=case.layer,
        true_classification=case.classification,
        acted=acted,
        action=action if acted else "",
        layer=layer,
        classification=classification,
        verified=None,
        input_tokens=0,
        output_tokens=0,
        model_calls=0,
        tool_calls=0,
        tools=(),
    )


def baseline_on_log(case: HoldoutCase, log: str) -> CaseResult:
    hits = match_all(log)
    sig = hits[0] if hits else UNKNOWN
    deterministic = sig.id not in (GENERIC_EXIT_ID, UNKNOWN.id)
    acted = deterministic and sig.tier0_rule
    return _result(
        case,
        acted=acted,
        action=RERUN,
        layer=sig.layer,
        classification="transient" if acted else sig.classification,
    )


def agent_from_run(case: HoldoutCase, run: RunRecord) -> CaseResult:
    acted = run.action_kind != "escalate"
    action = RERUN if run.action_kind == "rerun" else (run.fix or "")
    return _result(case, acted=acted, action=action, layer=run.layer, classification=run.classification)


@dataclass(frozen=True)
class Scored:
    baseline: CaseResult
    agent: CaseResult | None


def score_case(case: HoldoutCase, log: str, run: RunRecord | None) -> Scored:
    return Scored(baseline_on_log(case, log), agent_from_run(case, run) if run else None)


@dataclass(frozen=True)
class SplitMetrics:
    n: int
    baseline: Metrics
    agent: Metrics | None  # None: the agent has no run records for this split
    agent_runs: int


def aggregate(scores: list[Scored]) -> SplitMetrics:
    agent = [s.agent for s in scores if s.agent is not None]
    return SplitMetrics(
        n=len(scores),
        baseline=compute_metrics([s.baseline for s in scores], has_verification=False),
        agent=compute_metrics(agent, has_verification=False) if agent else None,
        agent_runs=len(agent),
    )


_ROWS: tuple[tuple[str, str], ...] = (
    ("False remediation rate (all cases; lower is better)", "false_remediation"),
    ("  of which: acted where the label says escalate", "false_on_escalate_close"),
    ("  of which: acted with a different action than the label", "false_wrong_action"),
    ("Remediation recall (label-remediate cases fixed correctly)", "remediation_recall"),
    ("Escalation precision", "escalation_precision"),
    ("Classification accuracy", "classification_accuracy"),
    ("Layer accuracy", "layer_accuracy"),
)

HONESTY = (
    "**Real-data column, separate from the synthetic tables.** Stored logs from public GitHub Actions runs, labelled "
    "by a human before any run. Counts sit beside every percentage; with n this small a difference of one or two "
    "cases is not an improvement. Dev cases may be inspected; the test split is sealed and appears only as aggregates. "
    "The baseline sees one log and no fleet or flake history, so it is not the same pipeline as the synthetic "
    "baseline column. The agent column comes only from recorded run records, never from this command."
)


def _md_table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    return "\n".join(out + ["| " + " | ".join(r) + " |" for r in rows])


def _cell(m: Metrics | None, attr: str) -> str:
    return "not run" if m is None else fmt(getattr(m, attr))


def render(
    dev: list[tuple[HoldoutCase, Scored]], dev_m: SplitMetrics, test_m: SplitMetrics, counts: dict[str, int]
) -> str:
    header = ["metric", "baseline dev", "agent dev", "baseline test", "agent test"]
    rows = [
        [label, _cell(dev_m.baseline, a), _cell(dev_m.agent, a), _cell(test_m.baseline, a), _cell(test_m.agent, a)]
        for label, a in _ROWS
    ]
    rows.insert(
        0,
        [
            "cases (n)",
            str(dev_m.n),
            f"{dev_m.agent_runs}/{dev_m.n} run",
            str(test_m.n),
            f"{test_m.agent_runs}/{test_m.n} run",
        ],
    )
    out = [
        "# Real-log holdout (offline)",
        "",
        HONESTY,
        "",
        f"Stored: {counts['total']} cases ({counts['dev']} dev, {counts['test']} test). `*` marks n < {SMALL_N}.",
        "",
        _md_table(header, rows),
        "",
        "## Dev cases (per-case detail allowed)",
        "",
    ]
    detail = []
    for case, s in dev:
        agent = "not run" if s.agent is None else (s.agent.action or "escalate")
        detail.append(
            [
                case.id,
                f"{case.layer}/{case.classification}",
                case.fix or case.action_kind,
                f"{s.baseline.layer}/{s.baseline.classification}",
                s.baseline.action or "escalate",
                agent,
            ]
        )
    out.append(
        _md_table(
            ["case", "label layer/class", "label action", "baseline layer/class", "baseline action", "agent action"],
            detail,
        )
        if detail
        else "No dev cases stored."
    )
    out += [
        "",
        "## Test split",
        "",
        "Sealed: aggregate metrics only (the table above). No per-case output exists for it.",
    ]
    return "\n".join(out) + "\n"


def evaluate(root: Path, now: datetime) -> tuple[str | None, ValidationReport]:
    """The rendered report, or None when there is nothing to evaluate."""
    holdout = Holdout.load(root, now=now)
    counts = holdout.counts()
    if counts["total"] == 0:
        return None, holdout.report
    dev = holdout.dev_details(score_case)
    dev_m = aggregate([s for _, s in dev])
    test_m = holdout.test_aggregate(score_case, aggregate)
    return render(dev, dev_m, test_m, counts), holdout.report


def _validate_cmd(root: Path, now: datetime) -> int:
    report = validate(root, now=now)
    c = report.counts
    print(
        f"holdout: {c['total']} cases ({c['dev']} dev, {c['test']} test); layers "
        + ", ".join(f"{k} {c[k]}" for k in ("L1", "L2", "L3", "L4"))
        + f"; escalations {c['escalations']}"
    )
    for p in report.problems:
        print(f"INVALID: {p}")
    for q in report.unmet_quotas:
        print(f"UNMET QUOTA: {q}")
    print("holdout-validate: " + ("ok" if report.ok else "FAILED"))
    return 0 if report.ok else 1


def _eval_cmd(root: Path, now: datetime) -> int:
    try:
        text, report = evaluate(root, now)
    except HoldoutInvalid as e:
        print("eval-holdout: refusing to evaluate an invalid holdout")
        for p in e.problems:
            print(f"INVALID: {p}")
        return 1
    if text is None:
        print("eval-holdout: no holdout cases are stored (holdout/labels.json has 0 cases). Nothing was evaluated and")
        print("no numbers are shown. Choose and label the real runs first; see holdout/README.md.")
        for q in report.unmet_quotas:
            print(f"UNMET QUOTA: {q}")
        return 1
    print(text)
    for q in report.unmet_quotas:
        print(f"UNMET QUOTA: {q} (results above are partial)")
    return 0


def main(argv: list[str] | None = None, *, clock: Clock | None = None) -> int:
    parser = argparse.ArgumentParser(prog="driftgate.eval.holdout_eval")
    parser.add_argument("command", choices=["validate", "eval"])
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    args = parser.parse_args(argv)
    now = (clock or (lambda: datetime.now(UTC)))()
    return _validate_cmd(args.dir, now) if args.command == "validate" else _eval_cmd(args.dir, now)


if __name__ == "__main__":
    sys.exit(main())
