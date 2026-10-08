"""Test doubles for the gateway tests: fake clock and a stub SDK client."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class StubApiError(Exception):
    def __init__(self, status_code: int, retry_after: str | None = None) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code
        headers = {"retry-after": retry_after} if retry_after is not None else {}
        self.response = SimpleNamespace(headers=headers)


def sdk_response(
    text: str = "ok",
    inp: int = 100,
    out: int = 50,
    cache_read: int = 0,
    cache_write: int = 0,
    tool: tuple[str, str, dict[str, Any]] | None = None,
) -> SimpleNamespace:
    content: list[Any] = [SimpleNamespace(type="text", text=text)]
    if tool:
        content.append(SimpleNamespace(type="tool_use", id=tool[0], name=tool[1], input=tool[2]))
    return SimpleNamespace(
        content=content,
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=inp,
            output_tokens=out,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
    )


class StubClient:
    """Mimics `anthropic.Anthropic().messages.create`; items in `script` are responses or exceptions."""

    def __init__(self, script: list[Any] | Callable[[dict[str, Any]], Any] | None = None) -> None:
        self._script = script if script is not None else [sdk_response()]
        self.payloads: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **payload: Any) -> Any:
        self.payloads.append(payload)
        if callable(self._script):
            item = self._script(payload)
        else:
            item = self._script[min(len(self.payloads) - 1, len(self._script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item
