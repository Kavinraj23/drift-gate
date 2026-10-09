"""e2e-live is human-only: plan mode needs no network or key, and nothing runs without --yes-live."""

from __future__ import annotations

import io
from pathlib import Path

from driftgate.eval import live
from driftgate.eval.live import E2E_SCENARIOS


def _run(argv: list[str], root: Path, **kw: object) -> tuple[int, str]:
    out = io.StringIO()
    code = live.main(["e2e-live", *argv], root=root, out=out, **kw)  # type: ignore[arg-type]
    return code, out.getvalue()


def test_plan_lists_all_six_scenarios_and_calls_nothing(tmp_path: Path) -> None:
    def boom(*_a: object) -> object:
        raise AssertionError("plan mode must not build a gateway or config")

    code, text = _run([], tmp_path, config_loader=boom, gateway_factory=boom)  # type: ignore[arg-type]
    assert code == 2 and "not running: pass --yes-live" in text
    assert all(name in text for name in E2E_SCENARIOS) and len(E2E_SCENARIOS) == 6


def test_flaky_is_expected_to_escalate_under_invariant_3() -> None:
    assert E2E_SCENARIOS["flaky"][1] == "escalated"
    assert E2E_SCENARIOS["approval-rejected"][1] == "closed"


def test_refuses_when_autonomous_marker_exists(tmp_path: Path) -> None:
    (tmp_path / ".autonomous").write_text("", encoding="utf-8")
    code, text = _run(["--yes-live"], tmp_path)
    assert code == 1 and "human-only" in text


def test_missing_github_config_stops_before_any_call(tmp_path: Path, monkeypatch: object) -> None:
    def no_config() -> object:
        raise ValueError("set DRIFTGATE_PLAYGROUND_REPO and PLAYGROUND_PAT")

    out = io.StringIO()
    code = live.run_e2e_live(["--yes-live"], root=tmp_path, out=out, connect=no_config)  # type: ignore[arg-type]
    assert code == 2 and "cannot start" in out.getvalue()
