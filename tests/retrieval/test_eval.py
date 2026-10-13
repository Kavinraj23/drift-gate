"""Recording incidents from outcomes, and the offline on/off comparison over the synthetic scenarios."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from driftgate.eval import e2e
from driftgate.eval.ground_truth import FailureLabel
from driftgate.eval.retrieval_eval import NO_EVIDENCE, arm_stats, render, run_arms, shipping_verdict
from driftgate.orchestrator import CLOSED
from driftgate.retrieval import HistoryIndex
from driftgate.tools.similar_history import start_epoch


def test_a_finished_incident_is_recorded_with_agent_visible_fields_only(
    dataset_dir: Path, pick: Callable[..., FailureLabel]
) -> None:
    label = pick("transient_throttling", fleet=False)
    idx = HistoryIndex()
    res = e2e.run_scenario(dataset_dir, label, False, mode="fake", history=idx)
    assert res.outcome is not None and len(idx) == 1
    row = idx._con.execute(
        "SELECT m.execution_id, t.signature, t.fingerprint, t.hypothesis, t.action, t.outcome "
        "FROM incident_text t JOIN incident_meta m ON m.id = t.rowid"
    ).fetchone()
    assert row[0] == label.execution_id
    assert row[2] == res.outcome.report.fingerprint
    assert row[3] == res.outcome.report.hypothesis
    # nothing from the label's free text or fix ends up in the index
    blob = " ".join(str(c) for c in row) + str(idx._con.execute("SELECT excerpt FROM incident_text").fetchall())
    for secret in (label.label_note, label.fix_diff):
        assert not secret or secret not in blob


def test_closed_cases_are_not_indexed(dataset_dir: Path, truth) -> None:  # type: ignore[no-untyped-def]
    closed = next(s for s in truth.scenarios() if s.disposition == "close")
    idx = HistoryIndex()
    res = e2e.run_scenario(dataset_dir, closed, False, mode="fake", history=idx)
    assert res.outcome is not None and res.outcome.kind == CLOSED
    assert len(idx) == 0


def test_the_recorded_finish_time_is_the_executions_own(dataset_dir: Path, pick: Callable[..., FailureLabel]) -> None:
    from driftgate.adapters.synthetic import SyntheticSource

    label = pick("transient_throttling", fleet=False)
    idx = HistoryIndex()
    e2e.run_scenario(dataset_dir, label, False, mode="fake", history=idx)
    ex = SyntheticSource(dataset_dir).get_execution(label.execution_id)
    (finished,) = idx._con.execute("SELECT finished_at FROM incident_meta").fetchone()
    assert finished == start_epoch(ex.finished_at or ex.started_at)


@pytest.fixture(scope="module")
def arms(dataset_dir: Path):  # type: ignore[no-untyped-def]
    return run_arms(dataset_dir)


def test_the_comparison_runs_every_scenario_in_both_arms_without_leaks_or_failures(arms) -> None:  # type: ignore[no-untyped-def]
    off, on, plumbing = arms
    assert len(off.cases) == len(on.cases) == 33
    assert plumbing.cases == 33
    assert plumbing.leak_violations == 0
    assert plumbing.calls_failed == 0
    assert plumbing.cases_calling_tool > 0 and plumbing.calls_with_matches > 0


def test_off_arm_is_the_plain_pipeline_and_on_arm_does_not_raise_false_remediation(arms) -> None:  # type: ignore[no-untyped-def]
    off, on, _ = arms
    so, sn = arm_stats(off), arm_stats(on)
    assert so.false_remediation.numerator == 0
    assert sn.false_remediation.numerator <= so.false_remediation.numerator
    assert [c.scenario_id for c in off.cases] == [c.scenario_id for c in on.cases]


def test_the_report_is_honest_and_the_verdict_is_stays_off(arms) -> None:  # type: ignore[no-untyped-def]
    off, on, plumbing = arms
    verdict = shipping_verdict(arm_stats(off), arm_stats(on), recorded=False)
    text = render(off, on, plumbing, verdict)
    assert verdict.reason == NO_EVIDENCE
    assert f"**Verdict: {NO_EVIDENCE}**" in text
    assert "SCRIPTED" in text and "NOT evidence" in text
    lowered = text.lower()
    for claim in ("improves", "improved", "better than", "outperform"):
        assert claim not in lowered.replace("improves remediation success", "")
    assert "/" in text  # counts beside percentages


def _iso(epoch: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).isoformat()


def test_leak_check_judges_against_the_known_case_and_catches_a_mis_bound_call() -> None:
    from driftgate.eval.retrieval_eval import _leaks_in
    from driftgate.tools.base import ToolResult

    start = 1_000_000.0

    def match(eid: str, finished: float) -> dict[str, object]:
        return {"execution_id": eid, "finished_at": _iso(finished)}

    clean = ToolResult("c", "similar_history", True, {"execution_id": "case", "matches": [match("p", start - 5)]})
    assert _leaks_in([clean], "case", start) == 0
    # a call answered for another (later) execution: its matches look fine for that execution, not for this case
    mis_bound = ToolResult("c", "similar_history", True, {"execution_id": "later", "matches": [match("p", start - 5)]})
    assert _leaks_in([mis_bound], "case", start) == 1
    late = ToolResult("c", "similar_history", True, {"execution_id": "case", "matches": [match("x", start + 5)]})
    self_hit = ToolResult("c", "similar_history", True, {"execution_id": "case", "matches": [match("case", start - 5)]})
    assert _leaks_in([late], "case", start) == 1 and _leaks_in([self_hit], "case", start) == 1
    assert _leaks_in([ToolResult("c", "similar_history", False, error="refused")], "case", start) == 0
