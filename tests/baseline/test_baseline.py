"""Baseline: valid reports, evidence integrity, deterministic, scores above sanity thresholds."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from driftgate.adapters.synthetic import SyntheticSource
from driftgate.baseline import GENERIC_SIGNATURE
from driftgate.eval.baseline_score import ScoreReport, format_table, run
from driftgate.eval.ground_truth import load_ground_truth
from driftgate.tools import ToolContext, dispatch

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
    # Held to invariant 3 the baseline re-runs far less (see docs/design/M3.md): 6 of 303 at seed 20260908.
    assert allf.n > 250 and allf.remediation_recall >= 0.04 and allf.remediations >= 5
    assert scored.by_set["scenarios (held out)"].n >= 8


def _assert_invariant3_basis(dataset_dir: Path, eid: str, p: object) -> None:
    """Re-derive the cited tool results: Tier 0 needs deterministic_match AND (Tier 0 rule OR flake precedent)."""
    ctx = ToolContext(SyntheticSource(dataset_dir))
    report = p.report  # type: ignore[attr-defined]
    by_source = {e.source: e for e in report.evidence}
    sig_call = f"{eid}:baseline-1"
    assert sig_call in by_source, eid
    sig = dispatch(ctx, "classify_signature", {"execution_id": eid}, sig_call)
    assert sig.ok and sig.data["deterministic_match"] is True, (eid, sig.data.get("signature"))
    assert sig.data["signature"] != GENERIC_SIGNATURE, eid
    assert by_source[sig_call].finding.startswith(f"signature {sig.data['signature']} "), eid
    if sig.data["tier0_rule_exists"]:
        assert report.remediation.rationale.startswith("known-transient rule"), eid
    else:
        flake_sources = [e for e in report.evidence if e.source != sig_call and "precedent" in e.finding]
        assert flake_sources and flake_sources[0].finding.endswith("True"), eid
        flake = dispatch(ctx, "flake_history", {"execution_id": eid}, flake_sources[0].source)
        assert flake.ok and flake.data["precedent"] is True, eid
        assert report.remediation.rationale.startswith("flake precedent"), eid


def test_bare_exit_code_never_gets_a_tier0(scored: ScoreReport, dataset_dir: Path) -> None:
    """Invariant 3: exit_code_nonzero is not a deterministic match, so it always escalates."""
    ctx = ToolContext(SyntheticSource(dataset_dir))
    seen = 0
    for eid, p in scored.predictions.items():
        if p.closed_by_prefilter:
            continue
        sig = dispatch(ctx, "classify_signature", {"execution_id": eid}, f"{eid}:check")
        if sig.data.get("signature") == GENERIC_SIGNATURE:
            seen += 1
            assert not p.would_remediate, eid
    assert seen > 0


def test_tier0_remediations_have_a_deterministic_basis(scored: ScoreReport, dataset_dir: Path) -> None:
    """Invariant 3 as the baseline sees it: every Tier 0 comes from a rule or from fail-then-pass precedent."""
    truth = load_ground_truth(dataset_dir)
    for eid, p in scored.predictions.items():
        if p.tier == 0:
            assert p.report.remediation is not None
            assert p.report.remediation.rationale.startswith(("known-transient rule", "flake precedent"))
            assert p.report.remediation.gate == "auto" and p.report.remediation.reversible
        if p.tier == 0:
            _assert_invariant3_basis(dataset_dir, eid, p)
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
