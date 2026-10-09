"""The human-run live tooling, exercised offline: estimate needs no key and no network; spending needs --yes-spend."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from driftgate.agents.fingerprint import prompt_fingerprint
from driftgate.eval import live
from driftgate.llm.config import DEFAULT_PRICES, GatewayConfig
from driftgate.llm.errors import FixtureMissing, FixtureStale
from driftgate.llm.replay import FixtureStore, save_manifest_entry, scenario_status
from driftgate.llm.types import ModelRequest


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

    short = estimate_cost(DEFAULT_PRICES, "claude-haiku-5-5", Usage(input_tokens=1_000_000, output_tokens=0))
    long_ = estimate_cost(DEFAULT_PRICES, "claude-haiku-5-5", Usage(input_tokens=1_000_000, output_tokens=0))
    assert short == pytest.approx(0.50) and long_ == pytest.approx(0.50)  # a 1M prompt is over the threshold
    small = estimate_cost(DEFAULT_PRICES, "claude-haiku-5-5", Usage(input_tokens=50_000, output_tokens=0))
    assert small == pytest.approx(0.005)


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
