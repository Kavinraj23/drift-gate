"""Typed errors raised by the LLM gateway. Callers catch these and escalate or abstain."""

from __future__ import annotations


class GatewayError(RuntimeError):
    """Base class for every gateway failure."""


class BudgetExceeded(GatewayError):
    """A spend cap would be exceeded; raised before any network call is made."""

    def __init__(self, scope: str, cap_usd: float, spent_usd: float, needed_usd: float) -> None:
        self.scope = scope
        self.cap_usd = cap_usd
        self.spent_usd = spent_usd
        self.needed_usd = needed_usd
        super().__init__(
            f"{scope} spend cap ${cap_usd:.2f} would be exceeded "
            f"(spent ${spent_usd:.4f}, this call needs up to ${needed_usd:.4f})"
        )


class InvestigationBudgetExhausted(GatewayError):
    """The per-investigation token budget is used up; the agent abstains and marks budget_truncated."""


class RetriesExhausted(GatewayError):
    """The API kept answering 429/529 past the retry limit."""


class FixtureMissing(GatewayError):
    """Replay mode found no recorded fixture for this request."""

    def __init__(self, key: str, path: str) -> None:
        self.key = key
        self.path = path
        super().__init__(f"no replay fixture for request key {key} (expected {path})")


class UnknownModel(GatewayError):
    """No price table entry for the model; fail closed rather than guess a cost."""


class MissingApiKey(GatewayError):
    """A live or record call was attempted without a key in the environment."""
