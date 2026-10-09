"""The only module that imports the Anthropic SDK: rate limits, budgets, spend caps, retries, caching, record/replay.

The SDK is imported lazily and only when a real client must be constructed (live/record mode with no
injected client). Replay mode never reaches that code. Everything else lives in sibling modules.
"""

from __future__ import annotations

import copy
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from driftgate.llm.budget import CallRecord, InvestigationBudget
from driftgate.llm.clock import Clock, SystemClock
from driftgate.llm.config import GatewayConfig, estimate_cost
from driftgate.llm.errors import (
    BudgetExceeded,
    InvestigationBudgetExhausted,
    MissingApiKey,
    RetriesExhausted,
)
from driftgate.llm.ledger import SpendLedger, utc_day
from driftgate.llm.limits import ConcurrencyLimiter, RateLimiter
from driftgate.llm.replay import FixtureStore, request_key
from driftgate.llm.types import ModelRequest, ModelResponse, ToolCall, Usage

Mode = Literal["live", "record", "replay"]
RETRYABLE_STATUS = frozenset({429, 529})
CACHE_CONTROL = {"type": "ephemeral"}


def _estimate_input_tokens(request: ModelRequest) -> int:
    chars = len(request.system) + len(str(request.tools)) + len(str(request.messages))
    return chars // 4 + 1


estimate_input_tokens = _estimate_input_tokens  # public: the live-run cost estimator uses the same rough count


def build_payload(request: ModelRequest, model: str) -> dict[str, Any]:
    """SDK `messages.create` kwargs with cache_control on the static system prompt and tool definitions."""
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": request.max_tokens,
        "messages": copy.deepcopy(request.messages),
    }
    if request.system:
        payload["system"] = [{"type": "text", "text": request.system, "cache_control": dict(CACHE_CONTROL)}]
    if request.tools:
        tools = copy.deepcopy(request.tools)
        tools[-1]["cache_control"] = dict(CACHE_CONTROL)  # one breakpoint caches the whole tool prefix
        payload["tools"] = tools
    return payload


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def parse_sdk_response(raw: Any) -> ModelResponse:
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    for block in _get(raw, "content", []) or []:
        kind = _get(block, "type")
        if kind == "text":
            text_parts.append(_get(block, "text", ""))
        elif kind == "tool_use":
            calls.append(ToolCall(id=_get(block, "id"), name=_get(block, "name"), input=dict(_get(block, "input", {}))))
    u = _get(raw, "usage")
    usage = Usage(
        input_tokens=int(_get(u, "input_tokens", 0) or 0),
        output_tokens=int(_get(u, "output_tokens", 0) or 0),
        cache_read_tokens=int(_get(u, "cache_read_input_tokens", 0) or 0),
        cache_creation_tokens=int(_get(u, "cache_creation_input_tokens", 0) or 0),
    )
    return ModelResponse(
        text="".join(text_parts), tool_calls=calls, usage=usage, stop_reason=_get(raw, "stop_reason", "end_turn")
    )


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
    if not headers:
        return None
    value = headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _make_sdk_client(api_key: str | None) -> Any:
    if not api_key:
        raise MissingApiKey("no API key configured; live and record modes need one (replay does not)")
    import anthropic  # the only SDK import in the codebase

    return anthropic.Anthropic(api_key=api_key, max_retries=0)  # retries are ours, so the limiter sees them


@dataclass
class _Reservation:
    amount: float


class Gateway:
    """Implements ModelClient. Use `bind()` to attach a per-investigation budget and a model role."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        mode: Mode = "live",
        fixtures: FixtureStore | None = None,
        client: Any | None = None,
        client_factory: Callable[[str | None], Any] = _make_sdk_client,
        ledger: SpendLedger | None = None,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> None:
        if mode in ("record", "replay") and fixtures is None:
            raise ValueError(f"{mode} mode needs a FixtureStore")
        self._config = config
        self._mode: Mode = mode
        self._fixtures = fixtures
        self._client = client
        self._client_factory = client_factory
        self._clock = clock or SystemClock()
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._reserved = 0.0
        self.calls: list[CallRecord] = []
        self._ledger: SpendLedger | None = None
        self._limiter: RateLimiter | None = None
        self._concurrency: ConcurrencyLimiter | None = None
        if mode != "replay":
            self._ledger = ledger or SpendLedger(config.ledger_path)
            self._limiter = RateLimiter(config.requests_per_minute, config.tokens_per_minute, self._clock)
            self._concurrency = ConcurrencyLimiter(config.max_concurrency)

    # -- public ------------------------------------------------------------------------------
    def complete(self, request: ModelRequest) -> ModelResponse:
        return self._complete(request, role="investigator", budget=None)

    def bind(self, budget: InvestigationBudget | None = None, role: str = "investigator") -> BoundClient:
        return BoundClient(self, budget, role)

    # -- internals ---------------------------------------------------------------------------
    def _complete(self, request: ModelRequest, role: str, budget: InvestigationBudget | None) -> ModelResponse:
        if budget is not None and budget.tokens_exhausted:
            raise InvestigationBudgetExhausted(
                f"token budget {budget.limits.max_total_tokens} used ({budget.tokens_used})"
            )
        model = request.model or self._config.model_for(role)
        if self._mode == "replay":
            return self._replay(request, model, budget)
        return self._call_api(request, model, budget)

    def _replay(self, request: ModelRequest, model: str, budget: InvestigationBudget | None) -> ModelResponse:
        assert self._fixtures is not None
        response = self._fixtures.load(request_key(request, model))
        self._log(model, response.usage, 0.0, 0.0, budget)
        return response

    def _call_api(self, request: ModelRequest, model: str, budget: InvestigationBudget | None) -> ModelResponse:
        assert self._limiter is not None and self._ledger is not None and self._concurrency is not None
        reservation = self._reserve(request, model)  # fails fast, before any limiter wait or network call
        try:
            wait = self._limiter.acquire(_estimate_input_tokens(request) + request.max_tokens)
            payload = build_payload(request, model)
            started = self._clock.monotonic()
            with self._concurrency.slot():
                raw, retry_wait = self._send_with_retries(payload)
            latency = self._clock.monotonic() - started
            response = parse_sdk_response(raw)
            u = response.usage
            cost = estimate_cost(self._config.prices, model, response.usage)
            self._ledger.record(
                self._clock.time(),
                model,
                u.input_tokens,
                u.output_tokens,
                u.cache_read_tokens,
                u.cache_creation_tokens,
                cost,
            )
        finally:
            self._release(reservation)
        self._limiter.refund_tokens(max(0, request.max_tokens - u.output_tokens))
        self._log(model, u, latency, wait + retry_wait, budget, cost=cost)
        if self._mode == "record":
            assert self._fixtures is not None
            self._fixtures.save(request_key(request, model), request, model, response)
        return response

    def _reserve(self, request: ModelRequest, model: str) -> _Reservation:
        """Check both caps against spent + in-flight reservations + worst-case cost of this call."""
        assert self._ledger is not None
        cfg = self._config
        worst = estimate_cost(
            cfg.prices, model, Usage(input_tokens=_estimate_input_tokens(request), output_tokens=request.max_tokens)
        )
        with self._lock:
            lifetime = self._ledger.lifetime_spend() + self._reserved
            if lifetime + worst > cfg.lifetime_cap_usd:
                raise BudgetExceeded("lifetime", cfg.lifetime_cap_usd, lifetime, worst)
            daily = self._ledger.daily_spend(utc_day(self._clock.time())) + self._reserved
            if daily + worst > cfg.daily_cap_usd:
                raise BudgetExceeded("daily", cfg.daily_cap_usd, daily, worst)
            self._reserved += worst
        return _Reservation(worst)

    def _release(self, reservation: _Reservation) -> None:
        with self._lock:
            self._reserved -= reservation.amount

    def _send_with_retries(self, payload: dict[str, Any]) -> tuple[Any, float]:
        cfg = self._config
        if self._client is None:
            self._client = self._client_factory(cfg.api_key)
        waited = 0.0
        for attempt in range(cfg.max_retries + 1):
            try:
                return self._client.messages.create(**payload), waited
            except Exception as exc:  # duck-typed: the SDK's status errors carry status_code
                status = getattr(exc, "status_code", None)
                if status not in RETRYABLE_STATUS:
                    raise
                if attempt == cfg.max_retries:
                    raise RetriesExhausted(f"status {status} persisted after {cfg.max_retries} retries") from exc
                backoff = min(cfg.backoff_max_s, cfg.backoff_base_s * (2**attempt))
                delay = max(backoff, _retry_after_seconds(exc) or 0.0)
                delay += self._rng.uniform(0, 0.25 * backoff)
                self._clock.sleep(delay)
                waited += delay
        raise AssertionError("unreachable")  # pragma: no cover

    def _log(
        self,
        model: str,
        usage: Usage,
        latency: float,
        wait: float,
        budget: InvestigationBudget | None,
        cost: float | None = None,
    ) -> None:
        if cost is None:  # replay: estimated from the recorded usage so eval can report cost
            cost = estimate_cost(self._config.prices, model, usage)
        record = CallRecord(
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            cost_usd=cost,
            latency_s=latency,
            limiter_wait_s=wait,
            mode=self._mode,
        )
        with self._lock:
            self.calls.append(record)
        if budget is not None:
            budget.add_call(record)


class BoundClient:
    """A ModelClient view of the gateway for one investigation/role. Drop-in for FakeModel."""

    def __init__(self, gateway: Gateway, budget: InvestigationBudget | None, role: str) -> None:
        self._gateway = gateway
        self.budget = budget
        self._role = role

    def complete(self, request: ModelRequest) -> ModelResponse:
        return self._gateway._complete(request, self._role, self.budget)
