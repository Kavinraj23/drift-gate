"""tasks.py behaviour: human-only refusal, not-implemented targets, mvp-check table."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("tasks", Path(__file__).resolve().parent.parent / "tasks.py")
tasks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tasks)


def test_not_implemented_target_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # e2e-live is still unimplemented; use a root without `.autonomous` so the not-implemented path (not the
    # human-only refusal) is what runs.
    monkeypatch.setattr(tasks, "ROOT", tmp_path)
    assert tasks.main(["tasks.py", "e2e-live"]) == 1
    assert "not implemented (M8b)" in capsys.readouterr().out


@pytest.mark.parametrize("target", ["record", "eval-live"])
def test_live_targets_dispatch_to_the_live_module_with_options(
    target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(tasks, "ROOT", tmp_path)
    monkeypatch.setattr(tasks, "_live", lambda t, extra: seen.append((t, extra)) or 0)
    assert tasks.main(["tasks.py", target, "--estimate"]) == 0
    assert seen == [(target, ["--estimate"])]


@pytest.mark.parametrize("target", sorted(tasks.HUMAN_ONLY))
def test_human_only_refuses_under_autonomous(
    target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".autonomous").write_text("")
    monkeypatch.setattr(tasks, "ROOT", tmp_path)
    assert tasks.main(["tasks.py", target]) == 1
    assert "human-only" in capsys.readouterr().out


def test_mvp_check_reports_pending_and_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tasks.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    # Every milestone is implemented now, so pin a partial set to keep exercising the pending path.
    monkeypatch.setattr(tasks, "IMPLEMENTED_MILESTONES", {"M0"})
    assert tasks.mvp_check() == 1
    out = capsys.readouterr().out
    assert "pass" in out and "pending" in out
