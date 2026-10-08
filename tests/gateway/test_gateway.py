from __future__ import annotations

import random
from pathlib import Path

import pytest

from driftgate.llm.budget import InvestigationBudget
from driftgate.llm.config import DEFAULT_PRICES, GatewayConfig, InvestigationLimits, estimate_cost
from driftgate.llm.errors import (
    BudgetExceeded,
    InvestigationBudgetExhausted,
    MissingApiKey,
    RetriesExhausted,
    UnknownModel,
)
from driftgate.llm.gateway import Gateway
from driftgate.llm.ledger import SpendLedger, utc_day
from driftgate.llm.types import ModelRequest, Usage

from .helpers import FakeClock, StubApiError, StubClient, sdk_response

HAIKU = "claude-haiku-4-5-20251001"


def make(config: GatewayConfig, clock: FakeClock, client: StubClient, rng: random.Random) -> Gateway:
    return Gateway(config, client=client, clock=clock, rng=rng)


def test_response_is_translated_and_spend_recorded(config, clock, rng, request_) -> None:
    client = StubClient([sdk_response("hi", inp=1000, out=200, tool=("t1", "a", {"x": 1}))])
    gw = make(config, clock, client, rng)
    resp = gw.complete(request_)
    assert resp.text == "hi"
    assert resp.tool_calls[0].id == "t1" and resp.tool_calls[0].input == {"x": 1}
    assert resp.usage.input_tokens == 1000
    expected = (1000 * 1.0 + 200 * 5.0) / 1e6
    assert SpendLedger(config.ledger_path).lifetime_spend() == pytest.approx(expected)
    assert gw.calls[0].cost_usd == pytest.approx(expected)


def test_cost_computation_with_cache_multipliers() -> None:
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
    )
    assert estimate_cost(DEFAULT_PRICES, HAIKU, usage) == pytest.approx(1 + 5 + 0.1 + 1.25)
    assert "claude-sonnet-5-5" in DEFAULT_PRICES


def test_unknown_model_fails_closed(config, clock, rng, request_) -> None:
    gw = make(config, clock, StubClient(), rng)
    request_.model = "mystery-model"
    with pytest.raises(UnknownModel):
        gw.complete(request_)


def test_cache_control_on_system_and_tools(config, clock, rng, request_) -> None:
    client = StubClient()
    make(config, clock, client, rng).complete(request_)
    payload = client.payloads[0]
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in request_.tools[-1]  # caller's request is not mutated
    assert payload["model"] == HAIKU


def test_roles_use_configured_models(tmp_path, clock, rng, request_) -> None:
    cfg = GatewayConfig(ledger_path=tmp_path / "s.sqlite", reviewer_model="claude-sonnet-5-5")
    client = StubClient()
    gw = make(cfg, clock, client, rng)
    gw.bind(role="reviewer").complete(request_)
    gw.bind(role="investigator").complete(request_)
    assert [p["model"] for p in client.payloads] == ["claude-sonnet-5-5", HAIKU]


def test_daily_cap_fails_fast_without_network(tmp_path, clock, rng, request_) -> None:
    cfg = GatewayConfig(ledger_path=tmp_path / "s.sqlite", daily_cap_usd=0.01)
    SpendLedger(cfg.ledger_path).record(clock.time(), HAIKU, 0, 0, 0, 0, 0.0095)
    client = StubClient()
    gw = make(cfg, clock, client, rng)
    with pytest.raises(BudgetExceeded) as err:
        gw.complete(request_)
    assert err.value.scope == "daily"
    assert client.payloads == []


def test_daily_cap_resets_next_day_but_lifetime_does_not(tmp_path, clock, rng, request_) -> None:
    cfg = GatewayConfig(ledger_path=tmp_path / "s.sqlite", daily_cap_usd=0.01, lifetime_cap_usd=0.015)
    ledger = SpendLedger(cfg.ledger_path)
    ledger.record(clock.time(), HAIKU, 0, 0, 0, 0, 0.0099)
    day_one = utc_day(clock.time())
    clock.now += 86_400
    client = StubClient()
    gw = make(cfg, clock, client, rng)
    gw.complete(request_)  # new day: daily window is empty again
    assert utc_day(clock.time()) != day_one
    ledger.record(clock.time(), HAIKU, 0, 0, 0, 0, 0.0075)  # lifetime now ~0.0174 + prior call
    clock.now += 86_400
    with pytest.raises(BudgetExceeded) as err:
        gw.complete(request_)
    assert err.value.scope == "lifetime"


def test_caps_survive_restart(tmp_path, clock, rng, request_) -> None:
    cfg = GatewayConfig(ledger_path=tmp_path / "data" / "s.sqlite", lifetime_cap_usd=0.0035)
    first = make(cfg, clock, StubClient([sdk_response(inp=1000, out=250)]), rng)  # costs 0.00225
    first.complete(request_)
    del first
    client = StubClient()
    second = make(cfg, clock, client, rng)  # fresh process: new Gateway, same sqlite file
    with pytest.raises(BudgetExceeded):
        second.complete(request_)
    assert client.payloads == []


def test_worst_case_reservation_blocks_call_that_could_overshoot(tmp_path, clock, rng, request_) -> None:
    cfg = GatewayConfig(ledger_path=tmp_path / "s.sqlite", daily_cap_usd=0.001)  # 256 * $5/M alone is 0.00128
    client = StubClient()
    with pytest.raises(BudgetExceeded):
        make(cfg, clock, client, rng).complete(request_)
    assert client.payloads == []


def test_retry_honors_retry_after_and_backs_off(config, clock, rng, request_) -> None:
    client = StubClient([StubApiError(429, "30"), StubApiError(529), sdk_response()])
    gw = make(config, clock, client, rng)
    gw.complete(request_)
    assert len(client.payloads) == 3
    first, second = clock.sleeps[-2], clock.sleeps[-1]
    assert 30 <= first <= 30.25  # retry-after beats the 1s backoff; jitter is a fraction of backoff
    assert 2 <= second <= 2.5  # exponential: base * 2**1
    assert gw.calls[0].limiter_wait_s >= first + second


def test_retries_exhausted_and_nonretryable_errors(config, clock, rng, request_) -> None:
    always = StubClient([StubApiError(429)])
    with pytest.raises(RetriesExhausted):
        make(config, clock, always, rng).complete(request_)
    assert len(always.payloads) == config.max_retries + 1
    bad = StubClient([StubApiError(400)])
    with pytest.raises(StubApiError):
        make(config, clock, bad, rng).complete(request_)
    assert len(bad.payloads) == 1
    assert SpendLedger(config.ledger_path).lifetime_spend() == 0  # failed calls cost nothing


def test_limiter_waits_are_logged(tmp_path, clock, rng, request_) -> None:
    cfg = GatewayConfig(ledger_path=tmp_path / "s.sqlite", requests_per_minute=1)
    gw = make(cfg, clock, StubClient(), rng)
    gw.complete(request_)
    gw.complete(request_)
    assert gw.calls[0].limiter_wait_s == 0
    assert gw.calls[1].limiter_wait_s == pytest.approx(60.0)


def test_investigation_budget_rollup_and_exhaustion(config, clock, rng, request_) -> None:
    client = StubClient([sdk_response(inp=900, out=100, cache_read=500)])
    gw = make(config, clock, client, rng)
    budget = InvestigationBudget(InvestigationLimits(max_total_tokens=2000))
    bound = gw.bind(budget)
    bound.complete(request_)
    bound.complete(request_)
    assert budget.tokens_used == 3000
    assert budget.tokens_exhausted
    with pytest.raises(InvestigationBudgetExhausted):
        bound.complete(request_)
    assert len(client.payloads) == 2
    run = budget.run_fields()
    assert run["input_tokens"] == 2 * 1400 and run["output_tokens"] == 200
    assert run["cost_usd"] > 0
    assert set(run) == {"tool_calls", "input_tokens", "output_tokens", "cost_usd", "latency_s"}


def test_tool_call_and_reinvestigation_limits() -> None:
    budget = InvestigationBudget()
    assert all(budget.consume_tool_call() for _ in range(8))
    assert budget.consume_tool_call() is False and budget.exhausted
    assert budget.run_fields()["tool_calls"] == 8
    assert budget.begin_reinvestigation() is True
    assert budget.begin_reinvestigation() is False


def test_missing_api_key_is_typed(tmp_path, clock, rng, request_) -> None:
    cfg = GatewayConfig(ledger_path=tmp_path / "s.sqlite")  # no key, no injected client
    with pytest.raises(MissingApiKey):
        Gateway(cfg, clock=clock, rng=rng).complete(request_)


def test_lazy_client_factory_is_used_once(config, clock, rng, request_) -> None:
    built: list[str | None] = []
    client = StubClient()

    def factory(key: str | None) -> StubClient:
        built.append(key)
        return client

    gw = Gateway(config, client_factory=factory, clock=clock, rng=rng)
    assert built == []  # lazy
    gw.complete(request_)
    gw.complete(request_)
    assert built == ["test-key-not-real"]


def test_request_unchanged_by_calls(config, clock, rng, request_: ModelRequest) -> None:
    before = repr(request_)
    make(config, clock, StubClient(), rng).complete(request_)
    assert repr(request_) == before


def test_ledger_path_parent_created(tmp_path: Path) -> None:
    SpendLedger(tmp_path / "deep" / "dir" / "s.sqlite")
    assert (tmp_path / "deep" / "dir").is_dir()
