"""Per-investigation budget and rollup of per-call logs into the Report `run` fields."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from driftgate.llm.config import InvestigationLimits


@dataclass(frozen=True)
class CallRecord:
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    latency_s: float
    limiter_wait_s: float
    mode: str  # "live" | "record" | "replay"

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_creation_tokens


@dataclass
class InvestigationBudget:
    """Tracks one investigation. Exhaustion is a return value / typed error the agent turns into abstain."""

    limits: InvestigationLimits = field(default_factory=InvestigationLimits)
    tool_calls: int = 0
    reinvestigations: int = 0
    calls: list[CallRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def tokens_used(self) -> int:
        return sum(c.total_tokens for c in self.calls)

    @property
    def tokens_exhausted(self) -> bool:
        return self.tokens_used >= self.limits.max_total_tokens

    @property
    def tool_calls_exhausted(self) -> bool:
        return self.tool_calls >= self.limits.max_tool_calls

    @property
    def exhausted(self) -> bool:
        return self.tokens_exhausted or self.tool_calls_exhausted

    def consume_tool_call(self) -> bool:
        """Count one tool execution. False means the budget is spent (do not run it; abstain)."""
        with self._lock:
            if self.tool_calls >= self.limits.max_tool_calls:
                return False
            self.tool_calls += 1
            return True

    def begin_reinvestigation(self) -> bool:
        """Start the (single) re-investigation round. False means none left (escalate)."""
        with self._lock:
            if self.reinvestigations >= self.limits.max_reinvestigations:
                return False
            self.reinvestigations += 1
            return True

    def add_call(self, record: CallRecord) -> None:
        with self._lock:
            self.calls.append(record)

    def run_fields(self) -> dict[str, Any]:
        """The Report `run` object (contracts/report.schema.json)."""
        return {
            "tool_calls": self.tool_calls,
            "input_tokens": sum(c.input_tokens + c.cache_read_tokens + c.cache_creation_tokens for c in self.calls),
            "output_tokens": sum(c.output_tokens for c in self.calls),
            "cost_usd": sum(c.cost_usd for c in self.calls),
            "latency_s": sum(c.latency_s for c in self.calls),
        }
