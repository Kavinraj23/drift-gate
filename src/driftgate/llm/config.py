"""Gateway configuration: price table, caps, limits, model names. Env loading is explicit and never run by tests."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from driftgate.llm.errors import UnknownModel
from driftgate.llm.types import Usage

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
RATE_LIMITS_URL = "https://platform.claude.com/docs/en/api/rate-limits"
PRICES_AS_OF = "2026-10-08"  # the date the table below was checked against PRICING_URL
LONG_PROMPT_TOKENS = 100_000  # Haiku 5.5 charges its higher rates when the whole prompt exceeds this


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens, with prompt-cache multipliers applied to the input price.

    Every entry must carry `source_url` and `as_of` (a test enforces it). `long_prompt` is the higher price a model
    charges when the prompt (input + cache tokens) exceeds `long_prompt_tokens`; a model without one is flat-priced.
    """

    input_per_mtok: float
    output_per_mtok: float
    cache_read_multiplier: float = 0.1
    cache_write_multiplier: float = 1.25  # 5-minute cache write; the gateway uses 5-minute (ephemeral) caching
    source_url: str = ""
    as_of: str = ""
    long_prompt_tokens: int | None = None
    long_prompt: ModelPrice | None = None

    def for_prompt(self, prompt_tokens: int) -> ModelPrice:
        """The price tier that applies to a prompt of this size (fails closed: over the threshold means the dearer)."""
        if (
            self.long_prompt is not None
            and self.long_prompt_tokens is not None
            and prompt_tokens > self.long_prompt_tokens
        ):
            return self.long_prompt
        return self


def _price(inp: float, out: float, **kw: float) -> ModelPrice:
    return ModelPrice(inp, out, source_url=PRICING_URL, as_of=PRICES_AS_OF, **kw)


# Verified against PRICING_URL on 2026-10-08. Cache write = 1.25x input (5 minutes), cache read = 0.1x input, except
# Sonnet 5.5 whose cache read is 0.05x ($0.10/MTok) per that page. The Haiku 5.5 long-prompt tier is modelled
# explicitly. Claude 4.7+ tokenizers produce ~30% more tokens for the same text (the page says so), which is why
# estimates carry a margin. A model that is not in this table is never priced (UnknownModel).
_HAIKU_45 = _price(1.0, 5.0)
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "claude-haiku-4-5-20251001": _HAIKU_45,
    "claude-haiku-4-5": _HAIKU_45,  # alias, same model and price
    "claude-sonnet-5-5": _price(2.0, 10.0, cache_read_multiplier=0.05),
    "claude-haiku-5-5": ModelPrice(
        0.10,
        0.50,
        source_url=PRICING_URL,
        as_of=PRICES_AS_OF,
        long_prompt_tokens=LONG_PROMPT_TOKENS,
        long_prompt=_price(0.50, 2.50),
    ),
}


def estimate_cost(prices: Mapping[str, ModelPrice], model: str, usage: Usage) -> float:
    entry = prices.get(model)
    if entry is None:
        raise UnknownModel(f"no price configured for model {model!r}")
    # input_tokens excludes cached tokens in the API's usage; cache tokens are billed separately.
    prompt_tokens = usage.input_tokens + usage.cache_read_tokens + usage.cache_creation_tokens
    price = entry.for_prompt(prompt_tokens)
    inp = price.input_per_mtok
    total = (
        usage.input_tokens * inp
        + usage.cache_read_tokens * inp * price.cache_read_multiplier
        + usage.cache_creation_tokens * inp * price.cache_write_multiplier
        + usage.output_tokens * price.output_per_mtok
    )
    return total / 1_000_000


# Rate-limit defaults. The published STANDARD lowest tier ("Start") for every model above is 1,000 RPM, 2,000,000
# ITPM and 400,000 OTPM (RATE_LIMITS_URL, as of 2026-10-08). New organisations may sit in an "Evaluation" tier with
# lower, unpublished limits, so these defaults are deliberately a small fraction of the Start tier. CHECK YOUR REAL
# TIER in the Console (Settings > Limits) before a live run; the gateway also backs off on 429/529 and honours
# retry-after, so a lower real limit costs time, not correctness.
RATE_LIMIT_SOURCE = {
    "source_url": RATE_LIMITS_URL,
    "as_of": "2026-10-08",
    "tier": "Start (lowest standard tier)",
    "published_requests_per_minute": 1_000,
    "published_input_tokens_per_minute": 2_000_000,
    "published_output_tokens_per_minute": 400_000,
}
DEFAULT_REQUESTS_PER_MINUTE = 40  # 4% of the Start tier's RPM
DEFAULT_TOKENS_PER_MINUTE = 40_000  # 2% of the Start tier's ITPM; counts estimated input plus max_tokens per call


@dataclass(frozen=True)
class GatewayConfig:
    investigator_model: str = DEFAULT_MODEL
    reviewer_model: str = DEFAULT_MODEL
    prices: Mapping[str, ModelPrice] = field(default_factory=lambda: dict(DEFAULT_PRICES))
    lifetime_cap_usd: float = 2.50
    daily_cap_usd: float = 1.00
    requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE
    tokens_per_minute: int = DEFAULT_TOKENS_PER_MINUTE
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
