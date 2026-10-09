"""The human-run live tooling, exercised offline: estimate needs no key and no network; spending needs --yes-spend."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from driftgate.agents.fingerprint import prompt_fingerprint
from driftgate.eval import live
from driftgate.llm.budget import CallRecord
from driftgate.llm.config import DEFAULT_PRICES, GatewayConfig
from driftgate.llm.errors import BudgetExceeded, FixtureMissing, FixtureStale, GatewayError, RetriesExhausted
from driftgate.llm.replay import FixtureStore, save_manifest_entry, scenario_status
from driftgate.llm.types import ModelRequest, ModelResponse, Usage


def _models(root: Path) -> GatewayConfig:
    return GatewayConfig(ledger_path=root / "spend.sqlite")


def _run(argv: list[str], root: Path, data: Path, **kw) -> tuple[int, str]:  # type: ignore[no-untyped-def]
    out = io.StringIO()
    code = live.main(argv + ["--data", str(data)], root=root, out=out, models_loader=_models, **kw)
    return code, out.getvalue()


@pytest.mark.parametrize("target", ["record", "eval-live"])
def test_estimate_is_offline_and_prints_a_table(target: str, dataset_dir: Path, tmp_path: Path) -> None:
    def boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("estimate must not build a gateway or load credentials")

    code, text = _run([target, "--estimate"], tmp_path, dataset_dir, config_loader=boom, gateway_factory=boom)
    assert code == 0 and "ESTIMATE ONLY" in text and "with margin" in text and "ledger:" in text


def test_spending_requires_yes_spend(dataset_dir: Path, tmp_path: Path) -> None:
    def boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("must not reach the gateway without --yes-spend")

    code, text = _run(["record"], tmp_path, dataset_dir, config_loader=boom, gateway_factory=boom)
    assert code == 2 and "--yes-spend" in text


def test_eval_live_requires_yes_spend(dataset_dir: Path, tmp_path: Path) -> None:
    def boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("must not reach the gateway without --yes-spend")

    code, text = _run(["eval-live"], tmp_path, dataset_dir, config_loader=boom, gateway_factory=boom)
    assert code == 2 and "not spending" in text and "--yes-spend" in text


@pytest.mark.parametrize("argv", [["record"], ["record", "--yes-spend"], ["eval-live", "--yes-spend"]])
def test_spending_paths_refuse_under_autonomous(argv: list[str], dataset_dir: Path, tmp_path: Path) -> None:
    def boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("must not load credentials or build a gateway under .autonomous")

    (tmp_path / ".autonomous").write_text("")
    code, text = _run(argv, tmp_path, dataset_dir, config_loader=boom, gateway_factory=boom)
    assert code == 1 and "human-only" in text


# -- run loops against a stub gateway: no SDK, no network, no key ------------------------------------------------
KEY = "sk-ant-TESTSECRET-not-a-real-key"
REQ = ModelRequest(system="s", messages=[], max_tokens=10)


class _StubGateway:
    def __init__(self) -> None:
        self.calls: list[CallRecord] = []
        self.mode = ""


class _StubClient:
    def __init__(self, gw: _StubGateway, cost: float, exc: Exception | None) -> None:
        self._gw, self._cost, self._exc = gw, cost, exc

    def complete(self, request: ModelRequest) -> ModelResponse:
        if self._exc is not None:
            raise self._exc
        self._gw.calls.append(CallRecord("stub", 10, 5, 0, 0, self._cost, 0.0, 0.0, "live"))
        return ModelResponse(text="ok", usage=Usage())


def _stub(
    monkeypatch: pytest.MonkeyPatch, *, cost: float = 0.0, n_calls: int = 1, exc: Exception | None = None
) -> tuple[_StubGateway, list[str]]:
    """Replace the gateway-driven scenario run: each scenario makes `n_calls` calls through the (spied, guarded)
    client, then returns the real scripted result so the report and rows are genuine."""
    gw = _StubGateway()
    ran: list[str] = []

    def fake(data_dir, label, held_out, gateway, model_name, *, wrap_client):  # type: ignore[no-untyped-def]
        ran.append(label.scenario_id or "")
        client = wrap_client(_StubClient(gateway, cost, exc))
        try:
            for _ in range(n_calls):
                client.complete(REQ)
        except GatewayError:
            pass  # the real agents escalate on a gateway failure; the spy has already noted it
        return live.e2e.run_scenario(data_dir, label, held_out, mode="fake")

    monkeypatch.setattr(live.e2e, "run_with_gateway", fake)
    return gw, ran


def _spend(argv: list[str], gw: _StubGateway, tmp_path: Path, data: Path, key: str = KEY) -> tuple[int, str]:
    modes: list[str] = []

    def factory(_cfg, mode, _fx):  # type: ignore[no-untyped-def]
        modes.append(mode)
        return gw

    def loader(root: Path) -> GatewayConfig:
        return GatewayConfig(api_key=key, ledger_path=root / "spend.sqlite")

    code, text = _run(argv, tmp_path, data, config_loader=loader, gateway_factory=factory)
    assert modes and set(modes) <= {"record", "live"}
    return code, text


def _record_argv(tmp_path: Path, *extra: str) -> list[str]:
    return ["record", "--yes-spend", "--fixtures", str(tmp_path / "fx"), *extra]


def test_record_stops_mid_scenario_at_the_cap(
    dataset_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw, _ran = _stub(monkeypatch, cost=0.10, n_calls=5)
    code, text = _spend(_record_argv(tmp_path, "--cap-usd", "0.25"), gw, tmp_path, dataset_dir)
    assert code == 3 and "UNUSABLE" in text and "gateway failure" in text
    assert len(gw.calls) == 3  # the 4th call was refused before it was made: 0.30 >= 0.25
    assert not (tmp_path / "fx" / "manifest.json").exists()  # a partial scenario is not recorded as done


def test_record_stops_between_scenarios_at_the_cap(
    dataset_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw, ran = _stub(monkeypatch, cost=0.60, n_calls=1)
    code, text = _spend(_record_argv(tmp_path, "--all", "--cap-usd", "0.50"), gw, tmp_path, dataset_dir)
    assert code == 3 and "stopped before" in text and "cap $0.50" in text
    assert len(ran) == 1 and len(gw.calls) == 1


def test_record_stops_on_fatal_gateway_error(
    dataset_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw, ran = _stub(monkeypatch, exc=BudgetExceeded("daily", 1.0, 1.0, 0.1))
    code, text = _spend(_record_argv(tmp_path, "--all", "--cap-usd", "1.25"), gw, tmp_path, dataset_dir)
    assert code == 3 and "UNUSABLE" in text and "gateway failure" in text
    assert len(ran) == 1  # the run stops at the first unusable scenario


def test_record_scrubs_the_key_from_errors(dataset_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gw, _ = _stub(monkeypatch, exc=RuntimeError(f"401 invalid x-api-key {KEY}"))
    code, text = _spend(_record_argv(tmp_path), gw, tmp_path, dataset_dir)
    assert code == 3 and "[redacted]" in text and KEY not in text


def test_eval_live_skips_after_a_fatal_error_and_still_reports(
    dataset_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw, ran = _stub(monkeypatch, exc=RetriesExhausted("overloaded"))
    out_dir = tmp_path / "evalout"
    code, text = _spend(["eval-live", "--yes-spend", "--out", str(out_dir)], gw, tmp_path, dataset_dir)
    assert code == 3 and "UNUSABLE" in text and "an earlier gateway failure stopped the run" in text
    assert (out_dir / "report.json").exists() or any(out_dir.iterdir())  # a report is still written


def test_eval_live_stops_at_the_cap(dataset_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gw, ran = _stub(monkeypatch, cost=0.60, n_calls=1)
    argv = ["eval-live", "--yes-spend", "--cap-usd", "0.50", "--out", str(tmp_path / "evalout")]
    code, text = _spend(argv, gw, tmp_path, dataset_dir)
    assert code == 3 and "not run (" in text and "reached the cap" in text


def test_eval_live_scrubs_the_key_from_errors(
    dataset_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw, _ = _stub(monkeypatch, exc=RuntimeError(f"connection failed with {KEY}"))
    code, text = _spend(["eval-live", "--yes-spend", "--out", str(tmp_path / "evalout")], gw, tmp_path, dataset_dir)
    assert code == 3 and "[redacted]" in text and KEY not in text


def test_refuses_under_autonomous(dataset_dir: Path, tmp_path: Path) -> None:
    (tmp_path / ".autonomous").write_text("")
    code, text = _run(["eval-live", "--estimate"], tmp_path, dataset_dir)
    assert code == 1 and "human-only" in text


def test_every_default_model_has_a_sourced_price() -> None:
    for model, price in DEFAULT_PRICES.items():
        assert price.source_url.startswith("https://") and price.as_of, model
        if price.long_prompt:
            assert price.long_prompt.source_url and price.long_prompt_tokens


def test_haiku_55_long_prompt_tier_applies_above_threshold() -> None:
    from driftgate.llm.config import estimate_cost
    from driftgate.llm.types import Usage

    threshold = DEFAULT_PRICES["claude-haiku-5-5"].long_prompt_tokens
    assert threshold
    below, above = threshold - 1_000, threshold + 1_000
    short = estimate_cost(DEFAULT_PRICES, "claude-haiku-5-5", Usage(input_tokens=below, output_tokens=0))
    long_ = estimate_cost(DEFAULT_PRICES, "claude-haiku-5-5", Usage(input_tokens=above, output_tokens=0))
    assert short == pytest.approx(below * 0.10 / 1_000_000)  # base tier at or under the threshold
    assert long_ == pytest.approx(above * 0.50 / 1_000_000)  # the whole prompt is billed at the long-prompt tier
    assert long_ / above == pytest.approx(5 * short / below)


def test_stale_fixture_is_distinguished_from_missing(tmp_path: Path) -> None:
    from driftgate.llm.replay import request_key
    from driftgate.llm.types import ModelResponse, Usage

    req = ModelRequest(system="s", messages=[], max_tokens=10)
    resp = ModelResponse(text="ok", usage=Usage())
    key = request_key(req, "m")
    FixtureStore(tmp_path, fingerprint="old").save(key, req, "m", resp)
    with pytest.raises(FixtureStale):
        FixtureStore(tmp_path, fingerprint="new").load(key)
    with pytest.raises(FixtureMissing):
        FixtureStore(tmp_path, fingerprint="new").load("nope")
    save_manifest_entry(tmp_path, "sc-1", {"fingerprint": "old"})
    assert scenario_status(tmp_path, "sc-1", "new") == "stale"
    assert scenario_status(tmp_path, "sc-2", "new") == "missing"
    assert scenario_status(tmp_path, "sc-1", "old") == "fresh"
    assert prompt_fingerprint() == prompt_fingerprint()
