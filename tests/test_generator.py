from pathlib import Path

import pytest

from drift_gate.domain import ExecutionStatus, NodeStatus
from drift_gate.errors import BY_FAULT_ID as ERROR_BY_FAULT_ID
from drift_gate.generator.pipelines import FLAKY_IDS
from drift_gate.generator.population import build_population


@pytest.fixture(scope="module")
def population(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("synthetic")
    return build_population(seed=42, out_dir=out_dir)


def test_success_rate_within_tolerance(population):
    n = len(population.executions)
    n_success = sum(1 for e in population.executions if e.status == ExecutionStatus.SUCCESS)
    rate = n_success / n
    assert 0.70 <= rate <= 0.90


def test_failure_classification_split_within_tolerance(population):
    from collections import Counter

    non_governance = [g for g in population.ground_truth if g.correct_classification.value != "governance"]
    counts = Counter(g.correct_classification.value for g in non_governance)
    total = sum(counts.values())
    assert total > 50
    assert 0.45 <= counts["user"] / total <= 0.75
    assert 0.10 <= counts["platform"] / total <= 0.40
    assert 0.05 <= counts["transient"] / total <= 0.30


def test_every_failure_has_ground_truth(population):
    failed_ids = {e.id for e in population.executions if e.status == ExecutionStatus.FAILED}
    gt_ids = {g.execution_id for g in population.ground_truth}
    assert failed_ids == gt_ids


def test_at_least_three_bursts_detectable(population):
    dup_groups: dict[str, list] = {}
    for g in population.ground_truth:
        if g.is_duplicate_of is not None:
            dup_groups.setdefault(g.is_duplicate_of, []).append(g)
    assert len(dup_groups) >= 3
    executions_by_id = {e.id: e for e in population.executions}
    for primary_id, members in dup_groups.items():
        primary = executions_by_id[primary_id]
        for member in members:
            m_exec = executions_by_id[member.execution_id]
            delta = abs((m_exec.started_at - primary.started_at).total_seconds())
            assert delta <= 20 * 60


def test_at_least_two_pipelines_show_fail_then_pass(population):
    flaky_pipelines_with_flake = set()
    executions_by_id = {e.id: e for e in population.executions}
    for g in population.ground_truth:
        if g.is_flake:
            flaky_pipelines_with_flake.add(executions_by_id[g.execution_id].pipeline_id)
    assert len(flaky_pipelines_with_flake) >= 2
    assert flaky_pipelines_with_flake.issubset(set(FLAKY_IDS))


def test_fixture_repo_has_backdated_commits(population):
    repo = population.fixture_repo_path
    assert (repo / ".git").exists()
    assert len(population.events) > 0
    for event in population.events:
        assert event.commit_sha is not None


def test_error_strings_are_exact_and_multiline(population):
    lock_sig = ERROR_BY_FAULT_ID["state_lock_stale_holder"]
    assert "Error acquiring the state lock" in lock_sig.text
    assert lock_sig.text.count("\n") > 3
    lockfile_sig = ERROR_BY_FAULT_ID["lockfile_mismatch"]
    assert "Error: Inconsistent dependency lock file" in lockfile_sig.text


def test_failed_executions_have_exactly_one_failed_node(population):
    for e in population.executions:
        failed = [n for n in e.nodes if n.status == NodeStatus.FAILED]
        if e.status == ExecutionStatus.FAILED:
            assert len(failed) == 1
        else:
            assert len(failed) == 0
