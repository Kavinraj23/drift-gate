"""N1 follow-up: the REAL job count of a run, recorded at fetch time and fed honestly to the runner's gate.

Offline: stubbed transport, fake clock, no key, no sleep. Log text comes from `fetch_helpers` (sourced from the
verbatim catalog plus marked scaffolding) and from `holdout_helpers`; no error string is typed here (invariant 9).
Job NAMES in these tests are scaffolding for the stub API; the stored record keeps only the fetched job's name."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fetch_helpers import (
    API,
    REPO,
    RUN_ID,
    TOKEN,
    FakeTransport,
    failed_job,
    js,
    ok_job,
    routes_for,
)
from holdout_helpers import NOW, make_holdout
from jsonschema import Draft202012Validator

from driftgate.domain import SourceError
from driftgate.eval import holdout as hd
from driftgate.eval import holdout_eval as he
from driftgate.eval import holdout_fetch as hf
from driftgate.eval import holdout_gh as gh
from driftgate.eval import holdout_jobs as hj
from driftgate.eval import holdout_run as hr
from driftgate.eval import holdout_skeleton as sk
from driftgate.eval.holdout_source import HoldoutSource
from driftgate.tools import ToolContext, dispatch

CLOCK = lambda: datetime(2026, 10, 14, 23, 59, tzinfo=UTC)  # noqa: E731
CONTRACT = Path(hj.CONTRACT)


def write_candidates(root: Path, *entries: dict) -> None:  # type: ignore[type-arg]
    (root / "candidates.json").write_text(json.dumps({"schema_version": 1, "candidates": list(entries)}), "utf-8")


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


def record_of(root: Path, case_id: str = "hc-01") -> dict:  # type: ignore[type-arg]
    return json.loads((root / "fetched" / f"{case_id}.json").read_text(encoding="utf-8"))


@pytest.fixture
def fetch_root(tmp_path: Path) -> Path:
    r = tmp_path / "holdout"
    r.mkdir()
    return r


def jobs_doc(rec: dict) -> dict:  # type: ignore[type-arg]
    return rec["jobs"]


# -- the fetcher records real counts ----------------------------------------------------------------
def test_all_success_siblings_are_counted(fetch_root: Path) -> None:
    jobs = [ok_job(9000, "lint"), ok_job(9002, "unit"), failed_job()]
    write_candidates(fetch_root, {"repo": REPO, "run_id": RUN_ID})
    code, out = fetch(fetch_root, FakeTransport(routes_for(jobs=jobs)), "--yes-fetch")
    assert code == 0, out
    rec = record_of(fetch_root)
    assert rec["schema_version"] == 3
    assert jobs_doc(rec) == {"total": 3, "success": 2, "failure": 1, "cancelled": 0, "skipped": 0, "other": 0}
    assert "run had 3 job(s)" in out


def test_mixed_conclusions_are_counted_and_unknown_ones_are_other(fetch_root: Path) -> None:
    jobs = [
        failed_job(),
        {**ok_job(9001, "a"), "conclusion": "cancelled"},
        {**ok_job(9002, "b"), "conclusion": "skipped"},
        {**ok_job(9003, "c"), "conclusion": "skipped"},
        {**ok_job(9004, "d"), "conclusion": "neutral"},
        {**ok_job(9005, "e"), "conclusion": None},
        {**ok_job(9006, "f"), "conclusion": "timed_out"},
        ok_job(9007, "g"),
    ]
    write_candidates(fetch_root, {"repo": REPO, "run_id": RUN_ID})
    code, out = fetch(fetch_root, FakeTransport(routes_for(jobs=jobs)), "--yes-fetch")
    assert code == 0, out
    assert jobs_doc(record_of(fetch_root)) == {
        "total": 8,
        "success": 1,
        "failure": 1,
        "cancelled": 1,
        "skipped": 2,
        "other": 3,
    }


def test_the_stored_record_has_counts_only_no_other_job_names(fetch_root: Path) -> None:
    jobs = [ok_job(9000, "SIBLING-NAME-MARKER"), failed_job()]
    write_candidates(fetch_root, {"repo": REPO, "run_id": RUN_ID})
    assert fetch(fetch_root, FakeTransport(routes_for(jobs=jobs)), "--yes-fetch")[0] == 0
    text = (fetch_root / "fetched" / "hc-01.json").read_text(encoding="utf-8")
    assert "SIBLING-NAME-MARKER" not in text
    assert set(jobs_doc(json.loads(text))) == {"total", "success", "failure", "cancelled", "skipped", "other"}


def test_a_paginated_jobs_list_is_counted_in_full(fetch_root: Path) -> None:
    jobs = [failed_job()] + [ok_job(1000 + i, f"j{i}") for i in range(100)]  # 101 jobs, two pages
    base = f"{API}/repos/{REPO}/actions/runs/{RUN_ID}/jobs?filter=latest&per_page=100"
    routes = routes_for(jobs=jobs)
    routes[base] = js({"total_count": 101, "jobs": jobs[:100]})
    routes[base + "&page=2"] = js({"total_count": 101, "jobs": jobs[100:]})
    write_candidates(fetch_root, {"repo": REPO, "run_id": RUN_ID})
    code, out = fetch(fetch_root, FakeTransport(routes), "--yes-fetch")
    assert code == 0, out
    assert jobs_doc(record_of(fetch_root))["total"] == 101 and jobs_doc(record_of(fetch_root))["success"] == 100


def test_a_truncated_jobs_list_refuses_the_case_and_writes_nothing(fetch_root: Path) -> None:
    jobs = [failed_job(), ok_job(9000, "lint")]
    base = f"{API}/repos/{REPO}/actions/runs/{RUN_ID}/jobs?filter=latest&per_page=100"
    routes = routes_for(jobs=jobs)
    routes[base] = js({"total_count": 5, "jobs": jobs})  # GitHub says 5 jobs, delivered 2
    write_candidates(fetch_root, {"repo": REPO, "run_id": RUN_ID})
    code, out = fetch(fetch_root, FakeTransport(routes), "--yes-fetch")
    assert code == 1 and "truncated" in out
    assert not (fetch_root / "fetched").exists() and not (fetch_root / "logs").exists()


def test_a_list_over_the_page_cap_is_refused_not_counted() -> None:
    jobs = [{"id": i + 1, "name": f"j{i}", "conclusion": "success"} for i in range(100)]
    base = f"{API}/repos/{REPO}/actions/runs/{RUN_ID}/jobs?filter=latest&per_page=100"
    routes = {base + (f"&page={p}" if p > 1 else ""): js({"total_count": 999, "jobs": jobs}) for p in range(1, 7)}
    client = gh.ReadOnlyGitHub(FakeTransport(routes), TOKEN)
    with pytest.raises(gh.FetchError, match="more than"):
        client.list_jobs(REPO, RUN_ID)


# -- schema version 3, the loader, version 2 refused, cross-field checks --------------------------------------------
def _validator() -> Draft202012Validator:
    return hj.fetched_validator()


def test_a_fetched_record_validates_against_the_v3_contract(fetch_root: Path) -> None:
    write_candidates(fetch_root, {"repo": REPO, "run_id": RUN_ID})
    assert fetch(fetch_root, FakeTransport(routes_for()), "--yes-fetch")[0] == 0
    rec = record_of(fetch_root)
    assert list(_validator().iter_errors(rec)) == []
    assert hj.jobs_inconsistency(rec) is None
    schema = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert schema["$defs"]["fetched_record"]["properties"]["schema_version"] == {"const": 3}
    assert "jobs" in schema["$defs"]["fetched_record"]["required"]
    for bad in (2, 4):
        assert list(_validator().iter_errors({**rec, "schema_version": bad}))
    missing = {k: v for k, v in rec.items() if k != "jobs"}
    assert list(_validator().iter_errors(missing))


def test_the_job_count_cross_field_rules() -> None:
    def rec(**over: int) -> dict:  # type: ignore[type-arg]
        base = {"total": 4, "success": 2, "failure": 1, "cancelled": 1, "skipped": 0, "other": 0}
        return {"jobs": {**base, **over}}

    assert hj.jobs_inconsistency(rec()) is None
    assert "sum" in (hj.jobs_inconsistency(rec(total=5)) or "")
    assert "sum" in (hj.jobs_inconsistency(rec(success=3)) or "")
    assert "failure" in (hj.jobs_inconsistency(rec(total=3, failure=0)) or "")
    assert hj.jobs_inconsistency(rec(total=0, success=0, failure=0, cancelled=0)) is not None


def test_the_loader_refuses_a_version_2_record_with_a_clear_message(fetch_root: Path) -> None:
    write_candidates(fetch_root, {"repo": REPO, "run_id": RUN_ID})
    assert fetch(fetch_root, FakeTransport(routes_for()), "--yes-fetch")[0] == 0
    p = fetch_root / "fetched" / "hc-01.json"
    old = {k: v for k, v in record_of(fetch_root).items() if k != "jobs"}
    old["schema_version"] = 2
    p.write_text(json.dumps(old), encoding="utf-8")
    with pytest.raises(hj.FetchedRecordError, match="version 2 is no longer accepted"):
        hj.load_fetched_facts(fetch_root, "hc-01")
    problems = hj.fetched_problems(fetch_root)
    assert len(problems) == 1 and "version 2" in problems[0] and "hc-01" not in problems[0]
    _, _, skeleton_problems = sk.build_skeleton(fetch_root)
    assert any("version 2 is no longer accepted" in x and "re-fetch" in x for x in skeleton_problems)


def test_the_loader_and_skeleton_refuse_inconsistent_counts(fetch_root: Path) -> None:
    write_candidates(fetch_root, {"repo": REPO, "run_id": RUN_ID})
    assert fetch(fetch_root, FakeTransport(routes_for()), "--yes-fetch")[0] == 0
    p = fetch_root / "fetched" / "hc-01.json"
    rec = record_of(fetch_root)
    rec["jobs"]["total"] += 1
    p.write_text(json.dumps(rec), encoding="utf-8")
    with pytest.raises(hj.FetchedRecordError, match="sum"):
        hj.load_fetched_facts(fetch_root, "hc-01")
    assert sk.build_skeleton(fetch_root)[0] == []
    assert any("sum" in x for x in sk.build_skeleton(fetch_root)[2])


def test_a_missing_record_is_none_not_an_error(fetch_root: Path) -> None:
    assert hj.load_fetched_facts(fetch_root, "hc-01") is None
    assert hj.fetched_problems(fetch_root) == []


# -- holdout-fetch --limit ---------------------------------------------------------------------------
def _three_runs() -> dict:  # type: ignore[type-arg]
    routes: dict = {}  # type: ignore[type-arg]
    for rid in (5001, 5002, 5003):
        routes.update(routes_for(run_id=rid))
    return routes


CANDS = ({"repo": REPO, "run_id": 5001}, {"repo": REPO, "run_id": 5002}, {"repo": REPO, "run_id": 5003})


def test_limit_processes_only_the_first_n_new_candidates_in_input_order(fetch_root: Path) -> None:
    write_candidates(fetch_root, *CANDS)
    tr = FakeTransport(_three_runs())
    code, out = fetch(fetch_root, tr, "--yes-fetch", "--limit", "2")
    assert code == 0, out
    assert sorted(p.name for p in (fetch_root / "fetched").iterdir()) == ["hc-01.json", "hc-02.json"]
    assert [record_of(fetch_root, c)["source"]["run_id"] for c in ("hc-01", "hc-02")] == [5001, 5002]
    assert "--limit: 1 further not-yet-fetched candidate(s)" in out
    assert not any("/runs/5003" in url for url, _ in tr.calls)


def test_the_plan_shows_which_candidates_a_limit_selects_and_makes_no_request(fetch_root: Path) -> None:
    write_candidates(fetch_root, *CANDS)
    tr = FakeTransport({})
    code, out = fetch(fetch_root, tr, "--limit", "1")
    assert code == 2 and tr.calls == []
    assert "run 5001" in out and "run 5002" not in out and "run 5003" not in out
    assert "1 run(s) to fetch" in out and "2 further" in out


def test_limit_skips_over_already_fetched_candidates_named_by_job(fetch_root: Path) -> None:
    named = [{**c, "job": "build"} for c in CANDS]
    write_candidates(fetch_root, named[0])
    assert fetch(fetch_root, FakeTransport(_three_runs()), "--yes-fetch")[0] == 0
    write_candidates(fetch_root, *named)
    tr = FakeTransport(_three_runs())
    code, plan = fetch(fetch_root, tr, "--limit", "1")  # plan only: the fetched one is refused, run 5002 is next
    assert code == 2 and tr.calls == [] and "already fetched as hc-01" in plan
    assert "run 5002" in plan and "run 5003" not in plan and "1 run(s) to fetch" in plan
    code, out = fetch(fetch_root, FakeTransport(_three_runs()), "--yes-fetch", "--limit", "1")
    assert code == 0, out
    assert record_of(fetch_root, "hc-02")["source"]["run_id"] == 5002  # not 5001 again, not 5003 yet
    assert not (fetch_root / "fetched" / "hc-03.json").exists()


def test_an_unnamed_candidate_found_already_fetched_does_not_use_up_the_limit(fetch_root: Path) -> None:
    write_candidates(fetch_root, CANDS[0])
    assert fetch(fetch_root, FakeTransport(_three_runs()), "--yes-fetch")[0] == 0
    write_candidates(fetch_root, *CANDS)  # no job names: "already fetched" is only known at fetch time
    code, out = fetch(fetch_root, FakeTransport(_three_runs()), "--yes-fetch", "--limit", "1")
    assert code == 0, out
    assert "the failed job is already fetched as hc-01" in out
    assert record_of(fetch_root, "hc-03")["source"]["run_id"] == 5002
    assert not (fetch_root / "fetched" / "hc-04.json").exists()


def test_limit_larger_than_the_list_changes_nothing(fetch_root: Path) -> None:
    write_candidates(fetch_root, *CANDS)
    code, out = fetch(fetch_root, FakeTransport(_three_runs()), "--yes-fetch", "--limit", "50")
    assert code == 0 and len(list((fetch_root / "fetched").iterdir())) == 3 and "further" not in out


@pytest.mark.parametrize("bad", ["0", "-1", "x", "1.5", ""])
def test_limit_must_be_a_positive_whole_number(fetch_root: Path, bad: str) -> None:
    write_candidates(fetch_root, *CANDS)
    with pytest.raises(SystemExit):
        fetch(fetch_root, FakeTransport({}), "--limit", bad)


def test_the_limit_cut_is_pure() -> None:
    rows = [hf.PlanRow(hf.Candidate(REPO, i, None), f"hc-{i:02d}", None) for i in (1, 2, 3)]
    assert hf.limit_plan(rows, None) == (rows, 0)
    kept, left = hf.limit_plan(rows, 2)
    assert [r.case_id for r in kept] == ["hc-01", "hc-02"] and left == 1


# -- the source exposes the run's other jobs as siblings, from real counts only ---------------------
COUNTS6 = hj.JobCounts(total=6, success=3, failure=1, cancelled=1, skipped=1, other=0)


def _inp(case_id: str = "hc-01", counts: hj.JobCounts | None = COUNTS6, tmp: Path | None = None):  # type: ignore[no-untyped-def]
    assert tmp is not None
    root = make_holdout(tmp / "holdout")
    h = hd.Holdout.load(root, now=NOW)
    return replace(hr.inputs_for(h.case(case_id), h.dev_log(case_id), root), job_counts=counts)


def _leaves(node):  # type: ignore[no-untyped-def]
    return [node] if not node.children else [x for c in node.children for x in _leaves(c)]


def test_siblings_carry_only_real_counts_and_conclusions(tmp_path: Path) -> None:
    src = HoldoutSource(_inp(tmp=tmp_path))
    root = src.get_execution("hc-01").root
    assert len(_leaves(root)) == 6  # what the production derivation counts
    job, *sibs = root.children
    assert job.name == "job-1" and [c.status for c in job.children] == ["failed"]
    assert [s.status for s in sibs] == ["cancelled", "skipped", "success", "success", "success"]
    assert [s.name for s in sibs] == [f"sibling-{n}" for n in range(2, 7)]
    for s in sibs:
        assert s.children == [] and s.error_summary == "" and s.node_id == s.name
    assert [n.node_id for n in src.get_failed_leaf_nodes("hc-01")] == [
        "step-1"
    ]  # only the stored job is failed-and-stored


def test_a_failed_sibling_is_shown_failed_but_has_no_log(tmp_path: Path) -> None:
    counts = hj.JobCounts(total=4, success=1, failure=3, cancelled=0, skipped=0, other=0)
    src = HoldoutSource(_inp(counts=counts, tmp=tmp_path))
    sibs = src.get_execution("hc-01").root.children[1:]
    assert [s.status for s in sibs] == ["failed", "failed", "success"]
    assert len(src.get_failed_leaf_nodes("hc-01")) == 1
    with pytest.raises(SourceError):
        src.get_step_logs("hc-01", "sibling-2", 100)


def test_no_counts_or_one_job_means_the_lone_job_tree(tmp_path: Path) -> None:
    for i, counts in enumerate((None, hj.JobCounts(1, 0, 1, 0, 0, 0))):
        root = HoldoutSource(_inp(counts=counts, tmp=tmp_path / f"c{i}")).get_execution("hc-01").root
        assert root.node_id == "job" and len(_leaves(root)) == 1


def test_siblings_have_no_label_or_join_key_fields(tmp_path: Path) -> None:
    assert set(hj.JobCounts.__dataclass_fields__) == {"total", "success", "failure", "cancelled", "skipped", "other"}
    src = HoldoutSource(_inp(tmp=tmp_path))
    assert set(src.get_execution("hc-01").refs) == {"commit", "run_id", "job", "time_basis"}
    from driftgate.domain import Node

    assert set(Node.__dataclass_fields__) == {"node_id", "name", "status", "parent_id", "children", "error_summary"}


def test_siblings_are_not_executions_so_fleet_and_flake_tools_stay_honest(tmp_path: Path) -> None:
    counts = hj.JobCounts(total=7, success=1, failure=5, cancelled=1, skipped=0, other=0)  # five FAILED jobs in the run
    src = HoldoutSource(_inp(counts=counts, tmp=tmp_path))
    assert [s.execution_id for s in src.list_executions(None, None)] == ["hc-01"]
    assert src.list_executions(None, {"status": "success"}) == []
    ctx = ToolContext(src)
    fleet = dispatch(ctx, "fleet_correlate", {"execution_id": "hc-01"}, "c1")
    assert fleet.ok and fleet.data["fleet_wide"] is False and fleet.data["executions_affected"] == 0
    assert fleet.data["dimensions"] == []
    flake = dispatch(ctx, "flake_history", {"execution_id": "hc-01"}, "c2")
    assert flake.ok and flake.data["precedent"] is False
    tree = dispatch(ctx, "get_execution", {"execution_id": "hc-01"}, "c3")
    assert tree.ok and [f["node_id"] for f in tree.data["failed_leaves"]] == ["step-1"]


# -- the gate: real total, no override --------------------------------------------------------------
def _run(case_id: str, counts: hj.JobCounts | None, tmp: Path) -> hr.CaseRun:
    return hr.run_case(_inp(case_id, counts, tmp), lambda eid, _b: hr.plumbing_model(eid))


def test_a_multi_job_run_gives_the_gate_set_one_of_the_real_total_and_is_no_longer_refused(tmp_path: Path) -> None:
    run = _run("hc-01", COUNTS6, tmp_path)
    p = run.proposal
    assert (p["proposed_set_size"], p["resource_set_total"]) == (1, 6)
    assert p["kind"] == "rerun" and p["gate_decision"] == "allowed"
    assert p["refused_by_abort_ceiling_only"] is False
    assert run.action == {"kind": "rerun"} and run.gate_decision == "allowed"  # the headline, after the gates


def test_failed_siblings_in_the_same_run_do_not_trip_the_fleet_blast_radius(tmp_path: Path) -> None:
    counts = hj.JobCounts(total=6, success=1, failure=5, cancelled=0, skipped=0, other=0)
    p = _run("hc-01", counts, tmp_path).proposal
    assert (p["proposed_set_size"], p["resource_set_total"]) == (1, 6)
    assert p["gate_decision"] == "allowed" and p["gate_reason_code"] != "fleet_blast_radius"


def test_a_single_job_run_is_still_refused_by_the_ceiling(tmp_path: Path) -> None:
    one = hj.JobCounts(total=1, success=0, failure=1, cancelled=0, skipped=0, other=0)
    for i, counts in enumerate((one, None)):  # a stored count of one, and an unknown count (the lone job)
        run = _run("hc-01", counts, tmp_path / f"c{i}")
        p = run.proposal
        assert (p["proposed_set_size"], p["resource_set_total"]) == (1, 1)
        assert p["gate_decision"] == "refused" and p["gate_reason_code"] == "abort_ceiling_set_100pct"
        assert p["refused_by_abort_ceiling_only"] is True and run.action == {"kind": "escalate"}


def test_the_total_two_boundary_is_not_refused_by_the_ceiling(tmp_path: Path) -> None:
    two = hj.JobCounts(total=2, success=1, failure=1, cancelled=0, skipped=0, other=0)
    p = _run("hc-01", two, tmp_path).proposal
    assert (p["proposed_set_size"], p["resource_set_total"]) == (1, 2) and p["gate_decision"] == "allowed"


def test_other_gates_still_apply_with_a_real_total(tmp_path: Path) -> None:
    p = _run("hc-02", COUNTS6, tmp_path).proposal  # a signature with no Tier 0 rule: nothing to allow
    assert p["kind"] == "escalate" and p["refused_by_abort_ceiling_only"] is False


def test_the_runner_never_hands_the_gate_an_override() -> None:
    src = (Path(hr.__file__)).read_text(encoding="utf-8")
    for needle in ("resource_set_total=", "proposed_set_size=", "replace(facts", "assume_other_jobs"):
        assert needle not in src, needle


# -- the loader and preflight -----------------------------------------------------------------------
def _write_fetched(root: Path, case_id: str, total: int, failed: int = 1) -> None:
    n = int(case_id.split("-")[1])
    rec = {
        "schema_version": 3,
        "case_id": case_id,
        "source": {
            "repo": "example-org/example-repo",
            "run_id": 1000 + n,
            "job": f"job-{n}",
            "commit_sha": f"{n:040x}",
            "url": f"https://github.com/example-org/example-repo/actions/runs/{1000 + n}",
            "fetch_date": "2026-10-14",
        },
        "job_id": 77,
        "run_attempt": 1,
        "step": {
            "name": "Pull image",
            "number": 3,
            "started_at": "2026-10-01T10:00:04Z",
            "completed_at": "2026-10-01T10:00:06Z",
        },
        "other_failed_steps": [],
        "extraction": {
            "method": "error-anchor-v1",
            "job_log_lines": 10,
            "start_line": 2,
            "end_line": 4,
            "error_line": 4,
            "error_timestamp": "2026-10-01T10:00:06.0000000Z",
            "inner_group_lines": 0,
            "stored_lines": 3,
        },
        "log_sha256": "0" * 64,
        "log_bytes": 10,
        "jobs": {
            "total": total,
            "success": total - failed,
            "failure": failed,
            "cancelled": 0,
            "skipped": 0,
            "other": 0,
        },
    }
    (root / "fetched").mkdir(exist_ok=True)
    (root / "fetched" / f"{case_id}.json").write_text(json.dumps(rec), encoding="utf-8")


def test_inputs_for_reads_the_step_name_and_counts_from_a_v3_record(tmp_path: Path) -> None:
    root = make_holdout(tmp_path / "holdout")
    _write_fetched(root, "hc-01", 6)
    h = hd.Holdout.load(root, now=NOW)
    inp = hr.inputs_for(h.case("hc-01"), h.dev_log("hc-01"), root)
    assert inp.step_name == "Pull image" and inp.job_counts == hj.JobCounts(6, 5, 1, 0, 0, 0)
    inp2 = hr.inputs_for(h.case("hc-02"), h.dev_log("hc-02"), root)  # no record: unknown, not invented
    assert inp2.job_counts is None and inp2.step_name == "job-2"


def test_a_v2_record_stops_the_runner_with_counts_only(tmp_path: Path) -> None:
    root = make_holdout(tmp_path / "holdout")
    _write_fetched(root, "hc-11", 3)  # a test-split case: the message must not name it
    p = root / "fetched" / "hc-11.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["schema_version"] = 2
    del rec["jobs"]
    p.write_text(json.dumps(rec), encoding="utf-8")
    with pytest.raises(hr.RunRefused) as exc:
        hr.preflight(root, NOW, lambda _r: "")
    text = " ".join(exc.value.reasons)
    assert "1 fetched record(s)" in text and "version 2" in text and "hc-11" not in text


# -- the report -------------------------------------------------------------------------------------
def test_the_report_shows_jobs_per_dev_case_and_aggregates_for_test(tmp_path: Path) -> None:
    from test_holdout_run import real_records  # same helper the runner tests use

    root = make_holdout(tmp_path / "holdout")
    for cid, total in (("hc-01", 6), ("hc-02", 1), ("hc-11", 1), ("hc-12", 3)):
        _write_fetched(root, cid, total)
    real_records(root, tmp_path)
    text, _ = he.evaluate(root, NOW)
    assert text is not None
    assert "jobs in run" in text
    assert "1 of 2 dev cases are single-job runs." in text and "1 of 2 test cases are single-job runs." in text
    assert he.STRUCTURAL_ZERO_NOTE in text
    assert "hc-11" not in text and "hc-12" not in text  # sealed: counts only
    dev_rows = [line for line in text.splitlines() if line.startswith("| hc-01") or line.startswith("| hc-02")]
    assert any(line.rstrip().endswith("| 6 |") for line in dev_rows if line.startswith("| hc-01"))
    assert any(line.rstrip().endswith("| 1 |") for line in dev_rows if line.startswith("| hc-02"))
    # multi-job case: the headline rerun went through; the single-job one was refused by the ceiling alone
    assert '"kind": "rerun"' in (root / "runs.json").read_text(encoding="utf-8")


def test_no_single_job_runs_means_no_structural_zero_note(tmp_path: Path) -> None:
    from test_holdout_run import real_records

    root = make_holdout(tmp_path / "holdout")
    for cid in ("hc-01", "hc-02", "hc-11", "hc-12"):
        _write_fetched(root, cid, 4)
    real_records(root, tmp_path)
    text, _ = he.evaluate(root, NOW)
    assert text is not None and he.STRUCTURAL_ZERO_NOTE not in text
    assert "0 of 2 dev cases are single-job runs." in text


def test_the_structural_zero_note_now_names_single_job_runs_only() -> None:
    assert "single-job runs" in he.STRUCTURAL_ZERO_NOTE and "several jobs" in he.STRUCTURAL_ZERO_NOTE
