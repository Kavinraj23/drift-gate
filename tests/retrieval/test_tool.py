"""The similar_history tool against the seeded dataset: ids, query derivation, leakage, redaction, budget."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from driftgate.adapters.synthetic import SyntheticSource
from driftgate.agents.investigator import Investigator
from driftgate.eval.ground_truth import FailureLabel
from driftgate.llm.budget import InvestigationBudget
from driftgate.llm.config import InvestigationLimits
from driftgate.llm.fake import FakeModel
from driftgate.llm.types import ModelResponse, ToolCall
from driftgate.orchestrator import Orchestrator
from driftgate.retrieval import HistoryIndex, IncidentRecord
from driftgate.tools import ToolContext, dispatch
from driftgate.tools.similar_history import key_excerpt, start_epoch


@pytest.fixture
def case(pick: Callable[..., FailureLabel], dataset_dir: Path) -> tuple[FailureLabel, SyntheticSource, float]:
    label = pick("transient_throttling", fleet=False)
    source = SyntheticSource(dataset_dir)
    return label, source, start_epoch(source.get_execution(label.execution_id).started_at)


def _bound(source: SyntheticSource, idx: HistoryIndex, eid: str) -> ToolContext:
    """What the investigator does before a run: the case is bound by code."""
    return ToolContext(source, history=idx).bind_case(eid)


def _record(eid: str, finished_at: float, **kw: str) -> IncidentRecord:
    base = {
        "signature": "aws_throttling",
        "fingerprint": "f-" + eid,
        "excerpt": "ThrottlingException Rate exceeded",
        "hypothesis": "API throttling",
        "action": "rerun_failed_job",
        "outcome": "verified",
    }
    return IncidentRecord(eid, finished_at, **{**base, **kw})  # type: ignore[arg-type]


def test_result_carries_the_call_id_and_only_earlier_non_self_incidents(case) -> None:  # type: ignore[no-untyped-def]
    label, source, start = case
    idx = HistoryIndex()
    idx.add(_record("past-1", start - 3600))
    idx.add(_record("past-2", start - 60, signature="aws_throttling", excerpt="Rate exceeded again"))
    idx.add(_record("same-instant", start))
    idx.add(_record("future", start + 60))
    idx.add(_record(label.execution_id, start - 10))
    res = dispatch(_bound(source, idx, label.execution_id), "similar_history", {}, "call-7")
    assert res.ok and res.source == "call-7" and res.tool == "similar_history"
    got = [m["execution_id"] for m in res.data["matches"]]
    assert set(got) == {"past-1", "past-2"}
    assert all(m["outcome"] == "verified" and m["action_taken"] == "rerun_failed_job" for m in res.data["matches"])
    assert json.loads(json.dumps(res.to_dict()))["source"] == "call-7"


def test_a_model_cannot_widen_the_search_by_adding_arguments(case) -> None:  # type: ignore[no-untyped-def]
    label, source, _ = case
    ctx = _bound(source, HistoryIndex(), label.execution_id)
    for extra in ({"query": "x OR *"}, {"as_of": "2999-01-01T00:00:00Z"}, {"case_id": "other"}, {"execution_id": "x"}):
        res = dispatch(ctx, "similar_history", extra, "c")
        assert not res.ok and "invalid arguments" in res.error


def test_query_is_derived_from_the_cases_own_error_not_from_the_model(case) -> None:  # type: ignore[no-untyped-def]
    label, source, start = case
    idx = HistoryIndex()
    idx.add(_record("related", start - 100))
    idx.add(_record("unrelated", start - 100, signature="npm_peer_dep", excerpt="peer dependency conflict ERESOLVE"))
    res = dispatch(_bound(source, idx, label.execution_id), "similar_history", {}, "c")
    got = res.data["matches"]
    assert got[0]["execution_id"] == "related"
    assert all(m["bm25_score"] >= got[0]["bm25_score"] for m in got)


def test_not_offered_means_not_runnable(case) -> None:  # type: ignore[no-untyped-def]
    label, source, _ = case
    res = dispatch(ToolContext(source), "similar_history", {}, "c")
    assert not res.ok and "not enabled" in res.error


def test_output_is_redacted(case) -> None:  # type: ignore[no-untyped-def]
    label, source, start = case
    idx = HistoryIndex()
    key = "AKIA" + "IOSFODNN7EXAMPLE"
    idx.add(_record("past", start - 5, hypothesis=f"rotated key {key} still in use"))
    res = dispatch(_bound(source, idx, label.execution_id), "similar_history", {}, "c")
    assert res.data["matches"] and key not in json.dumps(res.data)


def test_a_successful_execution_has_nothing_to_look_up(dataset_dir: Path) -> None:
    source = SyntheticSource(dataset_dir)
    ok = next(s for s in source.list_executions(None, None) if s.status == "success")
    res = dispatch(_bound(source, HistoryIndex(), ok.execution_id), "similar_history", {}, "c")
    assert res.ok and res.data["matches"] == []


def test_the_call_counts_against_the_eight_call_budget_and_evidence_can_cite_it(
    make_orch: Callable[..., Orchestrator], case
) -> None:  # type: ignore[no-untyped-def]
    label, source, start = case
    idx = HistoryIndex()
    idx.add(_record("past", start - 100))
    eid = label.execution_id
    calls = [ToolCall(f"c{i}", "similar_history", {}) for i in range(9)]
    # Distinct ids, nine calls: the ninth is refused by the budget exactly as for any other tool.
    script = [ModelResponse(tool_calls=[c]) for c in calls]
    budget = InvestigationBudget(InvestigationLimits())
    inv = Investigator(FakeModel(script), ToolContext(source, history=idx), budget, optional_tools=("similar_history",))
    out = inv.investigate(eid)
    assert out.stop_reason == "budget_tool_calls"
    assert len(out.tool_results) == 8 and budget.limits.max_tool_calls == 8
    assert out.report.budget_truncated

    cited = [
        ToolCall("c0", "similar_history", {}),
    ]
    report = {
        "classification": "transient",
        "layer": "L4",
        "confidence": 0.5,
        "hypothesis": "h",
        "evidence": [
            {"source": "c0", "finding": "an earlier incident matched", "supports": "h"},
            {"source": "never-issued", "finding": "uncited", "supports": "h"},
        ],
    }
    script2 = [
        ModelResponse(tool_calls=cited),
        ModelResponse(tool_calls=[ToolCall("r", "submit_report", report)]),
    ]
    inv2 = Investigator(
        FakeModel(script2),
        ToolContext(source, history=idx),
        InvestigationBudget(InvestigationLimits()),
        optional_tools=("similar_history",),
    )
    done = inv2.investigate(eid)
    assert [e.source for e in done.report.evidence] == ["c0"]  # invariant 7 holds for the new tool too
    assert done.evidence_dropped == 1


# -- round 2: the case under investigation is bound by code, never named by the model (invariant 16) -----------------
def _two_cases(dataset_dir: Path) -> tuple[SyntheticSource, str, float, str, float]:
    """The earliest and the latest failed executions in the dataset, with their start times."""
    source = SyntheticSource(dataset_dir)
    rows = sorted(
        (start_epoch(s.started_at), s.execution_id) for s in source.list_executions(None, None) if s.status == "failed"
    )
    (t_early, early), (t_late, late) = rows[0], rows[-1]
    assert t_early + 20 < t_late
    return source, early, t_early, late, t_late


def test_the_schema_takes_no_id_at_all(case) -> None:  # type: ignore[no-untyped-def]
    label, source, _ = case
    ctx = _bound(source, HistoryIndex(), label.execution_id)
    for args in ({"execution_id": label.execution_id}, {"execution_id": "some-later-execution"}):
        res = dispatch(ctx, "similar_history", args, "c")
        assert not res.ok and "invalid arguments" in res.error


def test_a_model_naming_a_later_execution_cannot_see_incidents_after_the_real_case(dataset_dir: Path) -> None:
    source, early, t_early, late, _ = _two_cases(dataset_dir)
    idx = HistoryIndex()
    idx.add(_record("after-real-case", t_early + 10))  # finished after the real (early) case started
    ctx = _bound(source, idx, early)
    for args in ({"execution_id": late}, {}):
        res = dispatch(ctx, "similar_history", args, "c")
        assert not res.ok or res.data["matches"] == []  # without the fix, naming `late` returned "after-real-case"
        assert not res.ok or res.data["execution_id"] == early


def test_a_model_naming_another_earlier_execution_cannot_unexclude_the_real_case(dataset_dir: Path) -> None:
    source, early, _, late, t_late = _two_cases(dataset_dir)
    idx = HistoryIndex()
    ctx = _bound(source, idx, late)
    analysis = ctx.analyzer.analyze(late)
    assert analysis is not None
    like = {"signature": analysis.primary.id, "excerpt": key_excerpt(ctx, analysis)}  # text that matches the case
    idx.add(_record(late, t_late - 1, **like))  # the real case is already in the index
    idx.add(_record("ok-past", t_late - 5, **like))
    for args in ({"execution_id": early}, {}):
        res = dispatch(ctx, "similar_history", args, "c")
        if res.ok:
            got = [m["execution_id"] for m in res.data["matches"]]
            assert late not in got and got == ["ok-past"]


def test_passing_the_real_id_is_not_accepted_either_and_the_bound_case_is_used(case) -> None:  # type: ignore[no-untyped-def]
    label, source, start = case
    idx = HistoryIndex()
    idx.add(_record("past", start - 10))
    ctx = _bound(source, idx, label.execution_id)
    assert not dispatch(ctx, "similar_history", {"execution_id": label.execution_id}, "c").ok
    ok = dispatch(ctx, "similar_history", {}, "c")
    assert ok.ok and ok.data["execution_id"] == label.execution_id
    assert [m["execution_id"] for m in ok.data["matches"]] == ["past"]


def test_an_unbound_context_refuses(case) -> None:  # type: ignore[no-untyped-def]
    label, source, start = case
    idx = HistoryIndex()
    idx.add(_record("past", start - 10))
    res = dispatch(ToolContext(source, history=idx), "similar_history", {}, "c")
    assert not res.ok and "not bound" in res.error and not res.data


def test_the_bound_case_already_in_the_index_is_never_returned(case) -> None:  # type: ignore[no-untyped-def]
    label, source, start = case
    idx = HistoryIndex()
    idx.add(_record(label.execution_id, start - 1))
    idx.add(_record("past", start - 2))
    res = dispatch(_bound(source, idx, label.execution_id), "similar_history", {}, "c")
    assert [m["execution_id"] for m in res.data["matches"]] == ["past"]


def test_the_investigator_binds_the_case_every_round_and_a_model_id_is_refused(case) -> None:  # type: ignore[no-untyped-def]
    label, source, start = case
    idx = HistoryIndex()
    idx.add(_record("past", start - 10))
    idx.add(_record("future", start + 10))
    report = {"classification": "transient", "layer": "L4", "confidence": 0.5, "hypothesis": "h", "evidence": []}
    for rnd in range(2):  # round 1 and the re-investigation round
        script = [
            ModelResponse(tool_calls=[ToolCall("a", "similar_history", {"execution_id": "other"})]),
            ModelResponse(tool_calls=[ToolCall("b", "similar_history", {})]),
            ModelResponse(tool_calls=[ToolCall("r", "submit_report", report)]),
        ]
        inv = Investigator(
            FakeModel(script),
            ToolContext(source, history=idx),  # the caller never binds; the investigator does
            InvestigationBudget(InvestigationLimits()),
            optional_tools=("similar_history",),
        )
        out = inv.investigate(label.execution_id, "round 2 context" if rnd else "")
        a, b = out.tool_results[:2]
        assert not a.ok and b.ok
        assert [m["execution_id"] for m in b.data["matches"]] == ["past"]
