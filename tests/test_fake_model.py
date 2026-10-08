"""Smoke test for the scripted fake model."""

from __future__ import annotations

import pytest

from driftgate.llm.fake import FakeModel, ScriptExhaustedError
from driftgate.llm.types import ModelClient, ModelRequest, ModelResponse, ToolCall, Usage


def _req() -> ModelRequest:
    return ModelRequest(system="s", messages=[{"role": "user", "content": "hi"}])


def test_fake_returns_scripted_tool_call_then_text() -> None:
    fake: ModelClient = FakeModel(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="call_1", name="get_execution", input={"execution_id": "e1"})],
                stop_reason="tool_use",
            ),
            ModelResponse(text="done", usage=Usage(input_tokens=10, output_tokens=2)),
        ]
    )
    first = fake.complete(_req())
    assert first.tool_calls[0].id == "call_1" and first.stop_reason == "tool_use"
    assert fake.complete(_req()).text == "done"


def test_fake_raises_when_script_exhausted() -> None:
    fake = FakeModel([ModelResponse(text="only")])
    fake.complete(_req())
    with pytest.raises(ScriptExhaustedError):
        fake.complete(_req())


def test_fake_supports_callable_and_records_requests() -> None:
    fake = FakeModel([lambda r: ModelResponse(text=r.messages[-1]["content"])])
    assert fake.complete(_req()).text == "hi"
    assert len(fake.requests) == 1
