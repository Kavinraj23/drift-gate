"""An unreadable case start time refuses similar_history, keeps investigating, and leaves an audit line."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from driftgate.agents.investigator import HISTORY_SKIPPED_UNBOUND
from driftgate.eval.ground_truth import FailureLabel
from driftgate.llm.fake import FakeModel
from driftgate.llm.types import ModelResponse, ToolCall
from driftgate.orchestrator import Orchestrator
from driftgate.retrieval import HistoryIndex

REPORT = {
    "classification": "transient",
    "layer": "L4",
    "confidence": 0.5,
    "hypothesis": "h",
    "evidence": [{"source": "c0", "finding": "history lookup", "supports": "h"}],
}


def _factory(_e: str, _b: object) -> FakeModel:
    return FakeModel(
        [
            ModelResponse(tool_calls=[ToolCall("c0", "similar_history", {})]),
            ModelResponse(tool_calls=[ToolCall("r", "submit_report", REPORT)]),
        ]
    )


def _run(make_orch: Callable[..., Orchestrator], label: FailureLabel):  # type: ignore[no-untyped-def]
    orch = make_orch(
        label, model_factory=_factory, history=HistoryIndex(), optional_tools=frozenset({"similar_history"})
    )
    return orch, orch.handle(label.execution_id)


def test_unreadable_start_time_refuses_the_tool_continues_and_audits(
    make_orch: Callable[..., Orchestrator], pick: Callable[..., FailureLabel], monkeypatch: pytest.MonkeyPatch
) -> None:
    label = pick("transient_throttling", fleet=False)

    def boom(_value: object) -> float:
        raise ValueError("secret-looking raw value 12345")

    monkeypatch.setattr("driftgate.tools.similar_history.start_epoch", boom)
    orch, out = _run(make_orch, label)
    inv = out.investigation
    assert inv is not None and inv.history_skipped == HISTORY_SKIPPED_UNBOUND
    call = next(r for r in inv.tool_results if r.tool == "similar_history")
    assert not call.ok and "not bound" in call.error
    assert inv.stop_reason == "report"  # the investigation went on and finished its report
    lines = [e for e in orch.audit.read("proposal") if e.payload.get("stage") == "history_skipped"]
    assert len(lines) == 1 and lines[0].execution_id == label.execution_id
    assert lines[0].payload == {"stage": "history_skipped", "reason": HISTORY_SKIPPED_UNBOUND}
    assert "12345" not in str(lines[0])


def test_readable_start_time_writes_no_skip_line(
    make_orch: Callable[..., Orchestrator], pick: Callable[..., FailureLabel]
) -> None:
    label = pick("transient_throttling", fleet=False)
    orch, out = _run(make_orch, label)
    assert out.investigation is not None and out.investigation.history_skipped == ""
    assert not [e for e in orch.audit.read("proposal") if e.payload.get("stage") == "history_skipped"]
