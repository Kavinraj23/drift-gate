"""GitHubActionsSource over stubbed HTTP: mapping, logs, listing, file reads, repo refusal."""

from __future__ import annotations

import base64
import io
import zipfile

import pytest
from gh_helpers import HEAD_SHA, PREFIX, REPO, FakeResponse, FakeSession, fixture, route_failed_run

from driftgate.adapters.github_actions import (
    GitHubActionsSource,
    decode_log,
    node_status,
    split_execution_id,
    workflow_name,
)
from driftgate.domain import SourceError
from driftgate.prefilter import prefilter


def test_failed_run_maps_to_an_execution_tree_with_the_failed_step_as_leaf(
    source: GitHubActionsSource, session: FakeSession
) -> None:
    route_failed_run(session)
    ex = source.get_execution("7001.1")
    assert ex.execution_id == "7001.1" and ex.status == "failed"
    assert ex.pipeline == f"{REPO}:flaky.yml"
    assert ex.refs["commit"] == HEAD_SHA and ex.refs["branch"] == "scenario/flaky" and "retry_of" not in ex.refs
    assert ex.root is not None and ex.root.node_id == "run:7001"
    job = ex.root.children[0]
    assert (job.node_id, job.name, job.status) == ("job:9001", "canary", "failed")
    assert [(s.node_id, s.status) for s in job.children] == [
        ("step:9001:1", "success"),
        ("step:9001:2", "failed"),
        ("step:9001:3", "skipped"),
    ]
    leaves = source.get_failed_leaf_nodes("7001.1")
    assert [n.node_id for n in leaves] == ["step:9001:2"] and leaves[0].name == "Run canary"


def test_a_rerun_is_a_new_execution_that_points_at_the_failed_attempt(
    source: GitHubActionsSource, session: FakeSession
) -> None:
    route_failed_run(session)
    ex = source.get_execution("7001.2")
    assert ex.status == "success" and ex.refs["retry_of"] == "7001.1" and ex.refs["run_attempt"] == "2"
    assert source.get_failed_leaf_nodes("7001.2") == []


def test_statuses_use_the_synthetic_adapters_lowercase_strings() -> None:
    cases = {
        ("completed", "success"): "success",
        ("completed", "failure"): "failed",
        ("completed", "timed_out"): "failed",
        ("completed", "cancelled"): "cancelled",
        ("completed", "skipped"): "skipped",
        ("in_progress", None): "running",
        ("queued", None): "pending",
    }
    for (status, conclusion), want in cases.items():
        assert node_status(status, conclusion) == want


def test_a_cancelled_run_is_closed_by_the_prefilter_as_an_abort(
    source: GitHubActionsSource, session: FakeSession
) -> None:
    route_failed_run(session)
    run = fixture("run_7001_a1_failed.json")
    run["conclusion"] = "cancelled"
    session.on("GET", "/actions/runs/7001/attempts/1", FakeResponse(200, run))
    assert prefilter(source.get_execution("7001.1")).closed


def test_jobs_that_never_started_are_a_rejected_approval_and_closed_without_a_model(
    source: GitHubActionsSource, session: FakeSession
) -> None:
    """The mapping rule is an UNVERIFIED heuristic (see docs/design/M8a.md); this pins what it does."""
    session.on("GET", "/actions/runs/7100/attempts/1", FakeResponse(200, fixture("run_7100_rejected.json")))
    session.on("GET", "/actions/runs/7100/attempts/1/jobs", FakeResponse(200, fixture("jobs_rejected.json")))
    ex = source.get_execution("7100.1")
    assert ex.status == "approval_rejected" and ex.root is not None and ex.root.children[0].status == "rejected"
    assert source.get_failed_leaf_nodes("7100.1") == []
    assert prefilter(ex).closed


def test_a_failed_job_with_steps_is_not_mistaken_for_a_rejection(
    source: GitHubActionsSource, session: FakeSession
) -> None:
    route_failed_run(session)
    assert source.get_execution("7001.1").status == "failed"


def test_completed_attempts_are_cached_and_running_ones_are_not(
    source: GitHubActionsSource, session: FakeSession
) -> None:
    route_failed_run(session)
    source.get_execution("7001.1")
    source.get_execution("7001.1")
    assert session.paths("GET").count("/actions/runs/7001/attempts/1") == 1
    session.on("GET", "/actions/runs/7001/attempts/2", FakeResponse(200, fixture("run_7001_a2_in_progress.json")))
    assert source.get_execution("7001.2").status == "running"
    source.get_execution("7001.2")
    assert session.paths("GET").count("/actions/runs/7001/attempts/2") == 2


@pytest.mark.parametrize("bad", ["7001", "7001.", "abc.1", "7001.1/../x", "https://github.com/x/y", "../7001.1", ""])
def test_malformed_execution_ids_are_refused_before_any_http(
    source: GitHubActionsSource, session: FakeSession, bad: str
) -> None:
    with pytest.raises(SourceError):
        source.get_execution(bad)
    assert session.calls == []


def test_unknown_execution_is_a_source_error(source: GitHubActionsSource, session: FakeSession) -> None:
    session.on("GET", "/actions/runs/1/attempts/1", FakeResponse(404, fixture("not_found.json")))
    with pytest.raises(SourceError, match="unknown execution"):
        source.get_execution("1.1")


# -- logs --------------------------------------------------------------------------------------------------------
def _log_routes(session: FakeSession, body: bytes) -> None:
    route_failed_run(session)
    storage = "https://pipelines.example.invalid/signed/job-9001"
    session.on("GET", "/actions/jobs/9001/logs", FakeResponse(302, headers={"Location": storage}))
    session.on("GET", storage, FakeResponse(200, content=body))


def test_job_logs_follow_the_redirect_and_never_send_the_token_to_the_storage_host(
    source: GitHubActionsSource, session: FakeSession
) -> None:
    _log_routes(session, fixture("job_9001.log").encode())
    chunk = source.get_step_logs("7001.1", "step:9001:2", 100_000)
    assert "##[error]Process completed with exit code 1." in chunk.text and not chunk.truncated
    api, storage = [c for c in session.calls if "logs" in c.url or "signed" in c.url]
    assert api.allow_redirects is False and "Authorization" in api.headers
    assert "Authorization" not in storage.headers


def test_a_long_log_keeps_its_tail_and_is_flagged_truncated(source: GitHubActionsSource, session: FakeSession) -> None:
    _log_routes(session, ("noise\n" * 100 + "the failure\n").encode())
    chunk = source.get_step_logs("7001.1", "job:9001", 50)
    assert chunk.truncated and chunk.text.endswith("the failure\n") and len(chunk.text) == 50


def test_zip_logs_are_decoded() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("canary/2_Run canary.txt", "second\n")
        zf.writestr("canary/1_Set up job.txt", "first\n")
    assert decode_log(buf.getvalue()) == "first\n\nsecond\n"
    assert decode_log(b"plain \xff text") == "plain \ufffd text"


@pytest.mark.parametrize("node", ["run:7001", "job:1", "step:777:1", "../x", "job:9001/../1", ""])
def test_log_requests_for_foreign_or_malformed_nodes_make_no_log_call(
    source: GitHubActionsSource, session: FakeSession, node: str
) -> None:
    route_failed_run(session)
    with pytest.raises(SourceError):
        source.get_step_logs("7001.1", node, 1000)
    assert not any("/logs" in p for p in session.paths())


def test_log_http_failure_becomes_a_source_error(source: GitHubActionsSource, session: FakeSession) -> None:
    route_failed_run(session)
    session.on("GET", "/actions/jobs/9001/logs", FakeResponse(410, {"message": "Gone"}))
    with pytest.raises(SourceError, match="410"):
        source.get_step_logs("7001.1", "job:9001", 1000)


# -- listing -----------------------------------------------------------------------------------------------------
def test_listing_expands_earlier_attempts_so_flake_history_can_see_fail_then_pass(
    source: GitHubActionsSource, session: FakeSession
) -> None:
    route_failed_run(session)
    session.on("GET", "/actions/runs", FakeResponse(200, fixture("runs_list.json")))
    rows = source.list_executions(("2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"), None)
    assert [(r.execution_id, r.status) for r in rows] == [
        ("7002.1", "success"),
        ("7001.1", "failed"),
        ("7001.2", "success"),
    ]
    assert rows[2].refs["retry_of"] == "7001.1"
    assert session.calls[0].params["created"] == "2026-10-01T00:00:00Z..2026-10-02T00:00:00Z"  # type: ignore[index]


def test_listing_filters_by_status_pipeline_and_refs(source: GitHubActionsSource, session: FakeSession) -> None:
    route_failed_run(session)
    session.on("GET", "/actions/runs", FakeResponse(200, fixture("runs_list.json")))
    failed = source.list_executions(None, {"status": "failed"})
    assert [r.execution_id for r in failed] == ["7001.1"]
    assert session.calls[0].params["status"] == "failure"  # type: ignore[index]
    assert [r.execution_id for r in source.list_executions(None, {"pipeline": f"{REPO}:other.yml"})] == ["7002.1"]
    assert [r.execution_id for r in source.list_executions(None, {"branch": "scenario/throttle"})] == ["7002.1"]
    assert [r.execution_id for r in source.list_executions(None, {"retry_of": "7001.1"})] == ["7001.2"]


def test_listing_window_excludes_runs_outside_it(source: GitHubActionsSource, session: FakeSession) -> None:
    route_failed_run(session)
    session.on("GET", "/actions/runs", FakeResponse(200, fixture("runs_list.json")))
    rows = source.list_executions(("2026-10-01T10:01:00Z", "2026-10-01T11:00:00Z"), None)
    assert [r.execution_id for r in rows] == ["7001.2"]


def test_listing_caps_pages(client, session: FakeSession) -> None:  # type: ignore[no-untyped-def]
    one = fixture("runs_list.json")
    page = {"workflow_runs": [one["workflow_runs"][1]] * 2}
    session.on("GET", "/actions/runs", FakeResponse(200, page))
    src = GitHubActionsSource(client, max_pages=3, per_page=2)
    src.list_executions(None, None)
    assert session.paths("GET").count("/actions/runs") == 3


# -- files -------------------------------------------------------------------------------------------------------
def test_read_file_returns_raw_text_with_a_size_cap(source: GitHubActionsSource, session: FakeSession) -> None:
    text = fixture("main.tf")
    session.on("GET", "/contents/main.tf", FakeResponse(200, content=text.encode()))
    f = source.read_file(REPO, "main.tf", HEAD_SHA, 10_000)
    assert f.content == text and not f.truncated and f.repo == REPO and f.ref == HEAD_SHA
    capped = source.read_file(f"{REPO}:flaky.yml", "main.tf", "scenario/flaky", 20)
    assert capped.truncated and len(capped.content) == 20
    call = session.calls[0]
    assert call.params == {"ref": HEAD_SHA} and call.headers["Accept"] == "application/vnd.github.raw+json"


def test_read_file_missing_is_a_source_error(source: GitHubActionsSource, session: FakeSession) -> None:
    session.on("GET", "/contents/nope.tf", FakeResponse(404, fixture("not_found.json")))
    with pytest.raises(SourceError, match="no such file"):
        source.read_file(REPO, "nope.tf", "main", 100)


@pytest.mark.parametrize(
    "path", ["../etc/passwd", "/abs", "a/../../b", "C:\\x", "a\\b", "", "x?ref=evil", "x#frag", "a/./../../b"]
)
def test_read_file_refuses_unsafe_paths_without_http(
    source: GitHubActionsSource, session: FakeSession, path: str
) -> None:
    with pytest.raises(SourceError):
        source.read_file(REPO, path, "main", 100)
    assert session.calls == []


@pytest.mark.parametrize("ref", ["", "a b", "main?x=1", "a/../b", "x#y", "-" * 300])
def test_read_file_refuses_odd_refs(source: GitHubActionsSource, session: FakeSession, ref: str) -> None:
    with pytest.raises(SourceError):
        source.read_file(REPO, "main.tf", ref, 100)
    assert session.calls == []


def test_helpers() -> None:
    assert split_execution_id("12.3") == (12, 3)
    assert workflow_name({"path": ".github/workflows/a.yml@refs/heads/x"}) == "a.yml"
    assert workflow_name({"path": "weird name/with spaces"}) == "workflow"
    assert PREFIX.endswith(REPO)
    assert base64  # fixtures use base64 for contents
