from datetime import timedelta

import pytest

from drift_gate.dataset_io import save_population
from drift_gate.domain import ExecutionStatus, NodeStatus
from drift_gate.generator.population import build_population
from drift_gate.protocols import ExecutionSource, RemediationTarget
from drift_gate.synthetic.source import SyntheticSource
from drift_gate.synthetic.target import SyntheticTarget


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("synthetic_source")
    population = build_population(seed=7, out_dir=out_dir)
    save_population(population, out_dir)
    return out_dir


@pytest.fixture(scope="module")
def source(data_dir):
    return SyntheticSource(data_dir)


@pytest.fixture(scope="module")
def target(data_dir):
    return SyntheticTarget(data_dir)


def test_satisfies_execution_source_protocol(source):
    assert isinstance(source, ExecutionSource)


def test_satisfies_remediation_target_protocol(target):
    assert isinstance(target, RemediationTarget)


def test_get_execution_roundtrips(source):
    any_id = next(iter(source._executions))
    execution = source.get_execution(any_id)
    assert execution.id == any_id


def test_get_failed_leaf_nodes_only_for_failed_executions(source):
    for execution in source._executions.values():
        leaves = source.get_failed_leaf_nodes(execution.id)
        if execution.status == ExecutionStatus.FAILED:
            assert len(leaves) == 1
            assert leaves[0].status == NodeStatus.FAILED
        else:
            assert leaves == []


def test_get_step_logs_respects_budget(source):
    failing = next(
        e for e in source._executions.values() if e.status == ExecutionStatus.FAILED
    )
    node = source.get_failed_leaf_nodes(failing.id)[0]
    chunk = source.get_step_logs(failing.id, node.id, budget=20)
    assert len(chunk.text) <= 20
    assert chunk.truncated is True


def test_list_executions_filters_by_window(source):
    all_execs = list(source._executions.values())
    all_execs.sort(key=lambda e: e.started_at)
    mid = all_execs[len(all_execs) // 2].started_at
    window = (mid, mid + timedelta(days=1))
    summaries = source.list_executions(window)
    assert all(mid <= s.started_at <= mid + timedelta(days=1) for s in summaries)
    assert len(summaries) < len(all_execs)


def test_execute_matching_ground_truth_resolves_execution(data_dir, target):
    from drift_gate.dataset_io import load_ground_truth

    gt_with_remediation = next(
        g for g in load_ground_truth(data_dir) if g["correct_remediation"] is not None
    )
    from drift_gate.domain import Gate, Remediation, Tier

    action = Remediation(
        tier=Tier(gt_with_remediation["correct_remediation"]["tier"]),
        action=gt_with_remediation["correct_remediation"]["action"],
        rationale="test", reversible=True, gate=Gate.AUTO,
        context={"execution_id": gt_with_remediation["execution_id"]},
    )
    result = target.execute(action)
    assert result.succeeded is True
    verification = target.verify(action)
    assert verification.resolved is True


def test_execute_wrong_action_does_not_resolve(data_dir, target):
    from drift_gate.dataset_io import load_ground_truth

    gt_with_remediation = next(
        g for g in load_ground_truth(data_dir) if g["correct_remediation"] is not None
    )
    from drift_gate.domain import Gate, Remediation, Tier

    wrong_action = "definitely_not_the_right_action"
    assert wrong_action != gt_with_remediation["correct_remediation"]["action"]
    action = Remediation(
        tier=Tier(gt_with_remediation["correct_remediation"]["tier"]),
        action=wrong_action,
        rationale="test", reversible=True, gate=Gate.AUTO,
        context={"execution_id": gt_with_remediation["execution_id"]},
    )
    result = target.execute(action)
    assert result.succeeded is False
