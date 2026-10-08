"""Baseline: valid reports, evidence integrity, deterministic, scores above sanity thresholds."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from driftgate.eval.baseline_score import ScoreReport, format_table, run
from driftgate.eval.ground_truth import load_ground_truth

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def scored(dataset_dir: Path) -> ScoreReport:
    return run(dataset_dir)


def _validator() -> Draft202012Validator:
    remediation = json.loads((ROOT / "contracts" / "remediation.schema.json").read_text(encoding="utf-8"))
    report = json.loads((ROOT / "contracts" / "report.schema.json").read_text(encoding="utf-8"))
    registry = Registry().with_resource("remediation.schema.json", Resource.from_contents(remediation))
    return Draft202012Validator(report, registry=registry)


def test_every_failure_gets_a_schema_valid_report(scored: ScoreReport, dataset_dir: Path) -> None:
    truth = load_ground_truth(dataset_dir)
    assert set(scored.predictions) == set(truth.failures)
    v = _validator()
    for eid, p in scored.predictions.items():
        v.validate(json.loads(json.dumps(p.report.to_dict())))
        assert p.report.execution_id == eid


def test_evidence_cites_tool_calls_of_the_same_run(scored: ScoreReport) -> None:
    """Invariant 7: sources are call ids minted for this execution; the baseline cites only those."""
    for eid, p in scored.predictions.items():
        if p.closed_by_prefilter:
            assert p.report.evidence == []
            continue
        allowed = {f"{eid}:baseline-{i}" for i in range(1, p.tool_calls + 1)}
        assert p.report.evidence and {e.source for e in p.report.evidence} <= allowed
        assert p.report.run.tool_calls == p.tool_calls <= 3


def test_governance_cases_are_closed_by_the_prefilter_with_no_tool_calls(
    scored: ScoreReport, dataset_dir: Path
) -> None:
    truth = load_ground_truth(dataset_dir)
    closed = [e for e, f in truth.failures.items() if f.disposition == "close"]
    assert closed
    for eid in closed:
        p = scored.predictions[eid]
        assert p.closed_by_prefilter and p.tool_calls == 0 and not p.would_remediate


def test_baseline_only_ever_proposes_tier0_and_never_for_fleet_wide(scored: ScoreReport, dataset_dir: Path) -> None:
    truth = load_ground_truth(dataset_dir)
    for eid, p in scored.predictions.items():
        assert p.tier in (None, 0)
        if truth.failures[eid].fleet_wide:
            assert not p.would_remediate and p.report.blast_radius.executions_affected >= 3


def test_baseline_is_deterministic(dataset_dir: Path, scored: ScoreReport) -> None:
    again = run(dataset_dir)
    assert {k: v.report.to_dict() for k, v in again.predictions.items()} == {
        k: v.report.to_dict() for k, v in scored.predictions.items()
    }


def test_scores_clear_sanity_thresholds(scored: ScoreReport) -> None:
    for name, s in scored.by_set.items():
        assert s.n > 0, name
        assert s.layer_accuracy >= 0.9, name
        assert s.classification_accuracy >= 0.85, name
        assert s.tier_agreement >= 0.55, name
        assert s.false_remediation_rate <= 0.2, name
        assert s.blast_radius_accuracy >= 0.95, name
    allf = scored.by_set["all failures"]
    assert allf.n > 250 and allf.remediation_recall >= 0.25 and allf.remediations > 20
    assert scored.by_set["scenarios (held out)"].n >= 8


def test_tier0_remediations_have_a_deterministic_basis(scored: ScoreReport, dataset_dir: Path) -> None:
    """Invariant 3 as the baseline sees it: every Tier 0 comes from a rule or from fail-then-pass precedent."""
    truth = load_ground_truth(dataset_dir)
    for eid, p in scored.predictions.items():
        if p.tier == 0:
            assert p.report.remediation is not None
            assert p.report.remediation.rationale.startswith(("known-transient rule", "flake precedent"))
            assert p.report.remediation.gate == "auto" and p.report.remediation.reversible
        label = truth.failures[eid]
        if label.disposition == "remediate" and label.correct_tier == 0 and p.tier == 0:
            assert label.tier0_path in ("known_transient_rule", "flake_precedent")


def test_table_lists_all_sets_and_metrics(scored: ScoreReport) -> None:
    table = format_table(scored)
    for needle in (
        "all failures",
        "scenarios (held out)",
        "classification accuracy",
        "false remediation rate",
        "tier agreement",
    ):
        assert needle in table


def test_baseline_has_no_model_dependency() -> None:
    for rel in ("baseline.py", "prefilter.py"):
        tree = ast.parse((ROOT / "src" / "driftgate" / rel).read_text(encoding="utf-8"))
        names = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)] + [
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        ]
        assert not [m for m in names if m.startswith("driftgate.llm") or m.split(".")[0] == "anthropic"], rel
