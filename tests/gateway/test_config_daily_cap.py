"""The optional DRIFTGATE_DAILY_CAP_USD override: pacing only, never able to raise total spend."""

from __future__ import annotations

import pytest

from driftgate.llm.config import GatewayConfig, config_from_env


def test_default_daily_cap_is_unchanged_without_the_setting() -> None:
    assert config_from_env({}).daily_cap_usd == GatewayConfig().daily_cap_usd == 1.00
    assert config_from_env({"DRIFTGATE_DAILY_CAP_USD": "  "}).daily_cap_usd == 1.00


def test_the_override_can_raise_or_lower_the_daily_cap() -> None:
    assert config_from_env({"DRIFTGATE_DAILY_CAP_USD": "1.75"}).daily_cap_usd == 1.75
    assert config_from_env({"DRIFTGATE_DAILY_CAP_USD": "0.25"}).daily_cap_usd == 0.25


def test_the_override_is_clamped_to_the_lifetime_cap_and_leaves_it_alone() -> None:
    cfg = config_from_env({"DRIFTGATE_DAILY_CAP_USD": "100"})
    assert cfg.daily_cap_usd == cfg.lifetime_cap_usd == 2.50


@pytest.mark.parametrize("bad", ["abc", "0", "-1", "nan", "1,5"])
def test_a_malformed_or_non_positive_override_fails_loudly(bad: str) -> None:
    with pytest.raises(ValueError, match="DRIFTGATE_DAILY_CAP_USD"):
        config_from_env({"DRIFTGATE_DAILY_CAP_USD": bad})
