"""holdout-fetch round 2: error-anchored extraction with refusals, the verification fields, and hardening checks.

Log text here is only (a) the verified `docker_pull_rate_limit` catalog lines, (b) the runner's own
`##[error]Process completed with exit code 1.` wording and (c) STRUCTURAL SCAFFOLDING: timestamps, `##[group]` /
`##[endgroup]` framing and `Run ...` header titles (see fetch_helpers.py). Nothing here is invented error text.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fetch_helpers import (
    API,
    CATALOG_LINE,
    JOB_ID,
    REPO,
    RUN_ID,
    RUNNER_ERROR,
    TOKEN,
    FakeTransport,
    failed_job,
    js,
    log_text,
    routes_for,
    run_doc,
    step,
)

from driftgate.eval import holdout_fetch as hf
from driftgate.eval import holdout_gh as gh
from driftgate.eval import holdout_skeleton as sk
from driftgate.eval import holdout_steplog as sl

CLOCK = lambda: datetime(2026, 10, 14, 23, 59, tzinfo=UTC)  # noqa: E731


def L(sec: float, text: str, nl: str = "\n") -> str:
    whole = int(sec)
    frac = f"{round((sec - whole) * 10):01d}000000"
    return f"2026-10-01T10:00:{whole:02d}.{frac}Z {text}{nl}"


def job_of(*steps: dict) -> dict:  # type: ignore[type-arg]
    return {"id": JOB_ID, "name": "build", "conclusion": "failure", "steps": list(steps)}


def extract(log: str, job: dict) -> sl.Extraction:  # type: ignore[type-arg]
    return sl.extract_step_section(log, sl.first_failed_step(job))


def refused(log: str, job: dict, match: str) -> None:  # type: ignore[type-arg]
    with pytest.raises(sl.StepLogError, match=match):
        extract(log, job)


G = "##[group]"
EG = "##[endgroup]"
FAILED_JOB = job_of(
    step("Set up job", 1, "success", 0, 2),
    step("Prev", 2, "success", 3, 4),
    step("Pull image", 3, "failure", 4, 6),
    step("Next", 4, "success", 6, 7),
)


# -- adversarial layouts ----------------------------------------
def test_standard_layout_is_cut_from_its_header_to_the_runner_error_with_positions() -> None:
    ext = extract(log_text(), failed_job())
    lines = sl.split_lines(log_text())
    assert ext.text == "".join(lines[ext.start_line - 1 : ext.end_line])
    assert lines[ext.start_line - 1].endswith(G + "Run docker pull example\n")
    assert lines[ext.error_line - 1].rstrip().endswith(RUNNER_ERROR) and ext.error_line == ext.end_line
    assert ext.job_log_lines == len(lines) and ext.method == "error-anchor-v1"
    assert ext.error_timestamp == "2026-10-01T10:00:06.0000000Z" and ext.inner_group_lines == 0


def test_a_fast_passing_neighbour_with_its_own_header_in_the_same_second_before_the_failed_step_is_refused() -> None:
    log = (
        L(3, G + "Run prev")
        + L(4.1, G + "Run neighbour in the same second")
        + L(4.2, EG)
        + L(4.5, G + "Run docker pull example")
        + L(4.6, EG)
        + L(5, CATALOG_LINE)
        + L(6, RUNNER_ERROR)
    )
    refused(log, FAILED_JOB, "2 step headers")


def test_a_neighbour_after_the_failed_step_is_not_included() -> None:
    log = (
        L(4, G + "Run docker pull example")
        + L(4, EG)
        + L(5, CATALOG_LINE)
        + L(6, RUNNER_ERROR)
        + L(6, G + "Run next in the same second")
        + L(6.5, "output of the next step")
    )
    ext = extract(log, FAILED_JOB)
    assert "next" not in ext.text and ext.text.endswith(RUNNER_ERROR + "\n")


def test_a_following_step_that_also_errors_in_the_window_is_refused() -> None:
    log = (
        L(4, G + "Run docker pull example")
        + L(5, CATALOG_LINE)
        + L(6, RUNNER_ERROR)
        + L(6.2, G + "Run next (continue-on-error)")
        + L(6.6, RUNNER_ERROR)
    )
    refused(log, FAILED_JOB, "2 step headers")


def test_two_adjacent_failing_steps_are_refused() -> None:
    job = job_of(step("One", 1, "failure", 4, 6), step("Two", 2, "failure", 6, 7))
    log = L(4, G + "Run one") + L(6.2, RUNNER_ERROR) + L(6.5, G + "Run two") + L(6.9, RUNNER_ERROR)
    refused(log, job, "step headers|another step header")


def test_two_failing_steps_far_apart_store_the_first_and_name_the_other() -> None:
    job = job_of(step("One", 1, "failure", 4, 5), step("Cleanup", 2, "failure", 8, 9))
    log = L(4, G + "Run one") + L(5.1, RUNNER_ERROR) + L(8, G + "Run cleanup") + L(8.5, RUNNER_ERROR)
    ext = extract(log, job)
    assert ext.text.count("##[error]") == 1 and "cleanup" not in ext.text
    assert [o.name for o in sl.first_failed_step(job).other_failed] == ["Cleanup"]


def test_another_failed_step_whose_window_contains_the_error_is_refused() -> None:
    job = job_of(step("One", 1, "failure", 4, 5), step("Other", 2, "failure", 5, 6))
    log = L(4, G + "Run one") + L(5.3, RUNNER_ERROR)
    refused(log, job, "also contains the error line")


@pytest.mark.parametrize("name", ["Set up job", "Initialize containers", "Complete job", "Stop containers"])
def test_a_failed_runner_phase_step_is_refused(name: str) -> None:
    job = job_of(step(name, 1, "failure", 4, 6))
    log = L(4, G + name) + L(5, CATALOG_LINE) + L(6, RUNNER_ERROR)
    refused(log, job, "runner phase")


def test_a_runner_phase_header_as_the_start_is_refused() -> None:
    log = L(4, G + "Set up job") + L(6, RUNNER_ERROR)
    refused(log, FAILED_JOB, "runner phase header")


def test_a_composite_action_with_a_nested_run_header_is_refused() -> None:
    job = job_of(step("Composite", 1, "failure", 4, 8))
    log = (
        L(4, G + "Run ./composite") + L(5, G + "Run inner command") + L(5, EG) + L(6, CATALOG_LINE) + L(7, RUNNER_ERROR)
    )
    refused(log, job, "2 step headers")


def test_a_nested_header_outside_the_window_but_between_start_and_error_is_refused() -> None:
    job = job_of(step("Long", 1, "failure", 4, 6))
    log = (
        L(4, G + "Run outer")
        + "no-timestamp continuation\n"
        + G
        + "Run inner without a timestamp\n"
        + L(6, RUNNER_ERROR)
    )
    refused(log, job, "another step header sits between")


def test_a_user_group_inside_the_step_is_kept_and_counted() -> None:
    log = (
        L(4, G + "Run docker pull example")
        + L(4, EG)
        + L(5, G + "details")
        + L(5, CATALOG_LINE)
        + L(5, EG)
        + L(6, RUNNER_ERROR)
    )
    assert extract(log, FAILED_JOB).inner_group_lines == 1


def test_a_missing_error_line_is_refused() -> None:
    log = L(4, G + "Run docker pull example") + L(5, CATALOG_LINE)
    refused(log, FAILED_JOB, "no '##\\[error\\]' line")


def test_an_error_line_without_its_own_timestamp_does_not_count() -> None:
    log = L(4, G + "Run docker pull example") + RUNNER_ERROR + "\n"
    refused(log, FAILED_JOB, "no '##\\[error\\]' line")


def test_an_error_stamped_outside_the_step_window_is_refused() -> None:
    log = L(4, G + "Run docker pull example") + L(5, CATALOG_LINE) + L(9, RUNNER_ERROR)
    refused(log, FAILED_JOB, "no '##\\[error\\]' line")


def test_an_error_in_an_earlier_step_inside_the_window_is_refused() -> None:
    log = L(4.1, RUNNER_ERROR) + L(4.5, G + "Run docker pull example") + L(6, RUNNER_ERROR)
    refused(log, FAILED_JOB, "before the step header")


def test_output_after_the_error_that_is_not_a_new_step_is_refused() -> None:
    log = L(4, G + "Run docker pull example") + L(6, RUNNER_ERROR) + L(6.5, CATALOG_LINE)
    refused(log, FAILED_JOB, "output continues")


def test_a_header_stamped_before_the_window_is_refused_not_guessed() -> None:
    log = L(3, G + "Run docker pull example") + L(5, CATALOG_LINE) + L(6, RUNNER_ERROR)
    refused(log, FAILED_JOB, "no step header")


def test_line_numbers_count_newlines_only_not_unicode_separators() -> None:
    log = L(3, "a b\x0cc") + L(4, G + "Run docker pull example") + L(6, RUNNER_ERROR)
    ext = extract(log, FAILED_JOB)
    assert ext.start_line == 2 and ext.job_log_lines == 3


# -- fetched record and stdout ----------------------------------------
@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "holdout"
    r.mkdir()
    return r


def fetch(root: Path, transport: FakeTransport, *args: str, **kw: object) -> tuple[int, str]:
    out = io.StringIO()
    code = hf.run_fetch(
        list(args),
        root=root,
        out=out,
        err=io.StringIO(),
        transport_factory=lambda: transport,
        token_provider=lambda: TOKEN,
        clock=CLOCK,
        autonomous=lambda: False,
        **kw,  # type: ignore[arg-type]
    )
    return code, out.getvalue()


def candidates(root: Path) -> None:
    (root / "candidates.json").write_text(
        json.dumps({"schema_version": 1, "candidates": [{"repo": REPO, "run_id": RUN_ID}]}), encoding="utf-8"
    )


def test_the_record_carries_everything_needed_to_verify_the_cut_and_no_log_text(root: Path) -> None:
    candidates(root)
    job = failed_job()
    job["steps"].append(step("Always cleanup", 6, "failure", 9, 10))
    log = log_text() + L(9, G + "Run cleanup") + L(9.5, RUNNER_ERROR)
    code, out = fetch(root, FakeTransport(routes_for(jobs=[job], log=log.encode())), "--yes-fetch")
    assert code == 0
    rec = json.loads((root / "fetched" / "hc-01.json").read_text(encoding="utf-8"))
    ex = rec["extraction"]
    lines = sl.split_lines(log)
    stored = (root / "logs" / "hc-01.log").read_text(encoding="utf-8")
    assert ex["job_log_lines"] == len(lines) and ex["method"] == "error-anchor-v1"
    assert "".join(lines[ex["start_line"] - 1 : ex["end_line"]]) == stored
    assert ex["stored_lines"] == ex["end_line"] - ex["start_line"] + 1 == sl.count_lines(stored)
    assert ex["error_line"] == ex["end_line"] and ex["error_timestamp"] == "2026-10-01T10:00:06.0000000Z"
    assert rec["log_bytes"] == len(stored.encode()) and rec["run_attempt"] == 1 and rec["job_id"] == JOB_ID
    assert rec["step"] == {
        "name": "Pull image",
        "number": 3,
        "started_at": "2026-10-01T10:00:04Z",
        "completed_at": "2026-10-01T10:00:06Z",
    }
    assert rec["other_failed_steps"] == [{"name": "Always cleanup", "number": 6}]
    assert "Always cleanup" in out and f"lines {ex['start_line']}-{ex['end_line']}" in out
    assert f"error at line {ex['error_line']}" in out and "attempt 1" in out
    assert CATALOG_LINE[:30] not in out and RUNNER_ERROR not in out  # no log text on stdout
    assert sk.build_skeleton(root)[2] == []  # the extended record validates against the schema


def test_a_refused_extraction_stores_nothing_and_says_which_case_and_why(root: Path) -> None:
    candidates(root)
    log = L(4, G + "Run docker pull example") + L(5, CATALOG_LINE)  # no error line
    code, out = fetch(root, FakeTransport(routes_for(log=log.encode())), "--yes-fetch")
    assert code == 1 and "NOT STORED" in out and f"run {RUN_ID}" in out and "by hand" in out
    assert "no '##[error]' line" in out
    assert not (root / "logs").exists() and not (root / "fetched").exists()


def test_a_job_whose_attempt_differs_from_the_run_is_refused(root: Path) -> None:
    candidates(root)
    job = failed_job()
    job["run_attempt"] = 2
    code, out = fetch(root, FakeTransport(routes_for(jobs=[job])), "--yes-fetch")
    assert code == 1 and "run_attempt differs" in out and not (root / "logs").exists()


# -- token masking (defence in depth) ----------------------------------------
def test_the_exact_token_is_replaced_in_stored_text_even_when_no_pattern_matches_it() -> None:
    odd = "plain-secret-without-a-known-shape"
    out = sl.prepare_for_storage(L(5, "value " + odd + " end"), odd)
    assert odd not in out and "[REDACTED]" in out


def test_the_exact_token_never_reaches_a_stored_file_or_the_output(root: Path) -> None:
    candidates(root)
    log = log_text().replace(CATALOG_LINE, CATALOG_LINE + "\n" + TOKEN)
    code, out = fetch(root, FakeTransport(routes_for(log=log.encode())), "--yes-fetch")
    assert code == 0 and TOKEN not in out
    assert all(TOKEN not in p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file())


# -- `.autonomous` inside the module ----------------------------------------
def test_the_module_itself_refuses_any_fetch_under_autonomous(root: Path) -> None:
    candidates(root)
    t = FakeTransport({})
    out = io.StringIO()
    code = hf.run_fetch(
        ["--yes-fetch"],
        root=root,
        out=out,
        err=io.StringIO(),
        transport_factory=lambda: pytest.fail("transport built"),
        token_provider=lambda: pytest.fail("token looked up"),
        clock=CLOCK,
        autonomous=lambda: True,
    )
    assert code == 1 and ".autonomous" in out.getvalue() and t.calls == []
    assert not (root / "logs").exists()


def test_autonomous_present_uses_the_repo_root_and_the_main_checkout_of_a_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hf, "ROOT", tmp_path)
    assert hf.autonomous_present() is False
    (tmp_path / ".autonomous").write_text("")
    assert hf.autonomous_present() is True
    main = tmp_path / "main"
    wt = main / ".claude" / "worktrees" / "w1"
    wt.mkdir(parents=True)
    monkeypatch.setattr(hf, "ROOT", wt)
    assert hf.autonomous_present() is False
    (main / ".autonomous").write_text("")
    assert hf.autonomous_present() is True


def test_main_refuses_under_autonomous_without_touching_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".autonomous").write_text("")
    monkeypatch.setattr(hf, "ROOT", tmp_path)
    monkeypatch.setattr(hf, "RequestsTransport", lambda: pytest.fail("transport built"))
    monkeypatch.setattr(hf, "env_token", lambda: pytest.fail("token read"))
    root = tmp_path / "h"
    root.mkdir()
    candidates(root)
    monkeypatch.setattr(hf, "HOLDOUT_DIR", root)
    # run_fetch's defaults were bound at definition time, so go through the real defaults explicitly
    out = io.StringIO()
    code = hf.run_fetch(["--yes-fetch"], root=root, out=out, err=io.StringIO(), autonomous=hf.autonomous_present)
    assert code == 1 and ".autonomous" in out.getvalue()


# -- .env reading ----------------------------------------
def test_env_token_parses_only_its_own_line_and_never_interpolates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / "env-file"
    env.write_text(
        "ANTHROPIC_API_KEY=sk-ant-SHOULD-NEVER-BE-SEEN\n"
        "OTHER=${HOLDOUT_FETCH_TOKEN}\n"
        f"export {hf.TOKEN_ENV} = '${{OTHER}}-literal'\n"
        "LATER=zzz-other-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hf, "ENV_FILE", env)
    monkeypatch.delenv(hf.TOKEN_ENV, raising=False)
    assert hf.env_token() == "${OTHER}-literal"  # no interpolation, quotes removed
    import gc

    got = hf.env_token()
    assert got is not None and "SHOULD-NEVER" not in got and "zzz-other" not in got
    # nothing module-level holds the other values
    assert not any("SHOULD-NEVER" in repr(v) or "zzz-other" in repr(v) for v in vars(hf).values())
    gc.collect()


def test_env_token_without_the_key_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / "env-file"
    env.write_text("ONLY_OTHER=1\n", encoding="utf-8")
    monkeypatch.setattr(hf, "ENV_FILE", env)
    monkeypatch.delenv(hf.TOKEN_ENV, raising=False)
    assert hf.env_token() is None


# -- ASCII-only host and path patterns ----------------------------------------
ARABIC = "١٢"  # Arabic-Indic digits one, two


def test_arabic_indic_digits_are_refused_in_blob_hosts_and_api_paths() -> None:
    assert not gh.blob_host_allowed(f"https://pipelinesghubeus{ARABIC}.actions.githubusercontent.com/x")
    assert not gh.blob_host_allowed(f"https://productionresultssa{ARABIC}.blob.core.windows.net/x")
    assert gh.blob_host_allowed("https://productionresultssa12.blob.core.windows.net/x")
    base = f"https://{gh.API_HOST}/repos/o/r/actions"
    for bad in (f"{base}/runs/{ARABIC}", f"{base}/runs/1{ARABIC}/jobs", f"{base}/jobs/{ARABIC}/logs"):
        with pytest.raises(gh.FetchError):
            gh._check_api_url(bad)
    gh._check_api_url(f"{base}/runs/12")
    with pytest.raises(gh.FetchError):
        gh._check_api_url(f"{base}/runs/1/jobs?per_page=1{ARABIC[0]}0")


# -- the real RequestsTransport with a fake session ----------------------------------------
class FakeResp:
    def __init__(self, status: int = 200, headers: dict | None = None, chunks: list[bytes] | None = None) -> None:  # type: ignore[type-arg]
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks if chunks is not None else [b"ok"]
        self.closed = False
        self.read = 0

    def iter_content(self, chunk_size: int):  # type: ignore[no-untyped-def]
        for c in self._chunks:
            self.read += 1
            yield c

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, script: list) -> None:  # type: ignore[type-arg]
        self.script = list(script)
        self.calls: list[tuple[str, dict, dict]] = []  # type: ignore[type-arg]
        self.trust_env = True

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def get(self, url: str, **kw: object):  # type: ignore[no-untyped-def]
        self.calls.append((url, dict(kw.get("headers", {})), kw))  # type: ignore[call-overload]
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def transport_with(session: FakeSession) -> gh.RequestsTransport:
    return gh.RequestsTransport(session_factory=lambda: session)


def test_the_real_transport_passes_no_redirects_no_env_a_timeout_and_streams() -> None:
    s = FakeSession([FakeResp(200, {"X-Thing": "v"}, [b"a", b"b"])])
    resp = transport_with(s).get(
        f"{API}/x".replace("/x", "/repos/o/r/actions/runs/1"), {"Accept": "a"}, timeout=7.0, max_bytes=100
    )
    ((_, headers, kw),) = s.calls
    assert kw["allow_redirects"] is False and kw["timeout"] == 7.0 and kw["stream"] is True
    assert s.trust_env is False and headers == {"Accept": "a"}
    assert resp.body == b"ab" and resp.headers == {"x-thing": "v"} and resp.status == 200


def test_the_size_cap_is_enforced_while_streaming_and_the_response_is_closed() -> None:
    r = FakeResp(200, {}, [b"x" * 60, b"x" * 60, b"x" * 60])
    t = transport_with(FakeSession([r]))
    with pytest.raises(gh.FetchError, match="larger"):
        t.get(f"{API}/repos/o/r/actions/runs/1", {}, timeout=1, max_bytes=100)
    assert r.read == 2 and r.closed  # stopped at the second chunk, never read the third


def test_requests_exceptions_are_wrapped_without_the_url_or_token_and_not_retried() -> None:
    import requests

    url = f"{API}/repos/o/r/actions/runs/1"
    boom = requests.ConnectionError(f"failed {url} with Bearer {TOKEN}")
    s = FakeSession([boom, FakeResp()])
    with pytest.raises(gh.FetchError) as exc:
        transport_with(s).get(url, {"Authorization": f"Bearer {TOKEN}"}, timeout=1, max_bytes=10)
    assert TOKEN not in str(exc.value) and "repos/o" not in str(exc.value) and "ConnectionError" in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__ is True
    assert len(s.calls) == 1  # one attempt only


def test_credentials_are_only_ever_sent_to_the_api_host_through_the_real_transport() -> None:
    blob = "https://pipelines.actions.githubusercontent.com/some/path?sig=S"
    session = FakeSession([FakeResp(302, {"Location": blob}, [b""]), FakeResp(200, {}, [b"LOG"])])
    client = gh.ReadOnlyGitHub(transport_with(session), TOKEN)
    assert client.get_job_log(REPO, 9001) == b"LOG"
    (api_url, api_headers, _), (blob_url, blob_headers, _) = session.calls
    assert api_url.startswith(f"https://{gh.API_HOST}/") and api_headers["Authorization"] == f"Bearer {TOKEN}"
    assert blob_url == blob and "Authorization" not in blob_headers
    # defence in depth: the transport itself refuses a credentialed request to another host
    with pytest.raises(gh.FetchError, match="credentials"):
        transport_with(FakeSession([])).get(blob, {"Authorization": "x"}, timeout=1, max_bytes=10)


def test_the_real_transport_end_to_end_with_the_client_marks_each_hop_get_only() -> None:
    session = FakeSession([FakeResp(200, {}, [json_bytes(run_doc())])])
    client = gh.ReadOnlyGitHub(transport_with(session), TOKEN)
    assert client.get_run(REPO, RUN_ID)["status"] == "completed"
    assert len(session.calls) == 1 and js is not None


def json_bytes(obj: object) -> bytes:
    return json.dumps(obj).encode("utf-8")


# -- skeleton fails closed ----------------------------------------
def test_an_unreadable_labels_file_makes_the_skeleton_refuse_before_hashing_any_log(root: Path) -> None:
    candidates(root)
    fetch(root, FakeTransport(routes_for()), "--yes-fetch")
    (root / "labels.json").write_text("{not json", encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    assert hf.run_fetch(["--skeleton"], root=root, out=out, err=err) == 2
    assert out.getvalue() == "" and "labels.json" in err.getvalue()
    for bad in ('{"cases": 5}', '{"cases": [{"no_id": 1}]}', "[]"):
        (root / "labels.json").write_text(bad, encoding="utf-8")
        with pytest.raises(sk.SkeletonError):
            sk.build_skeleton(root)
    (root / "labels.json").unlink()
    assert len(sk.build_skeleton(root)[0]) == 1  # a missing file simply means nothing is labelled yet
