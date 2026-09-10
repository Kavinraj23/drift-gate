"""Phase 1 checkpoint: measures the deterministic classifier's accuracy against the
full synthetic ground truth (TASKS.md Phase 1 "Baseline accuracy measured").

Runs classify() over every execution in ground_truth.json - never the other
direction - and reports:
  - what fraction the deterministic layer resolves outright (zero LLM calls) vs
    correctly declines and hands to the agent
  - among resolved (Tier 0, auto-executed) cases: classification/tier/action
    correctness against ground truth - this is the number that matters most, since
    a wrong "resolved" verdict here means an unsupervised action would have been wrong
  - flake-detection accuracy
  - classification accuracy on the cases where the classifier ventures a guess at
    all (matched signature, even if tier > 0 and it correctly escalates for content)

Usage: python -m scripts.measure_baseline
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from drift_gate.classifier import classify
from drift_gate.dataset_io import load_ground_truth
from drift_gate.domain import Classification
from drift_gate.synthetic.source import SyntheticSource

DATA_DIR = Path("data/synthetic")


def main() -> None:
    source = SyntheticSource(DATA_DIR)
    gts = {g["execution_id"]: g for g in load_ground_truth(DATA_DIR)}

    n = len(gts)
    resolved_total = 0
    resolved_action_correct = 0
    resolved_tier_correct = 0
    resolved_wrong: list[dict] = []

    guessed_total = 0
    guessed_classification_correct = 0

    flake_total_gt = 0
    flake_detected_correct = 0
    flake_false_positive = 0

    escalated_total = 0
    per_fault_id: Counter[str] = Counter()
    per_fault_id_correct: Counter[str] = Counter()

    for exec_id, gt in gts.items():
        det = classify(source, exec_id)
        gt_class = Classification(gt["correct_classification"])
        gt_remediation = gt["correct_remediation"]
        fault_id = gt["injected_fault_id"]

        per_fault_id[fault_id] += 1

        if det.classification is not None:
            guessed_total += 1
            if det.classification == gt_class:
                guessed_classification_correct += 1
                per_fault_id_correct[fault_id] += 1

        if gt["is_flake"]:
            flake_total_gt += 1
            if det.is_flake:
                flake_detected_correct += 1
        elif det.is_flake:
            flake_false_positive += 1

        if det.resolved:
            resolved_total += 1
            action_ok = bool(gt_remediation) and det.rule is not None and \
                gt_remediation["action"] == det.rule.action
            tier_ok = bool(gt_remediation) and det.rule is not None and \
                gt_remediation["tier"] == det.rule.tier.value
            if action_ok:
                resolved_action_correct += 1
            if tier_ok:
                resolved_tier_correct += 1
            if not (action_ok and tier_ok):
                resolved_wrong.append({
                    "execution_id": exec_id, "fault_id": fault_id,
                    "predicted_action": det.rule.action if det.rule else None,
                    "gt_remediation": gt_remediation,
                })
        else:
            escalated_total += 1

    report = {
        "total_executions": n,
        "resolved_deterministically": resolved_total,
        "resolved_pct": round(100 * resolved_total / n, 1),
        "resolved_action_correct": resolved_action_correct,
        "resolved_tier_correct": resolved_tier_correct,
        "resolved_wrong_count": len(resolved_wrong),
        "resolved_wrong_cases": resolved_wrong,
        "escalated_to_agent": escalated_total,
        "escalated_pct": round(100 * escalated_total / n, 1),
        "classification_guess_made": guessed_total,
        "classification_guess_accuracy": (
            round(100 * guessed_classification_correct / guessed_total, 1)
            if guessed_total else None
        ),
        "flake_ground_truth_count": flake_total_gt,
        "flake_detected_correct": flake_detected_correct,
        "flake_recall_pct": (
            round(100 * flake_detected_correct / flake_total_gt, 1)
            if flake_total_gt else None
        ),
        "flake_false_positives": flake_false_positive,
        "per_fault_id_classification_accuracy": {
            fid: {
                "n": per_fault_id[fid],
                "classification_correct": per_fault_id_correct.get(fid, 0),
            }
            for fid in sorted(per_fault_id)
        },
    }

    print(json.dumps(report, indent=2))

    out_path = DATA_DIR / "baseline_report.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nWritten to {out_path}")

    if resolved_wrong:
        print(
            f"\n*** {len(resolved_wrong)} case(s) resolved deterministically with a "
            "WRONG action/tier - these would have auto-executed incorrectly. ***"
        )


if __name__ == "__main__":
    main()
