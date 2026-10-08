"""TEST DOUBLE: scripted fake model with the same call interface as the gateway. Never touches the network."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from driftgate.llm.types import ModelRequest, ModelResponse

Scripted = ModelResponse | Callable[[ModelRequest], ModelResponse]


class ScriptExhaustedError(RuntimeError):
    pass


class FakeModel:
    """Returns predefined responses in order (text and/or tool calls) and records every request."""

    def __init__(self, script: Sequence[Scripted]) -> None:
        self._script = list(script)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if len(self.requests) > len(self._script):
            raise ScriptExhaustedError(
                f"fake model script has {len(self._script)} responses; call {len(self.requests)} made"
            )
        item = self._script[len(self.requests) - 1]
        return item(request) if callable(item) else item
