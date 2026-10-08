"""Scores the deterministic baseline against ground truth. Only eval/ may read labels (invariant 8).

`python -m driftgate.eval.baseline_score --data data` prints the table that `python tasks.py baseline` shows.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from driftgate.adapters.synthetic import SyntheticSource
from driftgate.baseline import BaselineResult, run_baseline
from driftgate.eval.ground_truth import FailureLabel, GroundTruth, load_ground_truth
from driftgate.tools import ToolContext


@dataclass(frozen=True)
class Scores:
    n: int
    classification_accuracy: float
    layer_accuracy: float
    decision_accuracy: float  # remediate-or-not agrees with the label
    remediations: int  # cases where the baseline would act
    false_remediations: int  # acted where the label says escalate or close
    false_remediation_rate: float  # false remediations / cases the label says not to remediate
    remediation_recall: float  # share of label-remediate cases the baseline acted on
    tier_agreement: float  # effective tier (None = escalate/close) equals the label's
    blast_radius_accuracy: float  # fleet-wide flag and affected count agree
    avg_tool_calls: float


@dataclass
class ScoreReport:
    by_set: dict[str, Scores] = field(default_factory=dict)
    predictions: dict[str, BaselineResult] = field(default_factory=dict)


def truth_tier(label: FailureLabel) -> int | None:
    return label.correct_tier if label.disposition == "remediate" else None


def _ratio(num: int, den: int) -> float:
    return num / den if den else 0.0


def score(labels: Iterable[FailureLabel], predictions: dict[str, BaselineResult]) -> Scores:
    rows = [(lb, predictions[lb.execution_id]) for lb in labels]
    n = len(rows)
    not_remediable = [r for r in rows if r[0].disposition != "remediate"]
    remediable = [r for r in rows if r[0].disposition == "remediate"]
    false_rem = sum(1 for _, p in not_remediable if p.would_remediate)
    return Scores(
        n=n,
        classification_accuracy=_ratio(sum(p.report.classification == lb.true_classification for lb, p in rows), n),
        layer_accuracy=_ratio(sum(p.report.layer == lb.true_layer for lb, p in rows), n),
        decision_accuracy=_ratio(sum(p.would_remediate == (lb.disposition == "remediate") for lb, p in rows), n),
        remediations=sum(p.would_remediate for _, p in rows),
        false_remediations=false_rem,
        false_remediation_rate=_ratio(false_rem, len(not_remediable)),
        remediation_recall=_ratio(sum(p.would_remediate for _, p in remediable), len(remediable)),
        tier_agreement=_ratio(sum(p.tier == truth_tier(lb) for lb, p in rows), n),
        blast_radius_accuracy=_ratio(
            sum(
                (p.report.blast_radius.executions_affected == lb.blast_executions_affected)
                and (p.report.blast_radius.shared_dimension.split(":")[0] == lb.blast_shared_dimension.split(":")[0])
                for lb, p in rows
            ),
            n,
        ),
        avg_tool_calls=_ratio(sum(p.tool_calls for _, p in rows), n) if n else 0.0,
    )


def run(data_dir: Path) -> ScoreReport:
    truth: GroundTruth = load_ground_truth(data_dir)
    ctx = ToolContext(SyntheticSource(data_dir))
    predictions = {eid: run_baseline(ctx, eid) for eid in sorted(truth.failures)}
    everything = list(truth.failures.values())
    report = ScoreReport(predictions=predictions)
    report.by_set["all failures"] = score(everything, predictions)
    report.by_set["scenarios (dev)"] = score(truth.scenarios(held_out=False), predictions)
    report.by_set["scenarios (held out)"] = score(truth.scenarios(held_out=True), predictions)
    return report


_ROWS: tuple[tuple[str, str, str], ...] = (
    ("cases", "n", "d"),
    ("classification accuracy", "classification_accuracy", "%"),
    ("layer accuracy", "layer_accuracy", "%"),
    ("remediate/escalate decision accuracy", "decision_accuracy", "%"),
    ("would remediate (count)", "remediations", "d"),
    ("false remediations (count)", "false_remediations", "d"),
    ("false remediation rate", "false_remediation_rate", "%"),
    ("remediation recall", "remediation_recall", "%"),
    ("tier agreement", "tier_agreement", "%"),
    ("blast radius accuracy", "blast_radius_accuracy", "%"),
    ("avg tool calls", "avg_tool_calls", "f"),
)


def format_table(report: ScoreReport) -> str:
    names = list(report.by_set)
    label_w = max(len(r[0]) for r in _ROWS)
    col_w = max(len(n) for n in names) + 2
    lines = ["Deterministic baseline vs ground truth (no model calls)", ""]
    lines.append("metric".ljust(label_w) + "".join(n.rjust(col_w) for n in names))
    for label, attr, kind in _ROWS:
        cells = []
        for n in names:
            v = getattr(report.by_set[n], attr)
            cells.append((f"{v:.1%}" if kind == "%" else f"{v:.1f}" if kind == "f" else str(v)).rjust(col_w))
        lines.append(label.ljust(label_w) + "".join(cells))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=Path("data"))
    args = ap.parse_args(argv)
    print(format_table(run(args.data)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
