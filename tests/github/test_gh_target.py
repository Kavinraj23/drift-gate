"""GitHubActionsTarget over stubbed HTTP: Tier 0 re-runs, Tier 3 branch/commit/PR, verification."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from gh_helpers import BASE_SHA, HEAD_SHA, FakeClock, FakeResponse, FakeSession, fixture

from driftgate.adapters.github_actions import GitHubActionsTarget, GitHubTargetError
from driftgate.domain import Remediation
from driftgate.tier3 import PullRequest

BRANCH = "driftgate/abc-pin_provider_version"
OLD = fixture("main.tf")
NEW = OLD.replace("~> 9.0", "3.2.1")


def tier0(action: str = "rerun_workflow", **dry: Any) -> Remediation:
    return Remediation(0, action, "transient", True, "auto", dry_run={"execution_id": "7001.1", **dry})


def tier3(**dry: Any) -> Remediation:
    return Remediation(
        3,
        "pin_provider_version",
        "pin",
        True,
        "pull_request",
        dry_run={"execution_id": "7001.1", "branch": BRANCH, **dry},
    )


def run_route(session: FakeSession, fixture_name: str) -> None:
    session.on("GET", "/actions/runs/7001", FakeResponse(200, fixture(fixture_name)))


# -- Tier 0 ------------------------------------------------------------------------------------------------------
def test_dry_run_describes_and_never_writes(target: GitHubActionsTarget, session: FakeSession) -> None:
    run_route(session, "run_7001_a1_failed.json")
    r = target.dry_run(tier0())
    assert r.ok and "POST /actions/runs/7001/rerun" in r.description and r.details["endpoint"] == "rerun"
    assert session.writes() == []


@pytest.mark.parametrize(
    ("action", "endpoint"), [("rerun_workflow", "rerun"), ("rerun_failed_job", "rerun-failed-jobs")]
)
def test_execute_posts_the_right_endpoint_once(
    target: GitHubActionsTarget, session: FakeSession, action: str, endpoint: str
) -> None:
    run_route(session, "run_7001_a1_failed.json")
    session.on("POST", f"/actions/runs/7001/{endpoint}", FakeResponse(201, {}))
    r = target.execute(tier0(action))
    assert r.ok and r.details["endpoint"] == endpoint
    posts = session.writes()
    assert [(c.method, c.path) for c in posts] == [("POST", f"/actions/runs/7001/{endpoint}")]
    assert posts[0].json is None


def test_execute_is_idempotent_when_the_run_was_already_rerun(
    target: GitHubActionsTarget, session: FakeSession
) -> None:
    run_route(session, "run_7001_a2_success.json")
    r = target.execute(tier0())
    assert r.ok and r.details["already"] is True and session.writes() == []
    assert target.dry_run(tier0()).ok


def test_execute_refuses_a_run_that_is_unfinished_or_did_not_fail(
    target: GitHubActionsTarget, session: FakeSession
) -> None:
    run_route(session, "run_7001_a2_in_progress.json")
    assert not target.execute(tier0(execution_id="7001.2")).ok
    run_route(session, "run_7002_success.json")
    assert not target.execute(tier0(execution_id="7001.1")).ok
    assert session.writes() == []


def test_execute_reports_api_failures_as_a_failed_result(target: GitHubActionsTarget, session: FakeSession) -> None:
    run_route(session, "run_7001_a1_failed.json")
    session.on("POST", "/actions/runs/7001/rerun", FakeResponse(403, {"message": "Resource not accessible"}))
    r = target.execute(tier0())
    assert not r.ok and "403" in r.description


@pytest.mark.parametrize("decision", ["refused", "downgraded"])
def test_a_non_allowed_gate_decision_never_reaches_github(
    target: GitHubActionsTarget, session: FakeSession, decision: str
) -> None:
    rem = tier0()
    rem.gate_decision = decision
    assert not target.dry_run(rem).ok and not target.execute(rem).ok
    assert session.calls == []


def test_unsupported_actions_and_tiers_are_refused(target: GitHubActionsTarget, session: FakeSession) -> None:
    assert not target.execute(tier0("clear_stale_cache")).ok
    assert not target.dry_run(tier0("clear_stale_cache")).ok
    for tier, action in ((1, "recycle_runner"), (2, "force_unlock_state"), (3, "pin_provider_version")):
        rem = Remediation(tier, action, "r", True, "pull_request", dry_run={"execution_id": "7001.1"})
        assert not target.execute(rem).ok  # Tier 3 goes through open_pull_request, never execute
    assert session.calls == []


@pytest.mark.parametrize("bad", ["", "7001", "7001.1/../x", "https://x/y", None])
def test_execution_id_is_validated(target: GitHubActionsTarget, session: FakeSession, bad: Any) -> None:
    assert not target.execute(tier0(execution_id=bad)).ok
    assert session.calls == []


def test_verify_waits_for_the_rerun_with_a_fake_clock(
    target: GitHubActionsTarget, session: FakeSession, clock: FakeClock
) -> None:
    session.on(
        "GET",
        "/actions/runs/7001",
        [
            FakeResponse(200, fixture("run_7001_a1_failed.json")),  # re-run not visible yet
            FakeResponse(200, fixture("run_7001_a2_in_progress.json")),
            FakeResponse(200, fixture("run_7001_a2_success.json")),
        ],
    )
    v = target.verify(tier0())
    assert v.verified and v.details["attempt"] == 2 and clock.sleeps == [10.0, 10.0]


def test_verify_reports_a_failing_rerun(target: GitHubActionsTarget, session: FakeSession) -> None:
    run_route(session, "run_7001_a2_failed.json")
    v = target.verify(tier0())
    assert not v.verified and "failed" in v.description and "pending" not in v.details


def test_verify_times_out_without_real_sleeping(
    target: GitHubActionsTarget, session: FakeSession, clock: FakeClock
) -> None:
    run_route(session, "run_7001_a1_failed.json")  # the re-run never appears
    v = target.verify(tier0())
    assert not v.verified and v.details["timed_out"] is True
    assert sum(clock.sleeps) >= 120 and len(clock.sleeps) == 12


# -- Tier 3: pull request -------------------------------------------------------------------------------------------
def pr_request(**kw: Any) -> PullRequest:
    base: dict[str, Any] = {
        "branch": BRANCH,
        "base": "scenario/flaky",
        "title": "DriftGate: pin_provider_version",
        "body": "## hypothesis\nevidence\nreviewer: approve\n",
        "diff_text": "this is the model's raw text and must never be committed",
        "paths": ("main.tf",),
        "files": (("main.tf", NEW),),
        "base_sha": HEAD_SHA,
    }
    base.update(kw)
    return PullRequest(**base)


def pr_routes(session: FakeSession, *, existing: dict[str, str] | None = None) -> None:
    """The branch does not exist yet; `existing` maps path -> current text on the new branch."""
    existing = existing if existing is not None else {"main.tf": OLD}
    session.on("GET", f"/git/ref/heads/{BRANCH}", FakeResponse(404, fixture("not_found.json")))
    session.on("POST", "/git/refs", FakeResponse(201, fixture("ref_created.json")))
    for path, text in existing.items():
        body = {
            "type": "file",
            "encoding": "base64",
            "sha": f"sha-{path}",
            "content": base64.b64encode(text.encode()).decode(),
        }
        session.on("GET", f"/contents/{path}", FakeResponse(200, body))
    session.on("PUT", "/contents/main.tf", FakeResponse(200, fixture("put_contents_ok.json")))
    session.on("POST", "/pulls", FakeResponse(201, fixture("pr_created.json")))


def test_pr_call_order_is_additive_branch_then_commit_then_pull_request(
    target: GitHubActionsTarget, session: FakeSession
) -> None:
    pr_routes(session)
    rec = target.open_pull_request(pr_request())
    assert (rec.number, rec.branch, rec.merged, rec.simulated) == (12, BRANCH, False, False)
    assert rec.url.endswith("/pull/12")
    assert [(c.method, c.path) for c in session.calls] == [
        ("GET", f"/git/ref/heads/{BRANCH}"),  # the branch must not exist
        ("POST", "/git/refs"),
        ("GET", "/contents/main.tf"),
        ("PUT", "/contents/main.tf"),
        ("POST", "/pulls"),
    ]
    assert all(c.method in ("GET", "POST", "PUT") for c in session.calls)  # nothing destructive


def test_branch_is_created_from_the_failing_commit_with_the_agent_prefix(
    target: GitHubActionsTarget, session: FakeSession
) -> None:
    pr_routes(session)
    target.open_pull_request(pr_request())
    create = next(c for c in session.calls if c.path == "/git/refs")
    assert create.json == {"ref": f"refs/heads/{BRANCH}", "sha": HEAD_SHA}


def test_without_a_base_sha_the_base_branch_tip_is_used(target: GitHubActionsTarget, session: FakeSession) -> None:
    pr_routes(session)
    session.on("GET", "/git/ref/heads/scenario/flaky", FakeResponse(200, fixture("ref_main.json")))
    target.open_pull_request(pr_request(base_sha=""))
    assert next(c for c in session.calls if c.path == "/git/refs").json["sha"] == BASE_SHA  # type: ignore[index]


def test_the_commit_is_built_from_the_checked_contents_never_from_the_models_diff_text(
    target: GitHubActionsTarget, session: FakeSession
) -> None:
    pr_routes(session)
    target.open_pull_request(pr_request(diff_text="--- a/main.tf\n+++ b/main.tf\n@@ -1 +1 @@\n-evil\n+EVIL_CONTENT\n"))
    put = next(c for c in session.calls if c.method == "PUT")
    assert base64.b64decode(put.json["content"]).decode() == NEW  # type: ignore[index]
    assert put.json["branch"] == BRANCH and put.json["sha"] == "sha-main.tf"  # type: ignore[index]
    sent = " ".join(str(c.json) for c in session.writes())
    assert "EVIL_CONTENT" not in sent and "raw text" not in sent


def test_a_new_file_is_created_without_a_blob_sha(target: GitHubActionsTarget, session: FakeSession) -> None:
    pr_routes(session, existing={})
    session.on("GET", "/contents/main.tf", FakeResponse(404, fixture("not_found.json")))
    target.open_pull_request(pr_request())
    put = next(c for c in session.calls if c.method == "PUT")
    assert "sha" not in put.json  # type: ignore[operator]


def test_growing_files_are_committed_before_shrinking_ones(target: GitHubActionsTarget, session: FakeSession) -> None:
    """Invariant 13 at the commit level, whatever order the proposal listed the files in."""
    pr_routes(session, existing={"shrinks.tf": "a\nb\nc\nd\n", "grows.tf": "a\n"})
    session.on("PUT", "/contents/shrinks.tf", FakeResponse(200, {}))
    session.on("PUT", "/contents/grows.tf", FakeResponse(200, {}))
    req = pr_request(paths=("shrinks.tf", "grows.tf"), files=(("shrinks.tf", "a\n"), ("grows.tf", "a\nb\nc\n")))
    target.open_pull_request(req)
    assert [c.path for c in session.calls if c.method == "PUT"] == ["/contents/grows.tf", "/contents/shrinks.tf"]


def test_the_pull_request_carries_the_body_and_targets_the_given_base(
    target: GitHubActionsTarget, session: FakeSession
) -> None:
    pr_routes(session)
    target.open_pull_request(pr_request())
    call = next(c for c in session.calls if c.path == "/pulls")
    assert call.json == {
        "title": "DriftGate: pin_provider_version",
        "head": BRANCH,
        "base": "scenario/flaky",
        "body": "## hypothesis\nevidence\nreviewer: approve\n",
        "draft": False,
    }


@pytest.mark.parametrize(
    "branch",
    ["main", "scenario/flaky", "driftgate/", "driftgate", "Driftgate/x", "driftgate/../main", "refs/heads/driftgate/x"],
)
def test_branches_outside_the_agent_namespace_are_refused_before_any_http(
    target: GitHubActionsTarget, session: FakeSession, branch: str
) -> None:
    with pytest.raises(GitHubTargetError):
        target.open_pull_request(pr_request(branch=branch))
    assert session.calls == []


@pytest.mark.parametrize(
    "kw",
    [
        {"files": ()},
        {"files": (("main.tf", NEW), ("other.tf", "x")), "paths": ("main.tf",)},
        {"files": (("main.tf", NEW),), "paths": ("other.tf",)},
        {"files": (("main.tf", NEW), ("main.tf", NEW)), "paths": ("main.tf", "main.tf")},
        {"files": ((".env", "X=1"),), "paths": (".env",)},
        {"files": (("../x.tf", "x"),), "paths": ("../x.tf",)},
        {"files": (("/abs.tf", "x"),), "paths": ("/abs.tf",)},
        {"files": ((".git/config", "x"),), "paths": (".git/config",)},
        {"files": (("a?b.tf", "x"),), "paths": ("a?b.tf",)},
        {"base": BRANCH},
        {"base": "../x"},
        {"base": ""},
    ],
)
def test_unsafe_or_inconsistent_pull_requests_are_refused_before_any_http(
    target: GitHubActionsTarget, session: FakeSession, kw: dict[str, Any]
) -> None:
    with pytest.raises(GitHubTargetError):
        target.open_pull_request(pr_request(**kw))
    assert session.calls == []


def test_an_existing_branch_is_never_overwritten(target: GitHubActionsTarget, session: FakeSession) -> None:
    session.on("GET", f"/git/ref/heads/{BRANCH}", FakeResponse(200, fixture("ref_pr_branch.json")))
    with pytest.raises(GitHubTargetError, match="already exists"):
        target.open_pull_request(pr_request())
    assert session.writes() == []


def test_a_failure_after_the_branch_exists_leaves_it_alone_and_escalates(
    target: GitHubActionsTarget, session: FakeSession
) -> None:
    pr_routes(session)
    session.on("PUT", "/contents/main.tf", FakeResponse(422, {"message": "Invalid request"}))
    with pytest.raises(GitHubTargetError, match="422"):
        target.open_pull_request(pr_request())
    assert not any(c.method not in ("GET", "POST", "PUT") for c in session.calls)  # no cleanup deletion
    assert "/pulls" not in session.paths("POST")


def test_no_merge_request_is_ever_issued_on_any_path(target: GitHubActionsTarget, session: FakeSession) -> None:
    pr_routes(session)
    run_route(session, "run_7001_a1_failed.json")
    session.on("POST", "/actions/runs/7001/rerun", FakeResponse(201, {}))
    target.open_pull_request(pr_request())
    target.execute(tier0())
    assert not [
        c for c in session.calls if "merge" in c.path.lower() or "merge" in str(c.json).lower().split("body")[0]
    ]
    assert not hasattr(target, "merge") and not hasattr(target, "enable_auto_merge")


# -- Tier 3: verification ---------------------------------------------------------------------------------------------
def ci_run(conclusion: str | None, status: str = "completed", sha: str = HEAD_SHA) -> dict[str, Any]:
    run = fixture("run_7001_a1_failed.json")
    run.update(status=status, conclusion=conclusion, head_sha=sha, head_branch=BRANCH, id=7500)
    return run


def verify_routes(session: FakeSession, runs: list[FakeResponse]) -> None:
    session.on("GET", f"/git/ref/heads/{BRANCH}", FakeResponse(200, fixture("ref_pr_branch.json")))
    session.on("GET", "/actions/runs", runs)
    run_route(session, "run_7001_a1_failed.json")
    session.on("POST", "/actions/workflows/flaky.yml/dispatches", FakeResponse(204))
    session.on("GET", "/issues/12/comments", FakeResponse(200, fixture("comments_empty.json")))
    session.on("POST", "/issues/12/comments", FakeResponse(201, {"id": 1}))


SHA_BRANCH = "dec0de" * 6 + "dec0"


def test_green_ci_verifies_and_leaves_one_verified_by_ci_comment(
    target: GitHubActionsTarget, session: FakeSession
) -> None:
    green = {"workflow_runs": [ci_run("success", sha=SHA_BRANCH)]}
    verify_routes(session, [FakeResponse(200, {"workflow_runs": []}), FakeResponse(200, green)])
    rem = tier3(pull_request={"number": 12})
    v = target.verify(rem)
    assert v.verified and v.details["comment"] == "commented"
    # no run existed, so CI was started on the PR branch by workflow_dispatch (workflows are dispatch-only)
    dispatch = next(c for c in session.calls if c.path.endswith("/dispatches"))
    assert dispatch.json == {"ref": BRANCH}
    comment = next(c for c in session.calls if c.method == "POST" and c.path == "/issues/12/comments")
    assert "Verified by CI" in comment.json["body"] and SHA_BRANCH[:7] in comment.json["body"]  # type: ignore[index]
    assert len(session.writes()) == 2  # the dispatch and the comment: no merge, no approval, no PR edit


def test_verification_is_idempotent_about_the_comment(target: GitHubActionsTarget, session: FakeSession) -> None:
    green = {"workflow_runs": [ci_run("success", sha=SHA_BRANCH)]}
    verify_routes(session, [FakeResponse(200, green)])
    marker = f"<!-- driftgate:verified-by-ci:{SHA_BRANCH} -->"
    session.on("GET", "/issues/12/comments", FakeResponse(200, [{"body": marker + "\nVerified by CI"}]))
    v = target.verify(tier3(pull_request={"number": 12}))
    assert v.verified and v.details["comment"] == "already commented"
    assert not [c for c in session.writes() if c.path.endswith("/comments")]


def test_existing_runs_are_not_dispatched_again(target: GitHubActionsTarget, session: FakeSession) -> None:
    queued = {"workflow_runs": [ci_run(None, status="queued", sha=SHA_BRANCH)]}
    green = {"workflow_runs": [ci_run("success", sha=SHA_BRANCH)]}
    verify_routes(session, [FakeResponse(200, queued), FakeResponse(200, green)])
    assert target.verify(tier3(pull_request={"number": 12})).verified
    assert not [c for c in session.calls if c.path.endswith("/dispatches")]


def test_red_ci_is_not_verified_and_leaves_no_comment(target: GitHubActionsTarget, session: FakeSession) -> None:
    red = {"workflow_runs": [ci_run("failure", sha=SHA_BRANCH)]}
    verify_routes(session, [FakeResponse(200, red)])
    v = target.verify(tier3(pull_request={"number": 12}))
    assert not v.verified and "failed" in v.description
    assert session.writes() == []


def test_runs_for_other_commits_do_not_count(target: GitHubActionsTarget, session: FakeSession) -> None:
    stale = {"workflow_runs": [ci_run("success", sha=HEAD_SHA)]}  # the failing commit's run, not the PR branch head
    verify_routes(session, [FakeResponse(200, stale)])
    v = target.verify(tier3(pull_request={"number": 12}))
    assert not v.verified and v.details["timed_out"] is True and v.details["pending"] is True


def test_the_pr_number_is_looked_up_when_the_record_is_missing(
    target: GitHubActionsTarget, session: FakeSession
) -> None:
    green = {"workflow_runs": [ci_run("success", sha=SHA_BRANCH)]}
    verify_routes(session, [FakeResponse(200, green)])
    session.on("GET", "/pulls", FakeResponse(200, [{"number": 12}]))
    assert target.verify(tier3()).verified
    pulls = next(c for c in session.calls if c.path == "/pulls")
    assert pulls.params == {"head": "octo-org:" + BRANCH, "state": "open"}


def test_verify_refuses_a_non_agent_branch(target: GitHubActionsTarget, session: FakeSession) -> None:
    v = target.verify(tier3(branch="main"))
    assert not v.verified and session.calls == []


def test_verify_of_unsupported_tiers_is_false(target: GitHubActionsTarget) -> None:
    assert not target.verify(Remediation(1, "recycle_runner", "r", True, "single_approval")).verified
