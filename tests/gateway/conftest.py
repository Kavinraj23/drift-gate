from __future__ import annotations

import random
from pathlib import Path

import pytest

from driftgate.llm.config import GatewayConfig
from driftgate.llm.types import ModelRequest

from .helpers import FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def config(tmp_path: Path) -> GatewayConfig:
    return GatewayConfig(ledger_path=tmp_path / "data" / "spend.sqlite", api_key="test-key-not-real")


@pytest.fixture
def rng() -> random.Random:
    return random.Random(0)


@pytest.fixture
def request_() -> ModelRequest:
    return ModelRequest(
        system="You are an on-call engineer.",
        messages=[{"role": "user", "content": "why did it fail?"}],
        tools=[
            {"name": "a", "description": "tool a", "input_schema": {"type": "object"}},
            {"name": "b", "description": "tool b", "input_schema": {"type": "object"}},
        ],
        max_tokens=256,
    )
