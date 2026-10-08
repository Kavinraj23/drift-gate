"""Invariant 12 (one repo), invariant 4 (no merge), the write allow-list, rate limits and token hygiene."""

from __future__ import annotations

import inspect
import logging

import pytest
from gh_helpers import HEAD_SHA, PREFIX, REPO, TOKEN, FakeClock, FakeResponse, FakeSession, fixture

from driftgate.adapters import github_actions, github_client
from driftgate.adapters.github_actions import GitHubActionsSource, GitHubActionsTarget
from driftgate.adapters.github_client import (
    GitHubClient,
    GitHubConfig,
    GitHubError,
    RateLimited,
    RepoRefused,
    WriteRefused,
    load_github_config,
    require_repo,
    valid_agent_branch,
)
from driftgate.domain import Remediation, SourceError

NEAR_MISSES = [
    "octo-org/drift-gate-playground2",
    "octo-org/drift-gate",
    "octo-org/drift-gate-playgroun",
    "octo-org/Drift-Gate-Playground",  # case variant
    "OCTO-ORG/drift-gate-playground",
    "Octo-Org/drift-gate-playground",
    "other-org/drift-gate-playground",
    "octo-org/drift-gate-playground.git",
    "octo-org/drift-gate-playground/",
    "octo-org/drift-gate-playground/../other",
    " octo-org/drift-gate-playground",
    "octo-org/drift-gate-playground ",
    "octo-org/drift-gate-playground\n",
    "octo-org\\drift-gate-playground",
    "https://github.com/octo-org/drift-gate-playground",
    "http://github.com/octo-org/drift-gate-playground",
    "github.com/octo-org/drift-gate-playground",
    "git@github.com:octo-org/drift-gate-playground.git",
    "https://api.github.com/repos/octo-org/drift-gate-playground",
    "octo-org/drift-gate-playground@main",
    "octo-org/drift-gate-playground#1",
    "octo-org/drift-gate-playground?x=1",
    "/repos/octo-org/drift-gate-playground",
    "drift-gate-playground",
    "octo-org",
    "",
    "evil/repo",
    "octo-org/drift-gate-playground%2Fx",
]


@pytest.mark.parametrize("repo", NEAR_MISSES)
def test_source_refuses_every_near_miss_repo_without_any_http(
    source: GitHubActionsSource, session: FakeSession, repo: str
) -> None:
    with pytest.raises(RepoRefused):
        source.read_file(repo, "main.tf", "main", 100)
    assert session.calls == []


@pytest.mark.parametrize("repo", NEAR_MISSES)
def test_target_refuses_an_action_that_names_another_repo(
    target: GitHubActionsTarget, session: FakeSession, repo: str
) -> None:
    rem = Remediation(0, "rerun_workflow", "r", True, "auto", dry_run={"execution_id": "7001.1", "repo": repo})
    result = target.execute(rem)
    assert not result.ok and "refusing repo" in result.description
    assert session.calls == []  # not even a read


def test_the_exact_repo_and_the_pipeline_form_are_accepted() -> None:
    require_repo(REPO, REPO)
    require_repo(REPO, f"{REPO}".partition(":")[0])
    with pytest.raises(RepoRefused):
        require_repo(REPO, REPO.upper())


def test_repo_refusal_is_a_source_error_and_a_value_error() -> None:
    assert issubclass(RepoRefused, SourceError) and issubclass(RepoRefused, ValueError)


def test_every_url_the_client_builds_is_under_the_configured_repo(client: GitHubClient, session: FakeSession) -> None:
    for path in ("/actions/runs/1", "/contents/a/b.tf", "/pulls", "/git/ref/heads/driftgate/x"):
        session.on("GET", path, FakeResponse(200, {}))
        client.request("GET", path)
    assert session.calls and all(c.url.startswith(PREFIX + "/") for c in session.calls)


@pytest.mark.parametrize("path", ["actions/runs/1", "/a//b", "/a/../b", "/a?x=1", "/a#b", ""])
def test_malformed_paths_never_leave_the_client(client: GitHubClient, session: FakeSession, path: str) -> None:
    with pytest.raises(GitHubError):
        client.request("GET", path)
    assert session.calls == []


def test_config_validation() -> None:
    for bad in ("", "noslash", "a/b/c", "https://github.com/a/b", "a/ b"):
        with pytest.raises(ValueError):
            GitHubConfig(repo=bad, token=TOKEN)
    with pytest.raises(ValueError):
        GitHubConfig(repo=REPO, token="")
    with pytest.raises(ValueError):
        GitHubConfig(repo=REPO, token=TOKEN, api_url="http://api.github.com")


# -- no merge, no destructive writes -----------------------------------------------------------------------------
def test_there_is_no_merge_or_auto_merge_anywhere_in_the_adapter() -> None:
    for cls in (GitHubActionsTarget, GitHubActionsSource, GitHubClient):
        names = [n for n, _ in inspect.getmembers(cls) if not n.startswith("__")]
        assert not [n for n in names if "merge" in n.lower()], cls
    assert not [n for n in dir(github_actions) + dir(github_client) if "merge" in n.lower() and n.islower()]
    for module in (github_actions, github_client):
        src = inspect.getsource(module)
        assert '"DELETE"' not in src and "'DELETE'" not in src
        assert "/merge" not in src.replace("merge-shaped", "") and "enable_auto" not in src.lower()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/pulls/12/merge", {}),
        ("POST", "/pulls/12/merge", {}),
        ("POST", "/merges", {"base": "main", "head": "driftgate/x"}),
        ("PUT", "/pulls/12/auto-merge", {}),
        ("POST", "/pulls/12/auto_merge", {}),
        ("GET", "/pulls/12/merge", None),
        ("DELETE", "/git/refs/heads/driftgate/x", None),
        ("DELETE", "/actions/runs/7001", None),
        ("PATCH", "/git/refs/heads/driftgate/x", {"force": True, "sha": "a" * 40}),
        ("PATCH", "/pulls/12", {"state": "closed"}),
        ("POST", "/git/refs", {"ref": "refs/heads/main", "sha": "a" * 40}),
        ("POST", "/git/refs", {"ref": "refs/heads/scenario/flaky", "sha": "a" * 40}),
        ("POST", "/git/refs", {"ref": "refs/tags/baseline", "sha": "a" * 40}),
        ("PUT", "/contents/main.tf", {"branch": "main", "content": "eA==", "message": "m"}),
        ("PUT", "/contents/main.tf", {"branch": "scenario/flaky", "content": "eA==", "message": "m"}),
        ("PUT", "/contents/main.tf", {"content": "eA==", "message": "m"}),  # no branch means the default branch
        ("PUT", "/contents/main.tf", {"branch": "driftgate/../main", "content": "eA==", "message": "m"}),
        ("POST", "/pulls", {"head": "scenario/flaky", "base": "main", "title": "t"}),
        ("POST", "/actions/workflows/x.yml/dispatches", {"ref": "main"}),
        ("POST", "/actions/runs/7001/cancel", {}),
        ("POST", "/actions/secrets", {}),
        ("PUT", "/actions/secrets/X", {}),
        ("POST", "/hooks", {}),
        ("PUT", "/branches/main/protection", {}),
        ("PATCH", "/", {}),
    ],
)
def test_forbidden_requests_are_refused_before_any_http(
    client: GitHubClient, session: FakeSession, method: str, path: str, body: dict | None
) -> None:
    with pytest.raises(GitHubError):
        client.request(method, path, json=body)
    assert session.calls == []


def test_the_allowed_writes_are_exactly_the_documented_ones(client: GitHubClient, session: FakeSession) -> None:
    allowed = [
        ("POST", "/actions/runs/7001/rerun", None),
        ("POST", "/actions/runs/7001/rerun-failed-jobs", None),
        ("POST", "/actions/workflows/flaky.yml/dispatches", {"ref": "driftgate/x"}),
        ("POST", "/git/refs", {"ref": "refs/heads/driftgate/x", "sha": "a" * 40}),
        ("PUT", "/contents/dir/main.tf", {"branch": "driftgate/x", "content": "eA==", "message": "m"}),
        ("POST", "/pulls", {"head": "driftgate/x", "base": "scenario/flaky", "title": "t"}),
        ("POST", "/issues/12/comments", {"body": "b"}),
    ]
    for method, path, body in allowed:
        session.on(method, path, FakeResponse(201, {}))
        assert client.request(method, path, json=body).status_code == 201
    assert len(session.calls) == len(allowed)


def test_file_names_that_merely_contain_merge_are_not_blocked(client: GitHubClient, session: FakeSession) -> None:
    session.on("GET", "/contents/docs/emergency.md", FakeResponse(200, {}))
    assert client.request("GET", "/contents/docs/emergency.md").status_code == 200


def test_the_write_budget_stops_a_runaway_loop(session: FakeSession, clock: FakeClock) -> None:
    c = GitHubClient(GitHubConfig(repo=REPO, token=TOKEN, max_writes=2), session, clock=clock, sleep=clock.sleep)
    session.on("POST", "/actions/runs/1/rerun", FakeResponse(201, {}))
    c.request("POST", "/actions/runs/1/rerun")
    c.request("POST", "/actions/runs/1/rerun")
    with pytest.raises(WriteRefused, match="budget"):
        c.request("POST", "/actions/runs/1/rerun")
    assert len(session.calls) == 2


@pytest.mark.parametrize(
    ("branch", "ok"),
    [
        ("driftgate/abc-pin_provider_version", True),
        ("driftgate/a/b", True),
        ("driftgate/", False),
        ("driftgate", False),
        ("main", False),
        ("Driftgate/x", False),
        ("driftgate/../main", False),
        ("driftgate//x", False),
        ("driftgate/x.lock", False),
        ("driftgate/x/", False),
        ("driftgate/x y", False),
        ("driftgate/x?y", False),
        ("xdriftgate/x", False),
        ("refs/heads/driftgate/x", False),
    ],
)
def test_agent_branch_names(branch: str, ok: bool) -> None:
    assert valid_agent_branch(branch) is ok


# -- rate limits (fake clock, no real sleeping) -------------------------------------------------------------------
def test_an_exhausted_budget_waits_until_the_reset_time_before_the_next_call(
    client: GitHubClient, session: FakeSession, clock: FakeClock
) -> None:
    reset = clock.now + 120
    session.on(
        "GET",
        "/actions/runs/1",
        [
            FakeResponse(200, {}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(reset))}),
            FakeResponse(200, {}, headers={"x-ratelimit-remaining": "4999"}),
        ],
    )
    client.request("GET", "/actions/runs/1")
    assert clock.sleeps == []
    client.request("GET", "/actions/runs/1")
    assert len(clock.sleeps) == 1 and clock.sleeps[0] == pytest.approx(121.0)
    assert clock.now >= reset


def test_a_secondary_rate_limit_honours_retry_after_then_succeeds(
    client: GitHubClient, session: FakeSession, clock: FakeClock
) -> None:
    session.on(
        "GET",
        "/actions/runs",
        [FakeResponse(403, fixture("rate_limited.json"), headers={"Retry-After": "30"}), FakeResponse(200, {"ok": 1})],
    )
    resp = client.request("GET", "/actions/runs")
    assert resp.status_code == 200 and clock.sleeps == [31.0] and len(session.calls) == 2


def test_a_primary_limit_403_waits_for_the_reset_and_retries(
    client: GitHubClient, session: FakeSession, clock: FakeClock
) -> None:
    reset = str(int(clock.now + 60))
    limited = FakeResponse(
        403, fixture("rate_limited.json"), headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset}
    )
    session.on("GET", "/actions/runs", [limited, FakeResponse(200, {"ok": 1})])
    assert client.request("GET", "/actions/runs").status_code == 200
    assert len(clock.sleeps) == 1 and clock.sleeps[0] == pytest.approx(61.0)


def test_waits_over_the_limit_raise_instead_of_sleeping(session: FakeSession, clock: FakeClock) -> None:
    c = GitHubClient(GitHubConfig(repo=REPO, token=TOKEN, max_wait_s=60), session, clock=clock, sleep=clock.sleep)
    session.on("GET", "/actions/runs", FakeResponse(429, {}, headers={"Retry-After": "3600"}))
    with pytest.raises(RateLimited):
        c.request("GET", "/actions/runs")
    assert clock.sleeps == []


def test_retries_are_bounded(client: GitHubClient, session: FakeSession, clock: FakeClock) -> None:
    session.on("GET", "/actions/runs", FakeResponse(429, {}, headers={"Retry-After": "1"}))
    assert client.request("GET", "/actions/runs").status_code == 429  # surfaced after max_retries
    assert len(session.calls) == 3 and len(clock.sleeps) == 2


def test_a_plain_403_is_not_retried(client: GitHubClient, session: FakeSession, clock: FakeClock) -> None:
    session.on(
        "GET", "/actions/runs", FakeResponse(403, {"message": "Resource not accessible by personal access token"})
    )
    assert client.request("GET", "/actions/runs").status_code == 403
    assert len(session.calls) == 1 and clock.sleeps == []


# -- the token never leaks ---------------------------------------------------------------------------------------------
def test_token_is_only_in_the_authorization_header(
    client: GitHubClient, config: GitHubConfig, session: FakeSession
) -> None:
    session.on("GET", "/actions/runs/1", FakeResponse(200, {}))
    client.request("GET", "/actions/runs/1")
    call = session.calls[0]
    assert call.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in call.url and TOKEN not in str(call.params) and TOKEN not in str(call.json)
    assert TOKEN not in repr(config) and TOKEN not in str(config) and TOKEN not in repr(client.__dict__.get("_config"))


def test_token_is_scrubbed_from_error_messages_and_session_failures(
    client: GitHubClient, session: FakeSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    session.on("GET", "/actions/runs/1", FakeResponse(401, {"message": f"Bad credentials {TOKEN}"}))
    with pytest.raises(GitHubError) as e:
        client.get_json("/actions/runs/1")
    assert TOKEN not in str(e.value) and "***" in str(e.value) and e.value.status == 401

    def boom(_call: object) -> FakeResponse:
        raise ConnectionError(f"proxy said Authorization: Bearer {TOKEN}")

    session.on("GET", "/actions/runs/2", boom)
    with pytest.raises(GitHubError) as e2:
        client.request("GET", "/actions/runs/2")
    assert TOKEN not in str(e2.value) and "ConnectionError" in str(e2.value)
    assert e2.value.__cause__ is None and e2.value.__suppress_context__
    assert TOKEN not in caplog.text


def test_adapter_failures_surface_without_the_token(
    source: GitHubActionsSource, target: GitHubActionsTarget, session: FakeSession
) -> None:
    session.on("GET", "/actions/runs/7001/attempts/1", FakeResponse(401, {"message": f"nope {TOKEN}"}))
    with pytest.raises(SourceError) as e:
        source.get_execution("7001.1")
    assert TOKEN not in str(e.value)
    session.on("GET", "/actions/runs/7001", FakeResponse(401, {"message": f"nope {TOKEN}"}))
    rem = Remediation(0, "rerun_workflow", "r", True, "auto", dry_run={"execution_id": "7001.1"})
    assert TOKEN not in target.execute(rem).description


def test_load_github_config_reads_only_the_given_mapping_and_never_prints(capsys: pytest.CaptureFixture[str]) -> None:
    cfg = load_github_config({"DRIFTGATE_PLAYGROUND_REPO": REPO, "GITHUB_TOKEN": TOKEN})
    assert cfg.repo == REPO and cfg.token == TOKEN
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err and TOKEN not in repr(cfg)
    with pytest.raises(ValueError) as e:
        load_github_config({"DRIFTGATE_PLAYGROUND_REPO": REPO})
    assert TOKEN not in str(e.value)


def test_head_sha_constant_is_a_full_sha() -> None:
    assert len(HEAD_SHA) == 40
