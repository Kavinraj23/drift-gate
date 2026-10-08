"""Gateway configuration: price table, caps, limits, model names. Env loading is explicit and never run by tests."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from driftgate.llm.errors import UnknownModel
from driftgate.llm.types import Usage

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens, with prompt-cache multipliers applied to the input price."""

    input_per_mtok: float
    output_per_mtok: float
    cache_read_multiplier: float = 0.1
    cache_write_multiplier: float = 1.25


# NOTE: the sonnet entry is a configured estimate; confirm against the current price page (see docs/design/M2.md).
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "claude-haiku-4-5-20251001": ModelPrice(1.0, 5.0),
    "claude-sonnet-5-5": ModelPrice(3.0, 15.0),
}


def estimate_cost(prices: Mapping[str, ModelPrice], model: str, usage: Usage) -> float:
    price = prices.get(model)
    if price is None:
        raise UnknownModel(f"no price configured for model {model!r}")
    # input_tokens excludes cached tokens in the API's usage; cache tokens are billed separately.
    inp = price.input_per_mtok
    total = (
        usage.input_tokens * inp
        + usage.cache_read_tokens * inp * price.cache_read_multiplier
        + usage.cache_creation_tokens * inp * price.cache_write_multiplier
        + usage.output_tokens * price.output_per_mtok
    )
    return total / 1_000_000


@dataclass(frozen=True)
class GatewayConfig:
    investigator_model: str = DEFAULT_MODEL
    reviewer_model: str = DEFAULT_MODEL
    prices: Mapping[str, ModelPrice] = field(default_factory=lambda: dict(DEFAULT_PRICES))
    lifetime_cap_usd: float = 2.50
    daily_cap_usd: float = 1.00
    requests_per_minute: int = 40
    tokens_per_minute: int = 40_000
    max_concurrency: int = 2
    max_retries: int = 5
    backoff_base_s: float = 1.0
    backoff_max_s: float = 60.0
    ledger_path: Path = Path("data") / "spend.sqlite"
    api_key: str | None = field(default=None, repr=False)

    def model_for(self, role: str) -> str:
        return self.reviewer_model if role == "reviewer" else self.investigator_model


@dataclass(frozen=True)
class InvestigationLimits:
    max_tool_calls: int = 8
    max_total_tokens: int = 40_000
    max_reinvestigations: int = 1


def config_from_env(env: Mapping[str, str]) -> GatewayConfig:
    """Pure: build a config from an env mapping. Never touches os.environ or files."""
    return GatewayConfig(
        investigator_model=env.get("DRIFTGATE_INVESTIGATOR_MODEL") or DEFAULT_MODEL,
        reviewer_model=env.get("DRIFTGATE_REVIEWER_MODEL") or DEFAULT_MODEL,
        api_key=env.get("ANTHROPIC_API_KEY") or None,
    )


def load_config(dotenv_path: Path | None = None) -> GatewayConfig:
    """Explicit loader for human-run targets: reads .env via python-dotenv, then the process env.

    Tests never call this. The key is stored on the config with repr disabled and is never logged.
    """
    from dotenv import load_dotenv

    load_dotenv(dotenv_path)
    return config_from_env(os.environ)
