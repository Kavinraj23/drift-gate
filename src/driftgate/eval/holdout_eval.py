"""Offline evaluation on the stored real-log holdout, and the CLI behind `holdout-validate` / `eval-holdout`.

`python -m driftgate.eval.holdout_eval validate|eval [--dir holdout]`

Never needs the API key and never touches the network. Two systems are scored against the human labels:
- baseline: the deterministic signature classifier run on the stored log text (`tools.signatures`). It has no fleet or
  flake history for a lone real log, so it can only re-run on a signature with an authored Tier 0 rule (invariant 3),
  and it cannot author a fix, so it escalates everything else.
- agent: read from run records in `holdout/runs.json` (the human-run record/replay path writes them). A case with no
  run record is simply not in the agent column; the agent is never invented offline. The agent column is built ONLY if
  every latest run record has a matching real-model provenance entry in `holdout/runs_meta.json` (see `holdout_meta`);
  a fake/scripted record, a record with no entry, or one that does not match its entry makes the command refuse
  (exit 1, counts only). The report header states the provenance.
- a second, clearly labelled block shows the agent's proposal BEFORE the gates (ungated; not the headline).

Dev cases get a per-case table. Test cases contribute to aggregate metrics only. Counts sit beside percentages and
this report is a separate real-data column: it is never merged with the synthetic tables.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from driftgate.eval.holdout import (
    DEFAULT_DIR,
    Holdout,
    HoldoutCase,
    HoldoutInvalid,
    RunRecord,
    ValidationReport,
    validate,
)
from driftgate.eval.holdout_meta import Provenance, check_provenance, load_meta
from driftgate.eval.report import SMALL_N, CaseResult, Metrics, compute_metrics, fmt
from driftgate.tools.signatures import GENERIC_EXIT_ID, UNKNOWN, match_all

RERUN = "rerun_failed_job"
Clock = Callable[[], datetime]
GitStatus = Callable[[Path], str | None]


def git_labels_status(root: Path) -> str | None:
    """`git status --porcelain` for labels.json (no network). None when git or the repository is unavailable."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--", "labels.json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


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
    proposal: dict[str, Any] | None = None  # the agent's ungated proposal (sidecar), if it has one


def score_case(case: HoldoutCase, log: str, run: RunRecord | None, meta: dict[str, Any] | None = None) -> Scored:
    proposal = (meta or {}).get("proposal_before_gates") if run else None
    return Scored(
        baseline_on_log(case, log),
        agent_from_run(case, run) if run else None,
        proposal if isinstance(proposal, dict) else None,
    )


@dataclass(frozen=True)
class SplitMetrics:
    n: int
    baseline: Metrics
    agent: Metrics | None  # None: the agent has no run records for this split
    agent_runs: int
    proposals: Counter[str] = field(default_factory=Counter)  # ungated proposal kind -> count
    proposals_seen: int = 0  # cases that have a recorded ungated proposal
    ceiling_only: int = 0  # proposals refused by the abort ceiling and nothing else


def aggregate(scores: list[Scored]) -> SplitMetrics:
    agent = [s.agent for s in scores if s.agent is not None]
    return SplitMetrics(
        n=len(scores),
        baseline=compute_metrics([s.baseline for s in scores], has_verification=False),
        agent=compute_metrics(agent, has_verification=False) if agent else None,
        agent_runs=len(agent),
        proposals=Counter(str(s.proposal.get("kind")) for s in scores if s.proposal is not None),
        proposals_seen=sum(1 for s in scores if s.proposal is not None),
        ceiling_only=sum(1 for s in scores if s.proposal and s.proposal.get("refused_by_abort_ceiling_only")),
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


def _proposal_block(dev: list[tuple[HoldoutCase, Scored]], dev_m: SplitMetrics, test_m: SplitMetrics) -> list[str]:
    """Counts of what the agent PROPOSED before the gates. Clearly not the headline; per case for dev only."""

    def row(name: str, m: SplitMetrics) -> list[str]:
        c = m.proposals
        return [name, str(m.proposals_seen), str(c["rerun"]), str(c["pr_fix"]), str(c["escalate"]), str(m.ceiling_only)]

    out = [
        "## Proposal before gates (ungated; NOT the headline)",
        "",
        "What the agent proposed before SafetyGate saw it. A lone stored job is 100% of its resource set, so the abort "
        "ceiling can refuse a Tier 0 re-run that the agent proposed correctly: `refused by the abort ceiling only` "
        "counts those. This block is for understanding the agent; the table above (action after the gates) is the "
        "result. Aggregate only for the test split.",
        "",
        _md_table(
            ["split", "cases", "rerun", "pr_fix", "escalate", "refused by the abort ceiling only"],
            [row("dev", dev_m), row("test", test_m)],
        ),
        "",
    ]
    lines = []
    for case, s in dev:
        if s.proposal is None:
            continue
        p = s.proposal
        fix = f" ({p['fix']})" if p.get("fix") else ""
        lines.append(
            [
                case.id,
                f"{p.get('kind')}{fix}",
                str(p.get("gate_decision")),
                str(p.get("gate_reason_code")),
                "yes" if p.get("refused_by_abort_ceiling_only") else "no",
            ]
        )
    if lines:
        out += [
            "Dev cases:",
            "",
            _md_table(["case", "proposed", "gate decision", "gate reason code", "ceiling only"], lines),
            "",
        ]
    return out


def render(
    dev: list[tuple[HoldoutCase, Scored]],
    dev_m: SplitMetrics,
    test_m: SplitMetrics,
    counts: dict[str, int],
    provenance: Provenance | None = None,
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
        provenance.header() if provenance and provenance.records else "Agent provenance: no run records.",
        "",
        "The headline agent column is the action AFTER the gates (the production gate on the production inputs).",
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
        "",
        *_proposal_block(dev, dev_m, test_m),
    ]
    return "\n".join(out) + "\n"


class ProvenanceRefused(Exception):
    """The agent column will not be built: some run record is not a recorded real-model run. Counts only."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def evaluate(root: Path, now: datetime) -> tuple[str | None, ValidationReport]:
    """The rendered report, or None when there is nothing to evaluate. Raises `ProvenanceRefused` when any latest run
    record is a fake/scripted run, has no provenance entry, or does not match its entry."""
    holdout = Holdout.load(root, now=now)
    counts = holdout.counts()
    if counts["total"] == 0:
        return None, holdout.report
    provenance = check_provenance(root)
    if not provenance.ok:
        raise ProvenanceRefused(provenance.problems())
    meta = load_meta(root)

    def score(case: HoldoutCase, log: str, run: RunRecord | None) -> Scored:
        return score_case(case, log, run, meta.get(case.id))

    dev = holdout.dev_details(score)
    dev_m = aggregate([s for _, s in dev])
    test_m = holdout.test_aggregate(score, aggregate)
    return render(dev, dev_m, test_m, counts, provenance), holdout.report


def _validate_cmd(root: Path, now: datetime, git_status: GitStatus) -> int:
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
    if c["runs"] and (git_status(root) or "").strip():
        print(
            "WARNING: labels.json has uncommitted changes while runs.json is non-empty. Labels must be committed "
            "before the runs that used them; git history is the real guarantee of that."
        )
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
    except ProvenanceRefused as e:
        print("eval-holdout: REFUSING to build the agent column: run records without a real-model provenance.")
        for p in e.problems:
            print(f"  {p}")
        print("Only records from `record-holdout` (or its replay) with a matching runs_meta.json entry are used.")
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


def main(argv: list[str] | None = None, *, clock: Clock | None = None, git_status: GitStatus | None = None) -> int:
    parser = argparse.ArgumentParser(prog="driftgate.eval.holdout_eval")
    parser.add_argument("command", choices=["validate", "eval"])
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    args = parser.parse_args(argv)
    now = (clock or (lambda: datetime.now(UTC)))()
    return (
        _validate_cmd(args.dir, now, git_status or git_labels_status)
        if args.command == "validate"
        else _eval_cmd(args.dir, now)
    )


if __name__ == "__main__":
    sys.exit(main())
