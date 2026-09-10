from pathlib import Path

import pytest

from drift_gate.classifier import classify, to_report
from drift_gate.dataset_io import load_ground_truth, save_population
from drift_gate.domain import Classification, Tier
from drift_gate.generator.population import build_population
from drift_gate.synthetic.source import SyntheticSource


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("classifier")
    population = build_population(seed=7, out_dir=out_dir)
    save_population(population, out_dir)
    return out_dir


@pytest.fixture(scope="module")
def source(data_dir):
    return SyntheticSource(data_dir)


def _exec_id_for_fault(data_dir: Path, fault_id: str) -> str:
    gts = load_ground_truth(data_dir)
    return next(g["execution_id"] for g in gts if g["injected_fault_id"] == fault_id)


def test_clean_tier0_signature_resolves_deterministically(data_dir, source):
    exec_id = _exec_id_for_fault(data_dir, "registry_429")
    result = classify(source, exec_id)
    assert result.resolved is True
    assert result.rule.tier == Tier.TIER_0
    assert result.matched_fault_id == "registry_429"


def test_tier3_signature_matches_but_needs_agent(data_dir, source):
    exec_id = _exec_id_for_fault(data_dir, "lockfile_mismatch")
    result = classify(source, exec_id)
    assert result.matched_fault_id == "lockfile_mismatch"
    assert result.rule.tier == Tier.TIER_3
    assert result.resolved is False
    assert result.escalate_reason is not None


def test_unmapped_signature_is_residual(data_dir, source):
    # assume_role_denied (not oom_killed): oom_killed only occurs on the two flaky
    # pipelines, where an unrelated later success can coincidentally land inside the
    # flake-check window and make this test seed-dependent.
    exec_id = _exec_id_for_fault(data_dir, "assume_role_denied")
    result = classify(source, exec_id)
    assert result.matched_fault_id == "assume_role_denied"
    assert result.rule is None
    assert result.resolved is False


def test_flaky_pipeline_retry_resolves_as_transient(data_dir, source):
    gts = load_ground_truth(data_dir)
    flake_gt = next(g for g in gts if g["is_flake"])
    result = classify(source, flake_gt["execution_id"])
    assert result.is_flake is True
    assert result.resolved is True
    assert result.classification == Classification.TRANSIENT
    # A flake resolving deterministically must still carry a Tier 0 retry rule -
    # to_report() has no other way to attach a Remediation to the output contract.
    assert result.rule is not None
    assert result.rule.tier == Tier.TIER_0
    report = to_report(result)
    assert report.remediation is not None
    assert report.remediation.action == "retry_execution"
    assert report.abstained is False


def test_policy_denial_escalates_without_crashing(data_dir, source):
    exec_id = _exec_id_for_fault(data_dir, "policy_denial")
    result = classify(source, exec_id)
    assert result.matched_fault_id == "policy_denial"
    assert result.classification == Classification.GOVERNANCE
    assert result.resolved is False
    assert result.escalate_reason is not None


def test_fleet_correlation_flips_burst_execution_to_platform(data_dir, source):
    gts = load_ground_truth(data_dir)
    burst_gt = next(g for g in gts if g["is_duplicate_of"] is not None)
    result = classify(source, burst_gt["execution_id"])
    assert result.blast_radius.executions_affected > 1
    assert result.classification == Classification.PLATFORM


def test_to_report_emits_output_contract_for_resolved_case(data_dir, source):
    exec_id = _exec_id_for_fault(data_dir, "registry_429")
    result = classify(source, exec_id)
    report = to_report(result)
    assert report.execution_id == exec_id
    assert report.remediation is not None
    assert report.remediation.action == "retry_execution"
    assert report.abstained is False
