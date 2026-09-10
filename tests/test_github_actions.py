"""Unit tests for GitHubActionsSource/Target against a fake client - no network calls,
no GITHUB_TOKEN needed. Fixtures below are trimmed but verbatim-shaped copies of real
API responses captured while manually running .github/workflows/demo-failure.yml
against the real GitHub Actions API during development.
"""
from __future__ import annotations

from drift_gate.domain import ExecutionStatus, NodeStatus, Remediation, Tier, Gate
from drift_gate.github_actions.source import GitHubActionsSource
from drift_gate.github_actions.target import GitHubActionsTarget

RUN = {
    "id": 34435597583, "name": "demo-failure",
    "path": ".github/workflows/demo-failure.yml",
    "status": "completed", "conclusion": "failure",
    "run_started_at": "2026-09-10T04:02:19Z", "created_at": "2026-09-10T04:02:18Z",
    "updated_at": "2026-09-10T04:02:23Z",
    "head_sha": "f9655741234567890abcdef1234567890abcdef",
    "head_branch": "main", "event": "workflow_dispatch",
}

JOBS = [{
    "id": 102739893085, "name": "apply", "status": "completed", "conclusion": "failure",
    "labels": ["ubuntu-latest"],
    "steps": [
        {"number": 1, "name": "Set up job", "status": "completed",
         "conclusion": "success", "started_at": "2026-09-10T04:02:20Z",
         "completed_at": "2026-09-10T04:02:21Z"},
        {"number": 2, "name": "checkout", "status": "completed",
         "conclusion": "success", "started_at": "2026-09-10T04:02:21Z",
         "completed_at": "2026-09-10T04:02:21Z"},
        {"number": 3, "name": "apply", "status": "completed",
         "conclusion": "failure", "started_at": "2026-09-10T04:02:21Z",
         "completed_at": "2026-09-10T04:02:21Z"},
        {"number": 6, "name": "Post checkout", "status": "completed",
         "conclusion": "success", "started_at": "2026-09-10T04:02:21Z",
         "completed_at": "2026-09-10T04:02:22Z"},
        {"number": 7, "name": "Complete job", "status": "completed",
         "conclusion": "success", "started_at": "2026-09-10T04:02:22Z",
         "completed_at": "2026-09-10T04:02:22Z"},
    ],
}]

FULL_LOG = (
    "﻿2026-09-10T04:02:20.6848382Z Current runner version: '2.337.0'\n"
    "2026-09-10T04:02:20.6873395Z ##[group]Runner Image Provisioner\n"
    "2026-09-10T04:02:20.6878486Z ##[endgroup]\n"
    "2026-09-10T04:02:21.3088202Z ##[group]Run actions/checkout@v4\n"
    "2026-09-10T04:02:21.3101612Z ##[endgroup]\n"
    "2026-09-10T04:02:21.9575151Z ##[group]Run if [ \"1\" = \"1\" ]; then\n"
    "2026-09-10T04:02:21.9578470Z \x1b[36;1m  echo \"Error: error creating EC2 "
    "Instance: ThrottlingException: Rate exceeded\" >&2\x1b[0m\n"
    "2026-09-10T04:02:21.9630122Z ##[endgroup]\n"
    "2026-09-10T04:02:21.9720832Z Error: error creating EC2 Instance: "
    "ThrottlingException: Rate exceeded\n"
    "2026-09-10T04:02:21.9738244Z \tstatus code: 400, request id: "
    "2b5e8f1a-0000-4c1d-9c2e-example\n"
    "2026-09-10T04:02:21.9740000Z ##[error]Process completed with exit code 1.\n"
    "2026-09-10T04:02:22.0018043Z Post job cleanup.\n"
)


class FakeClient:
    def __init__(self, owner="Kavinraj23", repo="drift-gate"):
        self.owner, self.repo = owner, repo
        self.posted: list[tuple[str, dict | None]] = []

    def get(self, path, params=None):
        assert path == "/actions/runs/34435597583"
        return RUN

    def get_all_pages(self, path, key, params=None):
        if key == "jobs":
            return JOBS
        if key == "workflow_runs":
            return [RUN]
        raise AssertionError(path)

    def get_text(self, path):
        assert path == "/actions/jobs/102739893085/logs"
        return FULL_LOG

    def post(self, path, json_body=None):
        self.posted.append((path, json_body))

        class _Resp:
            status_code = 201

        return _Resp()


def test_get_execution_maps_real_fields():
    src = GitHubActionsSource("Kavinraj23", "drift-gate", client=FakeClient())
    execution = src.get_execution("34435597583")
    assert execution.status == ExecutionStatus.FAILED
    assert execution.pipeline_id == "demo-failure"
    assert execution.template_ref.name == ".github/workflows/demo-failure.yml"
    assert execution.template_ref.version == "f965574"
    assert execution.runner_pool == "ubuntu-latest"
    assert execution.trigger == "workflow_dispatch"
    assert execution.infra_ref == "main"


def test_get_failed_leaf_nodes_returns_only_the_failed_step():
    src = GitHubActionsSource("Kavinraj23", "drift-gate", client=FakeClient())
    leaves = src.get_failed_leaf_nodes("34435597583")
    assert len(leaves) == 1
    assert leaves[0].name == "apply"
    assert leaves[0].status == NodeStatus.FAILED


def test_get_step_logs_isolates_the_real_step_and_strips_ansi():
    src = GitHubActionsSource("Kavinraj23", "drift-gate", client=FakeClient())
    leaves = src.get_failed_leaf_nodes("34435597583")
    log = src.get_step_logs("34435597583", leaves[0].id, budget=4000)
    assert "Error: error creating EC2 Instance: ThrottlingException: Rate exceeded" \
        in log.text
    assert "\x1b[" not in log.text
    # must not bleed into the neighboring checkout step's content
    assert "actions/checkout@v4" not in log.text


def test_get_step_logs_bookend_step_returns_empty_not_wrong_content():
    src = GitHubActionsSource("Kavinraj23", "drift-gate", client=FakeClient())
    execution = src.get_execution("34435597583")
    setup_node = next(n for n in execution.nodes if n.name == "Set up job")
    log = src.get_step_logs("34435597583", setup_node.id, budget=4000)
    assert log.text == ""


def test_list_executions_filters_by_window_and_pipeline_id():
    from datetime import datetime, timezone
    src = GitHubActionsSource("Kavinraj23", "drift-gate", client=FakeClient())
    window = (
        datetime(2026, 9, 10, tzinfo=timezone.utc),
        datetime(2026, 9, 11, tzinfo=timezone.utc),
    )
    matches = src.list_executions(window, {"pipeline_id": "demo-failure"})
    assert len(matches) == 1
    no_matches = src.list_executions(window, {"pipeline_id": "nonexistent"})
    assert no_matches == []


def test_target_execute_posts_rerun_failed_jobs():
    client = FakeClient()
    tgt = GitHubActionsTarget("Kavinraj23", "drift-gate", client=client)
    remediation = Remediation(
        tier=Tier.TIER_0, action="retry_execution", rationale="r", reversible=True,
        gate=Gate.AUTO, context={"execution_id": "34435597583"},
    )
    result = tgt.execute(remediation)
    assert result.succeeded is True
    assert client.posted == [("/actions/runs/34435597583/rerun-failed-jobs", None)]


def test_target_dry_run_and_verify_reflect_real_status():
    tgt = GitHubActionsTarget("Kavinraj23", "drift-gate", client=FakeClient())
    remediation = Remediation(
        tier=Tier.TIER_0, action="retry_execution", rationale="r", reversible=True,
        gate=Gate.AUTO, context={"execution_id": "34435597583"},
    )
    dry = tgt.dry_run(remediation)
    assert dry.would_succeed is True  # run is currently FAILED, so a retry applies

    verification = tgt.verify(remediation)
    assert verification.resolved is False  # fixture run's conclusion is still "failure"


def test_target_rejects_unsupported_action():
    tgt = GitHubActionsTarget("Kavinraj23", "drift-gate", client=FakeClient())
    remediation = Remediation(
        tier=Tier.TIER_3, action="regenerate_lockfile", rationale="r", reversible=True,
        gate=Gate.PULL_REQUEST, context={"execution_id": "34435597583"},
    )
    assert tgt.dry_run(remediation).would_succeed is False
    assert tgt.execute(remediation).succeeded is False
