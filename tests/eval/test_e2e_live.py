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


def test_different_investigator_and_reviewer_models_are_never_sent_as_a_combined_name(
    tmp_path: Path, monkeypatch: object
) -> None:
    """The display label 'a + b' once reached the gateway as a request's model, which overrides the per-role model."""
    from types import SimpleNamespace

    from driftgate.llm.config import GatewayConfig

    seen: dict[str, object] = {}

    def spy_build(*_a: object, **kw: object) -> object:
        seen["orchestrator_model"] = kw.get("model")

        def handle(_eid: str) -> object:
            raise RuntimeError("stop after the wiring is captured")

        return SimpleNamespace(handle=handle)

    def spy_hook(_source: object, _factory: object, **kw: object) -> object:
        seen["reviewer_model"] = kw.get("model")
        return object()

    monkeypatch.setattr(live, "build_github_orchestrator", spy_build)  # type: ignore[attr-defined]
    monkeypatch.setattr(live, "ReviewerHook", spy_hook)  # type: ignore[attr-defined]
    monkeypatch.setattr(live, "install_verification", lambda _o: None)  # type: ignore[attr-defined]
    pg = SimpleNamespace(
        source=SimpleNamespace(list_executions=lambda *_a: [SimpleNamespace(execution_id="1.1")]),
        target=None,
        repo="o/r",
        scrub=lambda s: s,
    )
    config = GatewayConfig(investigator_model="claude-sonnet-5-5", reviewer_model="claude-haiku-4-5-20251001")
    out = io.StringIO()
    live.run_e2e_live(
        ["--yes-live", "--scenario", "lockfile"],
        root=tmp_path,
        out=out,
        config_loader=lambda _root: config,
        gateway_factory=lambda *_a: SimpleNamespace(calls=[], bind=lambda *_a, **_k: None),  # type: ignore[arg-type,return-value]
        connect=lambda: pg,  # type: ignore[arg-type,return-value]
    )
    assert seen == {"orchestrator_model": "", "reviewer_model": ""}  # the gateway picks each role's model
