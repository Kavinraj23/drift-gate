from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from driftgate.llm.config import GatewayConfig
from driftgate.llm.errors import BudgetExceeded, FixtureMissing
from driftgate.llm.gateway import Gateway
from driftgate.llm.replay import FixtureStore, request_key, response_from_dict
from driftgate.llm.types import ModelRequest

from .helpers import StubClient, sdk_response

MODEL = "claude-haiku-4-5-20251001"
SRC = str(Path(__file__).resolve().parents[2] / "src")


def _run_isolated(code: str) -> subprocess.CompletedProcess[str]:
    """Fresh interpreter (so sys.modules is clean) with only this worktree's src on the path."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["PYTHONPATH"] = SRC
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, env=env)


def test_record_then_replay_round_trip(config, clock, rng, request_, tmp_path: Path) -> None:
    store = FixtureStore(tmp_path / "fixtures")
    stub = StubClient([sdk_response("answer", inp=321, out=45, tool=("tu_1", "a", {"k": "v"}))])
    recorder = Gateway(config, mode="record", fixtures=store, client=stub, clock=clock, rng=rng)
    live = recorder.complete(request_)
    path = store.path_for(request_key(request_, MODEL))
    assert path.is_file()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["key"] == path.stem and doc["request"]["model"] == MODEL

    replayer = Gateway(config, mode="replay", fixtures=store)  # no client, no ledger, no clock
    replayed = replayer.complete(request_)
    assert replayed == live
    assert replayer.calls[0].mode == "replay" and replayer.calls[0].input_tokens == 321
    assert replayer.calls[0].cost_usd > 0


def test_replay_missing_fixture_is_typed_error(config, request_, tmp_path: Path) -> None:
    gw = Gateway(config, mode="replay", fixtures=FixtureStore(tmp_path / "none"))
    with pytest.raises(FixtureMissing) as err:
        gw.complete(request_)
    assert err.value.key == request_key(request_, MODEL)


def test_key_is_stable_and_sensitive(request_) -> None:
    same = ModelRequest(
        system=request_.system, messages=list(request_.messages), tools=list(request_.tools), max_tokens=256
    )
    assert request_key(request_, MODEL) == request_key(same, MODEL)
    assert len(request_key(request_, MODEL)) == 64
    same.messages = [{"role": "user", "content": "different"}]
    assert request_key(request_, MODEL) != request_key(same, MODEL)
    assert request_key(request_, MODEL) != request_key(request_, "claude-sonnet-5-5")


def test_replay_resolves_default_model_from_role(config, request_, tmp_path: Path) -> None:
    store = FixtureStore(tmp_path / "f")
    gw = Gateway(config, mode="replay", fixtures=store)
    store.save(request_key(request_, MODEL), request_, MODEL, response_from_dict({"text": "x"}))
    assert gw.bind(role="reviewer").complete(request_).text == "x"


def test_replay_never_imports_sdk_or_touches_network() -> None:
    """Fresh interpreter: replay a fixture with no key and verify `anthropic` is never imported."""
    code = "\n".join(
        [
            "import sys, tempfile, pathlib, socket",
            "from driftgate.llm.config import GatewayConfig",
            "from driftgate.llm.gateway import Gateway",
            "from driftgate.llm.replay import FixtureStore, request_key, response_from_dict",
            "from driftgate.llm.types import ModelRequest",
            "req = ModelRequest(system='s', messages=[{'role': 'user', 'content': 'q'}])",
            "model = 'claude-haiku-4-5-20251001'",
            "store = FixtureStore(pathlib.Path(tempfile.mkdtemp()))",
            "store.save(request_key(req, model), req, model, response_from_dict({'text': 'ok'}))",
            "def refuse(*a, **k):",
            "    raise RuntimeError('network')",
            "socket.socket.connect = refuse",
            "out = Gateway(GatewayConfig(), mode='replay', fixtures=store).complete(req)",
            "assert out.text == 'ok'",
            "assert 'anthropic' not in sys.modules, 'SDK imported during replay'",
            "print('clean')",
        ]
    )
    done = _run_isolated(code)
    assert done.stdout.strip() == "clean", done.stderr


def test_gateway_module_does_not_import_sdk_at_import_time() -> None:
    code = "import sys; import driftgate.llm.gateway; assert 'anthropic' not in sys.modules; print('clean')"
    done = _run_isolated(code)
    assert done.stdout.strip() == "clean", done.stderr


def test_modes_requiring_fixtures_validate(config) -> None:
    with pytest.raises(ValueError):
        Gateway(config, mode="replay")


def test_record_mode_still_enforces_caps(tmp_path, clock, rng, request_) -> None:
    cfg = GatewayConfig(ledger_path=tmp_path / "s.sqlite", daily_cap_usd=0.0)
    stub = StubClient()
    gw = Gateway(cfg, mode="record", fixtures=FixtureStore(tmp_path / "f"), client=stub, clock=clock, rng=rng)
    with pytest.raises(BudgetExceeded):
        gw.complete(request_)
    assert stub.payloads == []
