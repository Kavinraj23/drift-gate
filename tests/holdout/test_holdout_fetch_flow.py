"""holdout-fetch pipeline: step extraction, redaction, storage, ids, idempotence, plan mode, token hygiene, skeleton."""

from __future__ import annotations

import hashlib
import importlib.util
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
    SHA,
    TOKEN,
    FakeTransport,
    expected_section,
    failed_job,
    js,
    log_text,
    ok_job,
    routes_for,
    step,
)

from driftgate.eval import holdout as hd
from driftgate.eval import holdout_fetch as hf
from driftgate.eval import holdout_skeleton as sk
from driftgate.eval import holdout_steplog as sl
from driftgate.eval.holdout_gh import HttpResponse

ROOT = Path(__file__).resolve().parents[2]
CLOCK = lambda: datetime(2026, 10, 14, 23, 59, tzinfo=UTC)  # noqa: E731
SECRET_LINE = "GITHUB_TOKEN=ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
_spec = importlib.util.spec_from_file_location("tasks_n1f", ROOT / "tasks.py")
tasks = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(tasks)  # type: ignore[union-attr]


def write_candidates(root: Path, *entries: dict) -> Path:  # type: ignore[type-arg]
    path = root / "candidates.json"
    path.write_text(json.dumps({"schema_version": 1, "candidates": list(entries)}), encoding="utf-8")
    return path


def fetch(root: Path, transport: FakeTransport, *args: str, token: str | None = TOKEN) -> tuple[int, str]:
    out = io.StringIO()
    code = hf.run_fetch(
        list(args),
        root=root,
        out=out,
        err=io.StringIO(),
        transport_factory=lambda: transport,
        token_provider=lambda: token,
        clock=CLOCK,
    )
    return code, out.getvalue()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "holdout"
    r.mkdir()
    return r


# -- step extraction ----------------------------------------
def _failed_step() -> sl.FailedStep:
    return sl.first_failed_step(failed_job())


def test_only_the_failed_step_is_extracted_and_neighbours_sharing_a_second_are_trimmed() -> None:
    assert sl.extract_step_section(log_text(), _failed_step()).text == expected_section()
    assert "Set up job" not in expected_section() and "Post Run" not in expected_section()


def test_crlf_endings_and_a_bom_are_kept_or_removed_exactly_as_documented() -> None:
    raw = ("﻿" + log_text("\r\n")).encode("utf-8")
    text = sl.decode_log(raw)
    assert not text.startswith("﻿") and text == log_text("\r\n")
    assert sl.extract_step_section(text, _failed_step()).text == expected_section("\r\n")
    assert sl.prepare_for_storage(expected_section("\r\n")) == expected_section("\r\n")


def test_invalid_utf8_is_an_error_not_a_silent_replacement() -> None:
    with pytest.raises(sl.StepLogError, match="UTF-8"):
        sl.decode_log(b"\xff\xfe\x00bad")


def test_ansi_is_stripped_and_nothing_else_changes() -> None:
    colored = expected_section().replace(RUNNER_ERROR, "\x1b[31;1m" + RUNNER_ERROR + "\x1b[0m")
    assert sl.prepare_for_storage(colored) == expected_section()


def test_a_credential_shaped_string_is_redacted_and_the_result_is_stable() -> None:
    dirty = expected_section().replace(RUNNER_ERROR, SECRET_LINE + "\n" + RUNNER_ERROR)
    clean = sl.prepare_for_storage(dirty)
    assert "ghp_" not in clean and RUNNER_ERROR in clean and CATALOG_LINE in clean
    from driftgate.redaction import redact

    assert redact(clean) == clean


def test_a_private_key_block_refuses_the_case_instead_of_cutting_lines_out() -> None:
    key = "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----\n"
    with pytest.raises(sl.StepLogError, match="private key"):
        sl.prepare_for_storage(expected_section() + key)


def test_first_failed_step_edge_cases() -> None:
    twice = failed_job()
    twice["steps"].append(step("Always cleanup", 6, "failure", 7, 8))
    got = sl.first_failed_step(twice)
    assert got.number == 3 and [o.name for o in got.other_failed] == ["Always cleanup"]
    cancelled = {"steps": [step("a", 1, "cancelled", 0, 1)]}
    with pytest.raises(sl.StepLogError, match="no step"):
        sl.first_failed_step(cancelled)
    with pytest.raises(sl.StepLogError):
        sl.first_failed_step({"steps": []})
    with pytest.raises(sl.StepLogError):
        sl.first_failed_step({"steps": [{"name": "x", "number": 1, "conclusion": "failure"}]})


def test_a_window_with_no_log_lines_is_an_error() -> None:
    far = sl.FailedStep("x", 1, datetime(2030, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, 0, 0, 5, tzinfo=UTC), 0)
    with pytest.raises(sl.StepLogError, match="window"):
        sl.extract_step_section(log_text(), far)


# -- candidates file ----------------------------------------
@pytest.mark.parametrize(
    "doc",
    [
        {"candidates": []},
        {"schema_version": 2, "candidates": []},
        {"schema_version": 1, "candidates": [{"repo": "../x/y", "run_id": 1}]},
        {"schema_version": 1, "candidates": [{"repo": "a/b", "run_id": "1"}]},
        {"schema_version": 1, "candidates": [{"repo": "a/b", "run_id": 0}]},
        {"schema_version": 1, "candidates": [{"repo": "a/b", "run_id": 1, "extra": 1}]},
        {"schema_version": 1, "candidates": [{"repo": "a/b", "run_id": 1}, {"repo": "A/b", "run_id": 1}]},
        {"schema_version": 1, "candidates": [{"repo": "a/b", "run_id": 1}, {"repo": "a/b", "run_id": 1}]},
        {"schema_version": 1, "candidates": [{"repo": "a/b", "run_id": 1, "job": ""}]},
    ],
)
def test_a_bad_candidates_file_is_rejected_before_any_network_call(root: Path, doc: dict) -> None:  # type: ignore[type-arg]
    (root / "candidates.json").write_text(json.dumps(doc), encoding="utf-8")
    t = FakeTransport({})
    code, out = fetch(root, t, "--yes-fetch")
    # `A/b` vs `a/b` is a case-only difference of one repo, flagged as a duplicate by the case-insensitive key
    assert code == 2 and "cannot plan" in out and t.calls == []


def test_a_missing_candidates_file_says_how_to_create_it(root: Path) -> None:
    code, out = fetch(root, FakeTransport({}), "--yes-fetch")
    assert code == 2 and "not found" in out and "schema_version" in out


# -- plan mode and flags ----------------------------------------
def test_plan_mode_makes_zero_requests_needs_no_token_and_writes_nothing(root: Path) -> None:
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID}, {"repo": REPO, "run_id": 5002, "job": "build"})
    t = FakeTransport({})

    def no_token() -> str:
        raise AssertionError("plan mode must not look for the token")

    out = io.StringIO()
    code = hf.run_fetch(
        [],
        root=root,
        out=out,
        err=io.StringIO(),
        transport_factory=lambda: pytest.fail("transport built"),
        token_provider=no_token,
        clock=CLOCK,
    )
    text = out.getvalue()
    assert code == 2 and t.calls == []
    assert "hc-01" in text and "hc-02" in text and "8 request(s)" in text and "pass --yes-fetch" in text
    assert sorted(p.name for p in root.iterdir()) == ["candidates.json"]


def test_yes_fetch_is_required_and_unknown_options_are_rejected(root: Path) -> None:
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID})
    t = FakeTransport(routes_for())
    assert fetch(root, t)[0] == 2 and t.calls == []
    for bad in ("--yes", "--dir=/tmp/x", "--yes-fetc", "--force"):
        with pytest.raises(SystemExit) as exc:
            hf.run_fetch([bad], root=root, out=io.StringIO(), err=io.StringIO())
        assert exc.value.code == 2
    assert t.calls == []


def test_a_missing_token_stops_before_any_request(root: Path) -> None:
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID})
    t = FakeTransport(routes_for())
    code, out = fetch(root, t, "--yes-fetch", token=None)
    assert code == 2 and "HOLDOUT_FETCH_TOKEN is not set" in out and t.calls == []


# -- a real fetch (stubbed) ----------------------------------------
def test_fetch_stores_the_log_and_metadata_and_the_hash_validates(root: Path) -> None:
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID})
    t = FakeTransport(routes_for())
    code, out = fetch(root, t, "--yes-fetch")
    assert code == 0 and "hc-01  stored" in out and "4 request(s) made" in out
    data = (root / "logs" / "hc-01.log").read_bytes()
    assert data.decode("utf-8") == expected_section()
    rec = json.loads((root / "fetched" / "hc-01.json").read_text(encoding="utf-8"))
    assert rec["source"] == {
        "repo": REPO,
        "run_id": RUN_ID,
        "job": "build",
        "commit_sha": SHA,
        "url": f"https://github.com/{REPO}/actions/runs/{RUN_ID}",
        "fetch_date": "2026-10-14",
    }
    assert rec["log_sha256"] == hashlib.sha256(data).hexdigest() == hd.sha256_bytes(data)
    assert rec["log_bytes"] == len(data) and (rec["step"]["name"], rec["step"]["number"]) == ("Pull image", 3)
    assert rec["job_id"] == JOB_ID and rec["run_attempt"] == 1 and rec["other_failed_steps"] == []
    assert sorted(p.name for p in root.iterdir()) == ["candidates.json", "fetched", "logs"]  # never labels.json


def test_the_stored_log_passes_holdout_validate_once_labelled(root: Path) -> None:
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID})
    fetch(root, FakeTransport(routes_for()), "--yes-fetch")
    skel, _, problems = sk.build_skeleton(root)
    assert problems == []
    case = skel[0]
    case.update(
        split="dev",
        layer="L2",
        classification="platform",
        correct_action={"kind": "rerun"},
        rationale="structure-only test label",
        labeled_at="2026-10-15T09:00:00Z",
    )
    (root / "labels.json").write_text(json.dumps({"schema_version": 1, "cases": [case]}), encoding="utf-8")
    report = hd.validate(root, now=datetime(2026, 10, 20, tzinfo=UTC))
    assert report.problems == ()  # schema, sha256, redaction fixed point, label timing all hold


def test_a_credential_in_the_log_never_lands_in_any_stored_file(root: Path) -> None:
    dirty = log_text().replace(RUNNER_ERROR, SECRET_LINE + "\n2026-10-01T10:00:06.0000000Z " + RUNNER_ERROR)
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID})
    code, out = fetch(root, FakeTransport(routes_for(log=dirty.encode("utf-8"))), "--yes-fetch")
    assert code == 0
    for p in root.rglob("*"):
        if p.is_file():
            assert "ghp_" not in p.read_text(encoding="utf-8") and "a1B2c3D4" not in p.read_text(encoding="utf-8")
    assert "a1B2c3D4" not in out


def test_the_token_and_the_signed_url_never_appear_in_output_or_stored_files(root: Path) -> None:
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID})
    _, out = fetch(root, FakeTransport(routes_for()), "--yes-fetch")
    assert TOKEN not in out and "SIGNED-URL-SECRET" not in out
    for p in root.rglob("*"):
        if p.is_file():
            body = p.read_text(encoding="utf-8")
            assert TOKEN not in body and "SIGNED-URL-SECRET" not in body


def test_exceptions_carrying_the_token_are_not_printed(root: Path) -> None:
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID})

    class Boom:
        def get(self, url, headers, *, timeout, max_bytes):  # type: ignore[no-untyped-def]
            raise RuntimeError(f"connection failed for {headers['Authorization']}")

    out = io.StringIO()
    code = hf.run_fetch(
        ["--yes-fetch"],
        root=root,
        out=out,
        err=io.StringIO(),
        transport_factory=Boom,
        token_provider=lambda: TOKEN,
        clock=CLOCK,
    )
    assert code == 1 and TOKEN not in out.getvalue() and "RuntimeError" in out.getvalue()


def test_labels_json_is_never_written_or_modified(root: Path) -> None:
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID})
    fetch(root, FakeTransport(routes_for()), "--yes-fetch")
    assert not (root / "labels.json").exists()
    labels = b'{"schema_version": 1, "cases": []}\n'
    (root / "labels.json").write_bytes(labels)
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID}, {"repo": REPO, "run_id": 5002})
    fetch(root, FakeTransport({**routes_for(), **routes_for(run_id=5002)}), "--yes-fetch")
    assert (root / "labels.json").read_bytes() == labels


# -- ids, duplicates, overwrite ----------------------------------------
def test_ids_continue_after_the_highest_used_in_labels_logs_and_fetched(root: Path) -> None:
    (root / "logs").mkdir()
    (root / "fetched").mkdir()
    (root / "labels.json").write_text(
        json.dumps(
            {"schema_version": 1, "cases": [{"id": "hc-05", "source": {"repo": "x/y", "run_id": 1, "job": "j"}}]}
        ),
        encoding="utf-8",
    )
    (root / "logs" / "hc-07.log").write_bytes(b"x")
    (root / "fetched" / "hc-08.json").write_text("{}", encoding="utf-8")
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID}, {"repo": REPO, "run_id": 5002})
    rows = hf.make_plan(hf.load_candidates(root / "candidates.json"), hf.Store(root))
    assert [r.case_id for r in rows] == ["hc-09", "hc-10"]


def test_a_second_run_refuses_what_is_already_fetched_and_writes_nothing(root: Path) -> None:
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID, "job": "build"})
    fetch(root, FakeTransport(routes_for()), "--yes-fetch")
    snapshot = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    t = FakeTransport(routes_for())
    code, out = fetch(root, t, "--yes-fetch")
    assert code == 0 and "REFUSED" in out and "already fetched as hc-01" in out and t.calls == []
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == snapshot


def test_an_unnamed_job_is_recognised_as_a_duplicate_before_the_log_is_downloaded(root: Path) -> None:
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID})
    fetch(root, FakeTransport(routes_for()), "--yes-fetch")
    t = FakeTransport(routes_for())
    code, out = fetch(root, t, "--yes-fetch")
    assert code == 0 and "failed job is already fetched as hc-01" in out
    assert len(t.calls) == 2  # run + jobs only; no log request
    assert sorted(p.name for p in (root / "logs").iterdir()) == ["hc-01.log"]


def test_write_new_never_overwrites_different_content(root: Path) -> None:
    store = hf.Store(root)
    path = root / "logs" / "hc-01.log"
    store.write_new(path, b"one")
    store.write_new(path, b"one")  # identical content is fine
    with pytest.raises(hf.FetchError, match="different content"):
        store.write_new(path, b"two")
    assert path.read_bytes() == b"one"


def test_a_failure_on_one_candidate_does_not_stop_the_others(root: Path) -> None:
    jobs = [failed_job(9001, "build"), failed_job(9002, "test")]
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID}, {"repo": REPO, "run_id": 5002})
    routes = {**routes_for(jobs=jobs), **routes_for(run_id=5002)}
    code, out = fetch(root, FakeTransport(routes), "--yes-fetch")
    assert code == 1 and "FAILED" in out and "2 jobs failed" in out and "name one" in out
    assert (root / "logs" / "hc-02.log").is_file() and not (root / "logs" / "hc-01.log").exists()


def test_a_named_job_picks_that_job_in_a_matrix(root: Path) -> None:
    jobs = [failed_job(9001, "build (a)"), failed_job(9002, "build (b)")]
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID, "job": "build (b)"})
    code, _ = fetch(root, FakeTransport(routes_for(jobs=jobs)), "--yes-fetch")
    rec = json.loads((root / "fetched" / "hc-01.json").read_text(encoding="utf-8"))
    assert code == 0 and rec["source"]["job"] == "build (b)" and rec["job_id"] == 9002


@pytest.mark.parametrize(
    ("run", "message"),
    [
        ({"conclusion": "success"}, "not completed/failure"),
        ({"status": "in_progress"}, "not completed/failure"),
        ({"head_sha": "short"}, "head_sha"),
        ({"html_url": "https://github.com/other/repo/actions/runs/1"}, "html_url"),
    ],
)
def test_bad_run_metadata_is_refused(root: Path, run: dict, message: str) -> None:  # type: ignore[type-arg]
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID})
    routes = routes_for()
    from fetch_helpers import run_doc

    routes[f"{API}/repos/{REPO}/actions/runs/{RUN_ID}"] = js(run_doc(**run))
    code, out = fetch(root, FakeTransport(routes), "--yes-fetch")
    assert code == 1 and message in out and not (root / "logs").exists()


def test_a_job_that_did_not_fail_is_refused(root: Path) -> None:
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID, "job": "lint"})
    code, out = fetch(root, FakeTransport(routes_for(jobs=[ok_job(), failed_job()])), "--yes-fetch")
    assert code == 1 and "did not conclude 'failure'" in out


def test_expired_logs_are_reported_clearly(root: Path) -> None:
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID})
    routes = routes_for()
    routes[f"{API}/repos/{REPO}/actions/jobs/{JOB_ID}/logs"] = HttpResponse(410, {}, b"")
    code, out = fetch(root, FakeTransport(routes), "--yes-fetch")
    assert code == 1 and "HTTP 410" in out and not (root / "logs").exists()


def test_the_rate_limit_stops_the_batch_keeps_what_was_stored_and_does_not_retry(root: Path) -> None:
    write_candidates(
        root, {"repo": REPO, "run_id": RUN_ID}, {"repo": REPO, "run_id": 5002}, {"repo": REPO, "run_id": 5003}
    )
    routes = {**routes_for(), **routes_for(run_id=5002)}
    routes[f"{API}/repos/{REPO}/actions/runs/5002"] = HttpResponse(429, {"retry-after": "120"}, b"")
    t = FakeTransport(routes)
    code, out = fetch(root, t, "--yes-fetch")
    assert code == 1 and "STOPPED" in out and "rate limit" in out and "retry after 120s" in out
    assert (root / "logs" / "hc-01.log").is_file() and not (root / "logs" / "hc-02.log").exists()
    assert not any("5003" in u for u, _ in t.calls)  # nothing after the stop
    assert sum("5002" in u for u, _ in t.calls) == 1  # and no retry


def test_fetch_date_comes_from_the_injected_clock_in_utc(root: Path) -> None:
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID})
    late = lambda: datetime(2026, 10, 14, 23, 30, tzinfo=UTC)  # noqa: E731
    out = io.StringIO()
    hf.run_fetch(
        ["--yes-fetch"],
        root=root,
        out=out,
        err=io.StringIO(),
        transport_factory=lambda: FakeTransport(routes_for()),
        token_provider=lambda: TOKEN,
        clock=late,
    )
    assert (
        json.loads((root / "fetched" / "hc-01.json").read_text(encoding="utf-8"))["source"]["fetch_date"]
        == "2026-10-14"
    )


# -- token source ----------------------------------------
def test_env_token_reads_only_its_own_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / "env-file"
    env.write_text(f"OTHER_KEY=do-not-read\n{hf.TOKEN_ENV}={TOKEN}\n", encoding="utf-8")
    monkeypatch.setattr(hf, "ENV_FILE", env)
    monkeypatch.delenv(hf.TOKEN_ENV, raising=False)
    assert hf.env_token() == TOKEN
    monkeypatch.setenv(hf.TOKEN_ENV, "from-environment")
    assert hf.env_token() == "from-environment"
    monkeypatch.delenv(hf.TOKEN_ENV)
    monkeypatch.setattr(hf, "ENV_FILE", tmp_path / "missing")
    assert hf.env_token() is None


# -- skeleton ----------------------------------------
def test_the_skeleton_prints_metadata_only_and_fails_validation_until_filled(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID})
    fetch(root, FakeTransport(routes_for()), "--yes-fetch")
    out, err = io.StringIO(), io.StringIO()
    assert hf.run_fetch(["--skeleton"], root=root, out=out, err=err) == 0
    everything = out.getvalue() + err.getvalue()
    assert (
        "toomanyrequests" not in everything and CATALOG_LINE[:30] not in everything and RUNNER_ERROR not in everything
    )
    assert (
        "hc-01" in err.getvalue()
        and REPO in err.getvalue()
        and "Pull image" in err.getvalue()
        and "bytes" in err.getvalue()
    )
    cases = json.loads(out.getvalue())
    assert cases[0]["id"] == "hc-01" and cases[0]["split"] == sk.PLACEHOLDER
    (root / "labels.json").write_text(json.dumps({"schema_version": 1, "cases": cases}), encoding="utf-8")
    report = hd.validate(root, now=datetime(2026, 10, 20, tzinfo=UTC))
    assert report.problems  # every placeholder field is rejected
    for field in ("split", "layer", "classification", "correct_action", "rationale", "labeled_at"):
        broken = json.loads(json.dumps(cases))
        # fill every field except one: still invalid
        broken[0].update(
            split="dev",
            layer="L1",
            classification="user",
            correct_action={"kind": "escalate"},
            rationale="r",
            labeled_at="2026-10-15T09:00:00Z",
        )
        broken[0][field] = {"correct_action": {"kind": sk.PLACEHOLDER}, "rationale": ""}.get(field, sk.PLACEHOLDER)
        (root / "labels.json").write_text(json.dumps({"schema_version": 1, "cases": broken}), encoding="utf-8")
        assert hd.validate(root, now=datetime(2026, 10, 20, tzinfo=UTC)).problems, field


def test_the_skeleton_skips_labelled_cases_and_reports_a_tampered_log(root: Path) -> None:
    write_candidates(root, {"repo": REPO, "run_id": RUN_ID}, {"repo": REPO, "run_id": 5002})
    fetch(root, FakeTransport({**routes_for(), **routes_for(run_id=5002)}), "--yes-fetch")
    (root / "labels.json").write_text(json.dumps({"schema_version": 1, "cases": [{"id": "hc-01"}]}), encoding="utf-8")
    cases, _, problems = sk.build_skeleton(root)
    assert [c["id"] for c in cases] == ["hc-02"] and problems == []
    (root / "logs" / "hc-02.log").write_bytes(b"tampered")
    assert sk.build_skeleton(root)[2]


# -- tasks wiring ----------------------------------------
def test_holdout_fetch_is_human_only_and_refuses_under_autonomous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "holdout-fetch" in tasks.HUMAN_ONLY and "holdout-fetch" not in tasks.NOT_IMPLEMENTED
    (tmp_path / ".autonomous").write_text("")
    monkeypatch.setattr(tasks, "ROOT", tmp_path)
    called: list[list[str]] = []
    monkeypatch.setattr(tasks, "holdout_fetch", lambda extra: called.append(extra) or 0)
    assert tasks.main(["tasks.py", "holdout-fetch", "--yes-fetch"]) == 1
    assert "human-only" in capsys.readouterr().out and called == []


def test_holdout_fetch_forwards_its_options_and_only_it_among_these(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tasks, "ROOT", tmp_path)
    seen: list[list[str]] = []
    monkeypatch.setattr(tasks, "holdout_fetch", lambda extra: seen.append(extra) or 0)
    assert (
        tasks.main(["tasks.py", "holdout-fetch"]) == 0 and tasks.main(["tasks.py", "holdout-fetch", "--skeleton"]) == 0
    )
    assert seen == [[], ["--skeleton"]]
    assert tasks.main(["tasks.py", "holdout-validate", "--yes-fetch"]) == 2  # other targets still take no options


def test_the_task_runs_the_fetch_module_without_an_injected_directory_or_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        tasks.subprocess, "run", lambda cmd, **k: calls.append(cmd) or type("R", (), {"returncode": 0})()
    )
    assert tasks.holdout_fetch([]) == 0
    assert calls[0][-2:] == ["-m", "driftgate.eval.holdout_fetch"]
