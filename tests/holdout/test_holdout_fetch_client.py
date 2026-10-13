"""holdout-fetch client: GET-only, three allowed paths, redirect allow-list, caps, rate limit (stub transport)."""

from __future__ import annotations

import pytest
from fetch_helpers import API, BLOB, REPO, RUN_ID, TOKEN, FakeTransport, js, routes_for

from driftgate.eval import holdout_gh as gh
from driftgate.eval.holdout_gh import FetchError, HttpResponse, RateLimited, ReadOnlyGitHub


def client(routes=None, **kw) -> tuple[ReadOnlyGitHub, FakeTransport]:  # type: ignore[no-untyped-def]
    t = FakeTransport(routes if routes is not None else routes_for(**kw))
    return ReadOnlyGitHub(t, TOKEN), t


@pytest.mark.parametrize(
    "repo",
    [
        "../x/y",
        "owner/../etc",
        "owner/..",
        "owner/.",
        "a/b/c",
        "owner",
        "",
        "own er/repo",
        "owner/re po",
        "owner/repo?x=1",
        "owner/repo#frag",
        "owner/repo%2f..",
        "-owner/repo",
        "owner/repo\n",
        "ow/ner/repo",
        "x" * 40 + "/repo",
        "owner/" + "r" * 101,
        "owner@evil.com/repo",
    ],
)
def test_hostile_repo_names_are_rejected_before_any_request(repo: str) -> None:
    c, t = client()
    for call in (lambda: c.get_run(repo, 1), lambda: c.list_jobs(repo, 1), lambda: c.get_job_log(repo, 1)):
        with pytest.raises(FetchError):
            call()
    assert t.calls == []


@pytest.mark.parametrize("bad", [0, -1, "12", "1/../2", True, 1.5, None, 2**63, "1?x=2"])
def test_hostile_ids_are_rejected_before_any_request(bad: object) -> None:
    c, t = client()
    with pytest.raises(FetchError):
        c.get_run(REPO, bad)  # type: ignore[arg-type]
    with pytest.raises(FetchError):
        c.get_job_log(REPO, bad)  # type: ignore[arg-type]
    assert t.calls == []


def test_every_request_of_a_full_fetch_is_a_get_to_an_allowed_path() -> None:
    c, t = client()
    c.get_run(REPO, RUN_ID)
    c.list_jobs(REPO, RUN_ID)
    assert c.get_job_log(REPO, 9001)
    api_calls = [u for u, _ in t.calls if u.startswith("https://api.github.com")]
    assert len(api_calls) == 3 and len(t.calls) == 4
    for url in api_calls:
        gh._check_api_url(url)  # raises if outside the allow-list
    # the transport interface has no way to express another verb
    assert [n for n in dir(gh.Transport) if not n.startswith("_")] == ["get"]


@pytest.mark.parametrize(
    "url",
    [
        "http://api.github.com/repos/o/r/actions/runs/1",
        "https://evil.example/repos/o/r/actions/runs/1",
        "https://api.github.com@evil.example/repos/o/r/actions/runs/1",
        "https://api.github.com:8443/repos/o/r/actions/runs/1",
        "https://api.github.com/repos/o/r/actions/runs/1?x=1",
        "https://api.github.com/repos/o/r/actions/runs/1#f",
        "https://api.github.com/repos/o/r/actions/runs/1/cancel",
        "https://api.github.com/repos/o/r/actions/runs/1/rerun",
        "https://api.github.com/repos/o/r/actions/runs/1/logs",
        "https://api.github.com/repos/o/r/actions/runs/01",
        "https://api.github.com/repos/o/r/contents/README.md",
        "https://api.github.com/repos/o/../actions/runs/1",
        "https://api.github.com/repos/o/r/actions/jobs/1/logs/../../x",
        "https://api.github.com/user",
    ],
)
def test_urls_outside_the_three_paths_are_refused(url: str) -> None:
    with pytest.raises(FetchError):
        gh._check_api_url(url)


def test_the_authorization_header_goes_to_the_api_and_never_to_the_log_blob() -> None:
    c, t = client()
    c.get_job_log(REPO, 9001)
    (api_url, api_headers), (blob_url, blob_headers) = t.calls
    assert api_url.endswith("/jobs/9001/logs") and api_headers["Authorization"] == f"Bearer {TOKEN}"
    assert blob_url == BLOB
    assert "Authorization" not in blob_headers
    assert not any(TOKEN in str(v) for v in blob_headers.values())


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/log",
        "http://pipelines.actions.githubusercontent.com/log",
        "https://pipelines.actions.githubusercontent.com.evil.example/log",
        "https://evil.example/pipelines.actions.githubusercontent.com",
        "https://user:pw@pipelines.actions.githubusercontent.com/log",
        "https://pipelines.actions.githubusercontent.com:8443/log",
        "https://otheraccount.blob.core.windows.net/log",
        "https://api.github.com/repos/o/r/actions/runs/1",
        "//pipelines.actions.githubusercontent.com/log",
        "",
    ],
)
def test_a_redirect_to_a_host_outside_the_allow_list_is_refused_and_not_requested(location: str) -> None:
    routes = routes_for()
    routes[f"{API}/repos/{REPO}/actions/jobs/9001/logs"] = HttpResponse(302, {"location": location}, b"")
    c, t = client(routes)
    with pytest.raises(FetchError, match="not an allowed log host"):
        c.get_job_log(REPO, 9001)
    assert len(t.calls) == 1  # the redirect was not followed


@pytest.mark.parametrize(
    "host",
    [
        "pipelines.actions.githubusercontent.com",
        "pipelinesghubeus22.actions.githubusercontent.com",
        "productionresultssa19.blob.core.windows.net",
    ],
)
def test_allow_listed_blob_hosts_are_followed(host: str) -> None:
    assert gh.blob_host_allowed(f"https://{host}/x?sig=1")


def test_only_one_redirect_is_followed() -> None:
    routes = routes_for()
    routes[BLOB] = HttpResponse(302, {"location": BLOB}, b"")
    c, _ = client(routes)
    with pytest.raises(FetchError, match="HTTP 302"):
        c.get_job_log(REPO, 9001)


def test_the_error_for_a_bad_redirect_does_not_contain_the_signed_url() -> None:
    routes = routes_for()
    routes[f"{API}/repos/{REPO}/actions/jobs/9001/logs"] = HttpResponse(
        302, {"location": "https://evil.example/log?sig=SIGNED-URL-SECRET"}, b""
    )
    c, _ = client(routes)
    with pytest.raises(FetchError) as exc:
        c.get_job_log(REPO, 9001)
    assert "SIGNED-URL-SECRET" not in str(exc.value) and "sig=" not in str(exc.value)


def test_api_endpoints_other_than_logs_never_follow_a_redirect() -> None:
    routes = routes_for()
    routes[f"{API}/repos/{REPO}/actions/runs/{RUN_ID}"] = HttpResponse(301, {"location": "https://evil.example/"}, b"")
    c, t = client(routes)
    with pytest.raises(FetchError, match="redirect"):
        c.get_run(REPO, RUN_ID)
    assert len(t.calls) == 1


def test_oversized_json_and_log_responses_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = client()
    monkeypatch.setattr(gh, "MAX_JSON_BYTES", 10)
    with pytest.raises(FetchError, match="larger"):
        c.get_run(REPO, RUN_ID)
    monkeypatch.setattr(gh, "MAX_LOG_BYTES", 10)
    with pytest.raises(FetchError, match="larger"):
        c.get_job_log(REPO, 9001)


@pytest.mark.parametrize(
    "resp",
    [
        HttpResponse(429, {"retry-after": "30"}, b""),
        HttpResponse(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1790000000"}, b""),
        HttpResponse(403, {"retry-after": "60"}, b"{}"),
        HttpResponse(403, {}, b'{"message": "API rate limit exceeded for user"}'),
    ],
)
def test_rate_limit_stops_with_a_clear_message_and_no_retry(resp: HttpResponse) -> None:
    routes = routes_for()
    routes[f"{API}/repos/{REPO}/actions/runs/{RUN_ID}"] = resp
    c, t = client(routes)
    with pytest.raises(RateLimited, match="rate limit"):
        c.get_run(REPO, RUN_ID)
    assert len(t.calls) == 1


def test_the_rate_limit_message_gives_the_reset_time() -> None:
    routes = routes_for()
    routes[f"{API}/repos/{REPO}/actions/runs/{RUN_ID}"] = HttpResponse(
        403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1790000000"}, b""
    )
    c, _ = client(routes)
    with pytest.raises(RateLimited, match=r"resets at 2026-09-2\d \d\d:\d\d:\d\dZ"):
        c.get_run(REPO, RUN_ID)


def test_a_plain_403_is_not_a_rate_limit_and_the_body_is_not_echoed() -> None:
    routes = routes_for()
    routes[f"{API}/repos/{REPO}/actions/runs/{RUN_ID}"] = HttpResponse(403, {}, b"BODY-TEXT-NOT-TO-ECHO")
    c, _ = client(routes)
    with pytest.raises(FetchError) as exc:
        c.get_run(REPO, RUN_ID)
    assert not isinstance(exc.value, RateLimited) and "BODY-TEXT" not in str(exc.value) and "403" in str(exc.value)


@pytest.mark.parametrize("token", ["", " ", "a b", "tok\nen", "tok\r\n", "tökén", "x\ty"])
def test_a_malformed_token_is_refused_without_echoing_it(token: str) -> None:
    with pytest.raises(FetchError) as exc:
        ReadOnlyGitHub(FakeTransport({}), token)
    assert token.strip() == "" or token not in str(exc.value)


def test_jobs_are_paged_up_to_a_bound() -> None:
    jobs = [{"id": i + 1, "name": f"j{i}", "conclusion": "success", "steps": []} for i in range(100)]
    base = f"{API}/repos/{REPO}/actions/runs/{RUN_ID}/jobs?filter=latest&per_page=100"
    routes = {
        base: js({"total_count": 101, "jobs": jobs}),
        base + "&page=2": js({"total_count": 101, "jobs": [{"id": 500, "name": "last", "conclusion": "failure"}]}),
    }
    c, t = client(routes)
    got = c.list_jobs(REPO, RUN_ID)
    assert len(got) == 101 and len(t.calls) == 2
    endless = {base + (f"&page={p}" if p > 1 else ""): js({"total_count": 999, "jobs": jobs}) for p in range(1, 7)}
    c2, _ = client(endless)
    with pytest.raises(FetchError, match="more than"):
        c2.list_jobs(REPO, RUN_ID)


def test_json_and_shape_errors_are_clean() -> None:
    routes = routes_for()
    routes[f"{API}/repos/{REPO}/actions/runs/{RUN_ID}"] = HttpResponse(200, {}, b"not json{")
    c, _ = client(routes)
    with pytest.raises(FetchError, match="invalid JSON"):
        c.get_run(REPO, RUN_ID)
    routes[f"{API}/repos/{REPO}/actions/runs/{RUN_ID}"] = js([1, 2])
    with pytest.raises(FetchError, match="shape"):
        client(routes)[0].get_run(REPO, RUN_ID)
