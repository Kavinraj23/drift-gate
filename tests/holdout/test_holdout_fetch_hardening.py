"""Round-3 hardening of holdout-fetch: explicit step choice, framing refusals, token file edge cases, consistency.

Log text here is the verified catalog line plus the runner's own `##[error]Process completed with exit code 1.`;
everything else (timestamps, `##[group]` framing, `Post job cleanup`) is marked scaffolding (invariant 9).
"""

from __future__ import annotations

import io
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fetch_helpers import (
    CATALOG_LINE,
    JOB_ID,
    REPO,
    RUN_ID,
    RUNNER_ERROR,
    TOKEN,
    FakeTransport,
    failed_job,
    routes_for,
    step,
)

from driftgate.eval import holdout_fetch as hf
from driftgate.eval import holdout_gh as gh
from driftgate.eval import holdout_skeleton as sk
from driftgate.eval import holdout_steplog as sl

G = "##[group]"
EG = "##[endgroup]"
CLOCK = lambda: datetime(2026, 10, 14, 23, 59, tzinfo=UTC)  # noqa: E731


def L(sec: float, text: str, nl: str = "\n") -> str:  # scaffolding timestamp
    whole = int(sec)
    return f"2026-10-01T10:00:{whole:02d}.{round((sec - whole) * 10):01d}000000Z {text}{nl}"


def job_of(*steps: dict) -> dict:  # type: ignore[type-arg]
    return {"id": JOB_ID, "name": "build", "conclusion": "failure", "steps": list(steps)}


def extract(log: str, job: dict, step_sel: str | int | None = None) -> sl.Extraction:  # type: ignore[type-arg]
    return sl.extract_step_section(log, sl.first_failed_step(job, step_sel))


def refused(log: str, job: dict, match: str, step_sel: str | int | None = None) -> None:  # type: ignore[type-arg]
    with pytest.raises(sl.StepLogError, match=match):
        extract(log, job, step_sel)


FAILED = job_of(
    step("Set up job", 1, "success", 0, 2),
    step("Prev", 2, "success", 3, 4),
    step("Pull image", 3, "failure", 4, 6),
    step("Next", 4, "success", 6, 7),
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "holdout"
    r.mkdir()
    return r


def write_candidates(root: Path, **extra: object) -> None:
    cand = {"repo": REPO, "run_id": RUN_ID, **extra}
    (root / "candidates.json").write_text(json.dumps({"schema_version": 1, "candidates": [cand]}), encoding="utf-8")


def fetch(root: Path, transport: FakeTransport, *args: str) -> tuple[int, str]:
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
    )
    return code, out.getvalue()


# -- item 2: which failed step ----------------------------------------
COE_JOB = job_of(
    step("Set up job", 1, "success", 0, 2),
    step("Lint (continue-on-error)", 2, "failure", 4, 5),
    step("Pull image", 3, "failure", 8, 9),
)
COE_LOG = (
    L(4, G + "Run lint")
    + L(5.1, RUNNER_ERROR)
    + L(8, G + "Run docker pull example")
    + L(8.5, EG)
    + L(9, CATALOG_LINE)
    + L(9.1, RUNNER_ERROR)
)


def test_two_failed_steps_without_step_are_refused_and_the_message_lists_them() -> None:
    with pytest.raises(sl.StepLogError) as e:
        sl.first_failed_step(COE_JOB)
    msg = str(e.value)
    assert "#2" in msg and "Lint (continue-on-error)" in msg and "#3" in msg and "Pull image" in msg and '"step"' in msg


def test_a_named_step_is_stored_by_name_or_by_number_and_the_other_is_listed() -> None:
    for sel in ("Pull image", 3):
        ext = extract(COE_LOG, COE_JOB, sel)
        assert ext.text.startswith(L(8, G + "Run docker pull example")) and CATALOG_LINE in ext.text
        assert "lint" not in ext.text and ext.error_timestamp == "2026-10-01T10:00:09.1000000Z"
        assert [o.name for o in sl.first_failed_step(COE_JOB, sel).other_failed] == ["Lint (continue-on-error)"]


def test_the_early_continue_on_error_step_can_be_chosen_explicitly_and_still_needs_its_anchor() -> None:
    ext = extract(COE_LOG, COE_JOB, "Lint (continue-on-error)")
    assert ext.text.endswith(RUNNER_ERROR + "\n") and ext.start_line == 1 and ext.error_line == 2
    no_anchor = L(4, G + "Run lint") + L(8, G + "Run docker pull example") + L(9.1, RUNNER_ERROR)
    refused(no_anchor, COE_JOB, "no '##\\[error\\]'", "Lint (continue-on-error)")


@pytest.mark.parametrize("sel", ["Set up job", "No such step", 1, 99, "pull image"])
def test_a_step_that_did_not_fail_or_does_not_exist_is_refused(sel: str | int) -> None:
    with pytest.raises(sl.StepLogError, match="not exactly one of the failed steps"):
        sl.first_failed_step(COE_JOB, sel)


def test_a_single_failed_step_may_be_named_but_must_be_the_failed_one() -> None:
    assert sl.first_failed_step(FAILED, "Pull image").number == 3
    assert sl.first_failed_step(FAILED).number == 3
    with pytest.raises(sl.StepLogError):
        sl.first_failed_step(FAILED, "Prev")


def test_end_to_end_two_failed_steps_without_step_store_nothing_and_with_step_store_the_named_one(root: Path) -> None:
    job = {**failed_job(), "steps": COE_JOB["steps"]}
    write_candidates(root)
    code, out = fetch(root, FakeTransport(routes_for(jobs=[job], log=COE_LOG.encode())), "--yes-fetch")
    assert code == 1 and "NOT STORED" in out and "Lint (continue-on-error)" in out
    assert not (root / "logs").exists() and not (root / "fetched").exists()
    write_candidates(root, step="Pull image")
    code, out = fetch(root, FakeTransport(routes_for(jobs=[job], log=COE_LOG.encode())), "--yes-fetch")
    assert code == 0
    rec = json.loads((root / "fetched" / "hc-01.json").read_text(encoding="utf-8"))
    assert rec["step"]["name"] == "Pull image" and [o["name"] for o in rec["other_failed_steps"]] == [
        "Lint (continue-on-error)"
    ]
    assert "Run lint" not in (root / "logs" / "hc-01.log").read_text(encoding="utf-8")


def test_end_to_end_a_step_naming_a_passing_step_is_refused(root: Path) -> None:
    write_candidates(root, step="Next")
    code, out = fetch(root, FakeTransport(routes_for()), "--yes-fetch")
    assert code == 1 and "NOT STORED" in out and not (root / "logs").exists()


@pytest.mark.parametrize("bad", [0, -1, "", True, 1.5, None, ["a"], "x" * 301])
def test_the_step_field_is_validated_by_the_schema(root: Path, bad: object) -> None:
    write_candidates(root, step=bad)
    with pytest.raises(hf.CandidateError, match="schema violation"):
        hf.load_candidates(root / "candidates.json")


@pytest.mark.parametrize("good", ["Pull image", 3])
def test_the_step_field_is_accepted_as_a_name_or_a_number(root: Path, good: object) -> None:
    write_candidates(root, step=good)
    assert hf.load_candidates(root / "candidates.json")[0].step == good


# -- item 3: untimestamped headers ----------------------------------------
def test_an_untimestamped_own_header_before_an_inner_header_is_refused() -> None:
    log = (
        L(3, G + "Run prev")
        + G
        + "Run docker pull example\n"  # the failed step's own header lacks a timestamp (scaffolding)
        + L(4.2, G + "Run inner composite header")
        + L(5, CATALOG_LINE)
        + L(6, RUNNER_ERROR)
    )
    refused(log, FAILED, "without a timestamp")


def test_an_untimestamped_header_before_the_window_is_not_in_the_region() -> None:
    log = (
        G
        + "Run something much earlier\n"
        + L(3, "output of the previous step")
        + L(4, G + "Run docker pull example")
        + L(5, CATALOG_LINE)
        + L(6, RUNNER_ERROR)
    )
    assert extract(log, FAILED).start_line == 3


# -- item 1: unrecognised framing ----------------------------------------
@pytest.mark.parametrize("line", ["Post job cleanup.", "Cleaning up orphan processes"])
def test_a_plain_post_job_cleanup_or_orphan_line_after_the_error_asks_for_a_real_sample(line: str) -> None:
    log = L(4, G + "Run docker pull example") + L(5, CATALOG_LINE) + L(6, RUNNER_ERROR) + L(6.5, line)
    refused(log, FAILED, "unrecognised runner framing.*paste a sample")


def test_ordinary_trailing_output_keeps_its_own_message() -> None:
    log = L(4, G + "Run docker pull example") + L(6, RUNNER_ERROR) + L(6.5, "some output")
    refused(log, FAILED, "output continues after the error line")


def test_post_job_cleanup_is_not_accepted_as_a_boundary_form() -> None:
    log = L(4, G + "Run docker pull example") + L(6, RUNNER_ERROR) + L(6.5, "Post job cleanup.") + L(7, G + "Run next")
    with pytest.raises(sl.StepLogError):
        extract(log, FAILED)


# -- item 4: refusal-path layouts ----------------------------------------
def test_crlf_nested_header_layout_is_refused_like_lf() -> None:
    log = (
        L(4, G + "Run docker pull example", "\r\n")
        + L(4.2, G + "Run inner", "\r\n")
        + L(5, CATALOG_LINE, "\r\n")
        + L(6, RUNNER_ERROR, "\r\n")
    )
    refused(log, FAILED, "2 step headers")


def test_crlf_success_keeps_endings_and_counts_lines() -> None:
    log = L(4, G + "Run docker pull example", "\r\n") + L(5, CATALOG_LINE, "\r\n") + L(6, RUNNER_ERROR, "\r\n")
    ext = extract(log, FAILED)
    assert ext.text == log and ext.end_line == 3


def test_a_lone_cr_log_is_one_line_and_is_refused() -> None:
    log = L(4, G + "Run docker pull example", "\r") + L(5, CATALOG_LINE, "\r") + L(6, RUNNER_ERROR, "\r")
    refused(log, FAILED, "no '##\\[error\\]'")


def test_a_bom_on_the_failed_steps_own_header_is_refused_undecoded_and_works_after_decode_log() -> None:
    log = L(4, G + "Run docker pull example") + L(5, CATALOG_LINE) + L(6, RUNNER_ERROR)
    refused("﻿" + log, FAILED, "no step header")
    ext = sl.extract_step_section(sl.decode_log(("﻿" + log).encode("utf-8")), sl.first_failed_step(FAILED))
    assert ext.text == log


def test_a_very_long_line_inside_the_step_is_kept_and_after_the_error_is_refused() -> None:
    big = "x" * 1_000_000
    inside = L(4, G + "Run docker pull example") + L(5, big) + L(6, RUNNER_ERROR)
    ext = extract(inside, FAILED)
    assert ext.text == inside and ext.end_line == 3
    refused(inside + L(6.5, big), FAILED, "output continues")


def test_a_passing_neighbour_echoing_an_error_line_before_the_header_in_the_same_second_is_refused() -> None:
    log = (
        L(3, G + "Run prev")
        + L(4.1, RUNNER_ERROR)  # a passing neighbour printing the runner's error wording (scaffolding)
        + L(4.5, G + "Run docker pull example")
        + L(5, CATALOG_LINE)
        + L(6, RUNNER_ERROR)
    )
    refused(log, FAILED, "comes before the step header")


# -- item 5: env_token ----------------------------------------
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    p = tmp_path / "env-file"
    p.write_bytes(data)
    monkeypatch.setattr(hf, "ENV_FILE", p)
    monkeypatch.delenv(hf.TOKEN_ENV, raising=False)


def test_a_duplicate_token_key_is_an_explicit_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file(tmp_path, monkeypatch, f"{hf.TOKEN_ENV}=first-value\nX=1\n{hf.TOKEN_ENV}=second-value\n".encode())
    with pytest.raises(gh.FetchError) as e:
        hf.env_token()
    assert "2 times" in str(e.value) and "first-value" not in str(e.value) and "second-value" not in str(e.value)


def test_the_environment_variable_wins_over_a_duplicated_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file(tmp_path, monkeypatch, f"{hf.TOKEN_ENV}=a\n{hf.TOKEN_ENV}=b\n".encode())
    monkeypatch.setenv(hf.TOKEN_ENV, "from-env")
    assert hf.env_token() == "from-env"


def test_a_non_utf8_env_file_is_a_clean_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file(tmp_path, monkeypatch, b"# caf\xe9\n" + f"{hf.TOKEN_ENV}=secret-value\n".encode())
    with pytest.raises(gh.FetchError, match="could not read the token.*UTF-8") as e:
        hf.env_token()
    assert "secret-value" not in str(e.value)


def test_the_cli_reports_token_file_problems_without_a_traceback_or_a_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: Path
) -> None:
    env_file(tmp_path, monkeypatch, b"\xff\xfe\x00bad")
    write_candidates(root)
    out = io.StringIO()
    code = hf.run_fetch(
        ["--yes-fetch"],
        root=root,
        out=out,
        err=io.StringIO(),
        transport_factory=lambda: pytest.fail("transport built"),
        token_provider=hf.env_token,
        clock=CLOCK,
        autonomous=lambda: False,
    )
    assert code == 2 and "could not read the token" in out.getvalue() and "Traceback" not in out.getvalue()


def test_a_leading_bom_does_not_hide_the_first_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file(tmp_path, monkeypatch, b"\xef\xbb\xbf" + f"{hf.TOKEN_ENV}=bom-first\nOTHER=1\n".encode())
    assert hf.env_token() == "bom-first"


# -- item 6: .autonomous ----------------------------------------
def test_a_broken_symlink_named_autonomous_counts_as_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hf, "ROOT", tmp_path)
    try:
        os.symlink(tmp_path / "does-not-exist", tmp_path / ".autonomous")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    assert not (tmp_path / ".autonomous").exists() and hf.autonomous_present() is True


def test_presence_is_decided_by_lexists_not_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Where symlinks cannot be created: a dangling entry is lexists-true and exists-false; simulate that."""
    monkeypatch.setattr(hf, "ROOT", tmp_path)
    marker = tmp_path / ".autonomous"
    monkeypatch.setattr(hf.os.path, "lexists", lambda p: Path(p) == marker)
    assert not marker.exists() and hf.autonomous_present() is True


def test_a_directory_named_autonomous_counts_as_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hf, "ROOT", tmp_path)
    (tmp_path / ".autonomous").mkdir()
    assert hf.autonomous_present() is True


def test_the_main_checkout_is_found_from_the_worktree_git_file_without_a_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "w9").mkdir(parents=True)
    wt = tmp_path / "elsewhere" / "wt"  # NOT under .claude/worktrees
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'w9'}\n", encoding="utf-8")
    monkeypatch.setattr(hf, "ROOT", wt)
    assert hf.autonomous_present() is False
    (main / ".autonomous").write_text("")
    assert hf.autonomous_present() is True


def test_an_unrelated_or_garbled_git_file_adds_no_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").write_bytes(b"\xff\xfe not a gitdir line")
    monkeypatch.setattr(hf, "ROOT", tmp_path)
    assert hf._worktree_main_checkout(tmp_path) is None
    (tmp_path / ".git").write_text("gitdir:\n", encoding="utf-8")
    assert hf._worktree_main_checkout(tmp_path) is None
    assert hf.autonomous_present() is False


# -- item 7: cross-field consistency ----------------------------------------
def record(**ex: int) -> dict:  # type: ignore[type-arg]
    base = {"job_log_lines": 20, "start_line": 5, "end_line": 8, "error_line": 8, "stored_lines": 4}
    return {"extraction": {**base, **ex}}


def test_consistent_extraction_fields_pass_and_inconsistent_ones_are_named() -> None:
    assert sl.extraction_inconsistency(record()) is None
    assert sl.extraction_inconsistency(record(stored_lines=5)) is not None
    assert sl.extraction_inconsistency(record(error_line=9)) is not None
    assert sl.extraction_inconsistency(record(error_line=4)) is not None
    assert sl.extraction_inconsistency(record(end_line=21, stored_lines=17)) is not None


def test_the_skeleton_reports_a_record_whose_fields_disagree(root: Path) -> None:
    write_candidates(root)
    code, _ = fetch(root, FakeTransport(routes_for()), "--yes-fetch")
    assert code == 0
    p = root / "fetched" / "hc-01.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    cases, _, problems = sk.build_skeleton(root)
    assert len(cases) == 1 and problems == []
    rec["extraction"]["stored_lines"] += 1
    p.write_text(json.dumps(rec), encoding="utf-8")
    cases, _, problems = sk.build_skeleton(root)
    assert cases == [] and "stored_lines" in problems[0]


def test_the_candidates_schema_has_no_raw_newline_escape_in_descriptions() -> None:
    text = Path(hf.CANDIDATES_SCHEMA).read_text(encoding="utf-8")
    assert "\\n-counted" not in text
