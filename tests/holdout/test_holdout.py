"""N1 holdout: schema, quotas, label timing, redaction, the sealed test split, and the offline eval.

No real case exists yet, so these tests build a throwaway holdout in tmp_path. The log text in it is copied at test
time from verified (`captured-verbatim`) entries of `generator/error_catalog.json` (their `source` URLs are recorded
there); nothing here writes error text by hand. Case metadata (repos, run ids, SHAs) is structure, not data.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from driftgate.eval import holdout as hd
from driftgate.eval import holdout_eval as he
from driftgate.generator.catalog import load_catalog

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
LABELED = "2026-10-15T09:00:00Z"
FETCHED = "2026-10-14"


def _verbatim(entry_id: str) -> str:
    # Entries without placeholders only, so no character of the text is ours.
    return "\n".join(load_catalog()[entry_id].lines) + "\n"


RATE_LIMIT_LOG = _verbatim("docker_pull_rate_limit")  # signature docker_pull_rate_limit: L2, Tier 0 rule exists
EXPIRED_LOG = _verbatim("aws_expired_token")  # signature aws_expired_token: L2, no Tier 0 rule


def _raw_case(n: int, *, split: str, layer: str, kind: str = "pr_fix", log: str = EXPIRED_LOG) -> dict[str, Any]:
    action: dict[str, Any] = {"kind": kind}
    if kind == "pr_fix":
        action["fix"] = "fix_secret_scope"
    return {
        "id": f"hc-{n:02d}",
        "split": split,
        "layer": layer,
        "classification": "user",
        "correct_action": action,
        "rationale": "structure-only test label",
        "source": {
            "repo": "example-org/example-repo",
            "run_id": 1000 + n,
            "job": f"job-{n}",
            "commit_sha": f"{n:040x}",
            "url": f"https://github.com/example-org/example-repo/actions/runs/{1000 + n}",
            "fetch_date": FETCHED,
        },
        "log_sha256": hd.sha256_bytes(log.encode("utf-8")),
        "labeled_at": LABELED,
    }


def _write(
    root: Path, cases: list[dict[str, Any]], logs: dict[str, str], runs: list[dict[str, Any]] | None = None
) -> Path:
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "labels.json").write_text(json.dumps({"schema_version": 1, "cases": cases}), encoding="utf-8")
    (root / "runs.json").write_text(json.dumps({"schema_version": 1, "runs": runs or []}), encoding="utf-8")
    for case_id, text in logs.items():
        (root / "logs" / f"{case_id}.log").write_bytes(text.encode("utf-8"))
    return root


def full_set() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """30 cases meeting every quota: 10 dev / 20 test, 7 per L1-L4 plus 2 L5, 4 escalations, 4 reruns."""
    layers = ["L1", "L2", "L3", "L4"] * 7 + ["L5", "L5"]
    cases, logs = [], {}
    for i, layer in enumerate(layers, start=1):
        kind = "escalate" if i in (5, 10, 15, 20) else ("rerun" if i in (1, 2, 3, 4) else "pr_fix")
        log = RATE_LIMIT_LOG if kind == "rerun" else EXPIRED_LOG
        cases.append(_raw_case(i, split="dev" if i <= 10 else "test", layer=layer, kind=kind, log=log))
        logs[f"hc-{i:02d}"] = log
    return cases, logs


@pytest.fixture
def root(tmp_path: Path) -> Path:
    cases, logs = full_set()
    return _write(tmp_path / "holdout", cases, logs)


def _problems(r: Path) -> tuple[str, ...]:
    return hd.validate(r, now=NOW).problems


# -- schema ----------------------------------------------------------------------------------------------------
def test_checked_in_holdout_is_a_valid_empty_file_that_reports_every_unmet_quota() -> None:
    report = hd.validate(hd.DEFAULT_DIR, now=NOW)
    assert report.problems == () and not report.ok
    assert report.counts["total"] == 0
    text = "\n".join(report.unmet_quotas)
    for expected in ("total cases: 0/30", "dev split: 0/10", "test split: 0/20", "layer L1: 0/4", "layer L4: 0/4"):
        assert expected in text
    assert "escalation cases: 0/3" in text


def test_a_complete_set_passes(root: Path) -> None:
    report = hd.validate(root, now=NOW)
    assert report.ok, (report.problems, report.unmet_quotas)


def _schema_errors(doc: dict[str, Any]) -> list[str]:
    return [e.message for e in Draft202012Validator(hd._schema()).iter_errors(doc)]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["correct_action"].pop("fix"),  # pr_fix needs a named fix
        lambda c: c.update(correct_action={"kind": "rerun", "fix": "x"}),  # only pr_fix names a fix
        lambda c: c.update(correct_action={"kind": "merge"}),
        lambda c: c.update(layer="L9"),
        lambda c: c.update(split="holdout"),
        lambda c: c.update(classification="flaky"),
        lambda c: c.update(extra=1),
        lambda c: c.update(log_sha256="abc"),
        lambda c: c.update(labeled_at="yesterday"),
        lambda c: c.update(rationale="two\nlines"),
        lambda c: c["source"].update(commit_sha="abc"),
        lambda c: c["source"].update(url="https://example.com/x"),
        lambda c: c["source"].pop("fetch_date"),
        lambda c: c.update(id="case-1"),
    ],
)
def test_schema_rejects_malformed_cases(mutate: Callable[[dict[str, Any]], object]) -> None:
    case = _raw_case(1, split="dev", layer="L1")
    assert _schema_errors({"schema_version": 1, "cases": [case]}) == []
    bad = copy.deepcopy(case)
    mutate(bad)
    assert _schema_errors({"schema_version": 1, "cases": [bad]})


def test_schema_version_is_pinned() -> None:
    assert _schema_errors({"schema_version": 2, "cases": []})


def test_schema_errors_never_echo_label_content(tmp_path: Path) -> None:
    case = _raw_case(7, split="test", layer="L1")
    case["rationale"] = "SECRET-LABEL-TEXT\nsecond line"
    _write(tmp_path, [case], {"hc-07": EXPIRED_LOG})
    problems = "\n".join(_problems(tmp_path))
    assert "schema violation" in problems and "SECRET-LABEL-TEXT" not in problems


def test_unparseable_labels_are_reported_not_raised(tmp_path: Path) -> None:
    (tmp_path / "labels.json").write_text("{not json", encoding="utf-8")
    assert any("unreadable" in p for p in _problems(tmp_path))


# -- quotas ----------------------------------------------------------------------------------------------------
def _unmet(cases: list[dict[str, Any]], logs: dict[str, str], tmp_path: Path) -> list[str]:
    return list(hd.validate(_write(tmp_path, cases, logs), now=NOW).unmet_quotas)


def test_each_quota_is_reported_separately(tmp_path: Path) -> None:
    cases, logs = full_set()
    # Drop hc-02 (an L2 rerun in dev) so total=29, dev=9; make every remaining L1 case something else.
    kept = [c for c in cases if c["id"] != "hc-02"]
    unmet = _unmet(kept, logs, tmp_path)
    assert "total cases: 29/30" in unmet and "dev split: 9/10" in unmet
    assert not any(q.startswith("layer") or q.startswith("escalation") for q in unmet)

    relabelled = copy.deepcopy(cases)
    for c in relabelled:
        if c["layer"] == "L3":
            c["layer"] = "L4"
    assert "layer L3: 0/4 minimum" in _unmet(relabelled, logs, tmp_path / "b")

    no_esc = copy.deepcopy(cases)
    for c in no_esc:
        if c["correct_action"]["kind"] == "escalate":
            c["correct_action"] = {"kind": "pr_fix", "fix": "fix_secret_scope"}
    assert _unmet(no_esc, logs, tmp_path / "c") == ["escalation cases: 0/3 minimum"]


def test_the_split_must_be_10_and_20(tmp_path: Path) -> None:
    cases, logs = full_set()
    cases[0]["split"] = "test"
    unmet = _unmet(cases, logs, tmp_path)
    assert "dev split: 9/10" in unmet and "test split: 21/20" in unmet


# -- logs, hashes, redaction -----------------------------------------------------------------------------------
def test_missing_modified_and_orphan_logs_are_flagged(root: Path) -> None:
    (root / "logs" / "hc-03.log").unlink()
    (root / "logs" / "hc-04.log").write_bytes(b"changed after hashing\n")
    (root / "logs" / "hc-99.log").write_bytes(b"x")
    problems = "\n".join(_problems(root))
    assert "hc-03: logs/hc-03.log is missing" in problems
    assert "hc-04: stored log does not match log_sha256" in problems
    assert "hc-99.log has no label" in problems


def test_a_log_with_a_credential_shaped_string_is_rejected(tmp_path: Path) -> None:
    leaked = EXPIRED_LOG + "GITHUB_TOKEN=ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8" + "\n"
    case = _raw_case(1, split="dev", layer="L2", log=leaked)
    _write(tmp_path, [case], {"hc-01": leaked})
    problems = "\n".join(_problems(tmp_path))
    assert "hc-01: stored log is changed by redaction" in problems
    assert "ghp_" not in problems


def test_a_redacted_log_is_stable_under_redaction(tmp_path: Path) -> None:
    from driftgate.redaction import redact

    text = redact(EXPIRED_LOG + "GITHUB_TOKEN=ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8" + "\n")
    _write(tmp_path, [_raw_case(1, split="dev", layer="L2", log=text)], {"hc-01": text})
    assert _problems(tmp_path) == ()


def test_duplicate_ids_and_duplicate_source_runs_are_flagged(tmp_path: Path) -> None:
    a = _raw_case(1, split="dev", layer="L1")
    b = copy.deepcopy(a)
    problems = "\n".join(_problems(_write(tmp_path, [a, b], {"hc-01": EXPIRED_LOG})))
    assert "duplicate case id hc-01" in problems and "duplicate source run/job" in problems


# -- labels before runs ----------------------------------------------------------------------------------------
def _run_for(case: dict[str, Any], ran_at: str) -> dict[str, Any]:
    return {
        "case_id": case["id"],
        "system": "agent",
        "ran_at": ran_at,
        "label_sha256": hd.label_digest(case),
        "layer": case["layer"],
        "classification": case["classification"],
        "action": case["correct_action"],
    }


def test_a_run_after_its_label_is_accepted(root: Path) -> None:
    cases, logs = full_set()
    _write(root, cases, logs, [_run_for(cases[0], "2026-10-16T00:00:00Z")])
    assert _problems(root) == ()


def test_a_run_not_after_its_label_is_flagged(root: Path) -> None:
    cases, logs = full_set()
    for ran_at in (LABELED, "2026-10-14T00:00:00Z"):
        _write(root, cases, logs, [_run_for(cases[0], ran_at)])
        assert any("run at or before labeled_at" in p for p in _problems(root))


def test_editing_a_label_after_the_run_voids_the_run(root: Path) -> None:
    cases, logs = full_set()
    run = _run_for(cases[0], "2026-10-16T00:00:00Z")
    cases[0]["rationale"] = "edited after the agent ran"
    _write(root, cases, logs, [run])
    assert any("label changed after the run" in p for p in _problems(root))


def test_a_run_for_an_unknown_case_is_flagged(root: Path) -> None:
    cases, logs = full_set()
    ghost = {**_run_for(cases[0], "2026-10-16T00:00:00Z"), "case_id": "hc-77"}
    _write(root, cases, logs, [ghost])
    assert any("unknown case hc-77" in p for p in _problems(root))


def test_future_labels_and_labels_older_than_the_fetch_are_flagged(root: Path) -> None:
    cases, logs = full_set()
    cases[0]["labeled_at"] = (NOW + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cases[1]["labeled_at"] = "2026-10-13T00:00:00Z"  # before fetch_date 2026-10-14
    _write(root, cases, logs)
    problems = "\n".join(_problems(root))
    assert "hc-01: labeled_at is in the future" in problems
    assert "hc-02: labeled_at is before the log's fetch_date" in problems


# -- the sealed test split --------------------------------------------------------------------------------------
def test_per_case_detail_for_test_cases_is_unavailable(root: Path) -> None:
    h = hd.Holdout.load(root, now=NOW)
    assert {c.split for c in h.dev_cases()} == {"dev"} and len(h.dev_cases()) == 10
    assert h.dev_log("hc-05")
    with pytest.raises(hd.SealedSplitError):
        h.test_cases()
    with pytest.raises(hd.SealedSplitError):
        h.case("hc-11")
    with pytest.raises(hd.SealedSplitError):
        h.dev_log("hc-11")
    assert not [n for n in dir(h) if "test" in n and n not in ("test_cases", "test_aggregate")]


def test_test_aggregate_returns_only_what_the_aggregator_returns(root: Path) -> None:
    h = hd.Holdout.load(root, now=NOW)
    seen: list[str] = []

    def scorer(case: hd.HoldoutCase, log: str, run: hd.RunRecord | None) -> int:
        seen.append(case.id)
        return 1

    assert h.test_aggregate(scorer, sum) == 20
    assert len(seen) == 20 and all(int(i[3:]) > 10 for i in seen)


# -- offline eval ------------------------------------------------------------------------------------------------
def test_baseline_reruns_only_on_a_signature_with_a_tier0_rule() -> None:
    rerun = _raw_case(1, split="dev", layer="L2", kind="rerun", log=RATE_LIMIT_LOG)
    fix = _raw_case(2, split="dev", layer="L2", log=EXPIRED_LOG)
    r = he.baseline_on_log(hd._case(rerun), RATE_LIMIT_LOG)
    f = he.baseline_on_log(hd._case(fix), EXPIRED_LOG)
    assert r.acted and r.action == he.RERUN and r.classification == "transient"
    assert not f.acted and f.layer == "L2"
    unknown = he.baseline_on_log(hd._case(fix), "no known signature here\n")
    assert not unknown.acted


def test_eval_with_no_cases_says_so_and_prints_no_numbers(capsys: pytest.CaptureFixture[str]) -> None:
    code = he.main(["eval"], clock=lambda: NOW)
    out = capsys.readouterr().out
    assert code == 1 and "no holdout cases are stored" in out and "no numbers are shown" in out
    assert "%" not in out and "|" not in out


def test_validate_cli_exits_nonzero_and_names_unmet_quotas(capsys: pytest.CaptureFixture[str]) -> None:
    assert he.main(["validate"], clock=lambda: NOW) == 1
    out = capsys.readouterr().out
    assert "UNMET QUOTA: total cases: 0/30" in out and "holdout-validate: FAILED" in out


def test_validate_cli_passes_on_a_complete_set(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert he.main(["validate", "--dir", str(root)], clock=lambda: NOW) == 0
    assert "holdout-validate: ok" in capsys.readouterr().out


def test_eval_reports_counts_beside_percentages_and_seals_the_test_split(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases, logs = full_set()
    cases[10]["rationale"] = "TEST-SPLIT-RATIONALE"
    _write(root, cases, logs)
    assert he.main(["eval", "--dir", str(root)], clock=lambda: NOW) == 0
    out = capsys.readouterr().out
    assert "/10 = " in out and "/20 = " in out  # counts beside percentages, per split
    assert "not run" in out  # no run records: the agent column is empty, not invented
    dev_section = out.split("## Dev cases")[1].split("## Test split")[0]
    assert all(f"hc-{i:02d}" in dev_section for i in range(1, 11))
    assert not any(f"hc-{i:02d}" in out for i in range(11, 31))
    assert "TEST-SPLIT-RATIONALE" not in out


def test_agent_column_comes_from_run_records_only(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cases, logs = full_set()
    runs = [_run_for(c, "2026-10-16T00:00:00Z") for c in cases[:4]]  # four dev reruns, the agent got them right
    _write(root, cases, logs, runs)
    assert he.main(["eval", "--dir", str(root)], clock=lambda: NOW) == 0
    out = capsys.readouterr().out
    assert "4/10 run" in out and "0/20 run" in out


def test_partial_data_evaluates_but_flags_the_unmet_quotas(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(
        tmp_path, [_raw_case(1, split="dev", layer="L2", kind="rerun", log=RATE_LIMIT_LOG)], {"hc-01": RATE_LIMIT_LOG}
    )
    assert he.main(["eval", "--dir", str(tmp_path)], clock=lambda: NOW) == 0
    out = capsys.readouterr().out
    assert "UNMET QUOTA: total cases: 1/30" in out and "(results above are partial)" in out


def test_eval_refuses_an_invalid_holdout(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (root / "logs" / "hc-01.log").write_bytes(b"tampered")
    assert he.main(["eval", "--dir", str(root)], clock=lambda: NOW) == 1
    assert "refusing to evaluate" in capsys.readouterr().out
