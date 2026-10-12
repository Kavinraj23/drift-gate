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
from driftgate.tools.similar_history import start_epoch


@pytest.fixture
def case(pick: Callable[..., FailureLabel], dataset_dir: Path) -> tuple[FailureLabel, SyntheticSource, float]:
    label = pick("transient_throttling", fleet=False)
    source = SyntheticSource(dataset_dir)
    return label, source, start_epoch(source.get_execution(label.execution_id).started_at)


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
    res = dispatch(ToolContext(source, history=idx), "similar_history", {"execution_id": label.execution_id}, "call-7")
    assert res.ok and res.source == "call-7" and res.tool == "similar_history"
    got = [m["execution_id"] for m in res.data["matches"]]
    assert set(got) == {"past-1", "past-2"}
    assert all(m["outcome"] == "verified" and m["action_taken"] == "rerun_failed_job" for m in res.data["matches"])
    assert json.loads(json.dumps(res.to_dict()))["source"] == "call-7"


def test_a_model_cannot_widen_the_search_by_adding_arguments(case) -> None:  # type: ignore[no-untyped-def]
    label, source, _ = case
    ctx = ToolContext(source, history=HistoryIndex())
    for extra in ({"query": "x OR *"}, {"as_of": "2999-01-01T00:00:00Z"}, {"case_id": "other"}):
        res = dispatch(ctx, "similar_history", {"execution_id": label.execution_id, **extra}, "c")
        assert not res.ok and "invalid arguments" in res.error


def test_query_is_derived_from_the_cases_own_error_not_from_the_model(case) -> None:  # type: ignore[no-untyped-def]
    label, source, start = case
    idx = HistoryIndex()
    idx.add(_record("related", start - 100))
    idx.add(_record("unrelated", start - 100, signature="npm_peer_dep", excerpt="peer dependency conflict ERESOLVE"))
    res = dispatch(ToolContext(source, history=idx), "similar_history", {"execution_id": label.execution_id}, "c")
    got = res.data["matches"]
    assert got[0]["execution_id"] == "related"
    assert all(m["bm25_score"] >= got[0]["bm25_score"] for m in got)


def test_not_offered_means_not_runnable(case) -> None:  # type: ignore[no-untyped-def]
    label, source, _ = case
    res = dispatch(ToolContext(source), "similar_history", {"execution_id": label.execution_id}, "c")
    assert not res.ok and "not enabled" in res.error


def test_output_is_redacted(case) -> None:  # type: ignore[no-untyped-def]
    label, source, start = case
    idx = HistoryIndex()
    key = "AKIA" + "IOSFODNN7EXAMPLE"
    idx.add(_record("past", start - 5, hypothesis=f"rotated key {key} still in use"))
    res = dispatch(ToolContext(source, history=idx), "similar_history", {"execution_id": label.execution_id}, "c")
    assert res.data["matches"] and key not in json.dumps(res.data)


def test_a_successful_execution_has_nothing_to_look_up(dataset_dir: Path) -> None:
    source = SyntheticSource(dataset_dir)
    ok = next(s for s in source.list_executions(None, None) if s.status == "success")
    res = dispatch(
        ToolContext(source, history=HistoryIndex()), "similar_history", {"execution_id": ok.execution_id}, "c"
    )
    assert res.ok and res.data["matches"] == []


def test_the_call_counts_against_the_eight_call_budget_and_evidence_can_cite_it(
    make_orch: Callable[..., Orchestrator], case
) -> None:  # type: ignore[no-untyped-def]
    label, source, start = case
    idx = HistoryIndex()
    idx.add(_record("past", start - 100))
    eid = label.execution_id
    calls = [ToolCall(f"c{i}", "similar_history", {"execution_id": eid}) for i in range(9)]
    # Distinct ids, nine calls: the ninth is refused by the budget exactly as for any other tool.
    script = [ModelResponse(tool_calls=[c]) for c in calls]
    budget = InvestigationBudget(InvestigationLimits())
    inv = Investigator(FakeModel(script), ToolContext(source, history=idx), budget, optional_tools=("similar_history",))
    out = inv.investigate(eid)
    assert out.stop_reason == "budget_tool_calls"
    assert len(out.tool_results) == 8 and budget.limits.max_tool_calls == 8
    assert out.report.budget_truncated

    cited = [
        ToolCall("c0", "similar_history", {"execution_id": eid}),
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
