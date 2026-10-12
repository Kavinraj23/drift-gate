"""tasks.py: `approvals` takes arguments, nothing else offline does; the target is implemented."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("tasks_n4", Path(__file__).resolve().parents[2] / "tasks.py")
tasks = importlib.util.module_from_spec(SPEC)  # type: ignore[arg-type]
SPEC.loader.exec_module(tasks)  # type: ignore[union-attr]


def test_approvals_is_implemented_and_forwards_its_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(tasks, "approvals", lambda extra: seen.append(extra) or 0)
    assert "approvals" not in tasks.NOT_IMPLEMENTED and "approvals" not in tasks.HUMAN_ONLY
    assert tasks.main(["tasks.py", "approvals", "approve", "3", "--as", "alice"]) == 0
    assert seen == [["approve", "3", "--as", "alice"]]


@pytest.mark.parametrize("target", ["lint", "test", "data", "baseline", "e2e", "eval", "eval-tier12", "mvp-check"])
def test_other_offline_targets_still_reject_extra_arguments(target: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert tasks.main(["tasks.py", target, "--surprise"]) == 2
    assert "usage" in capsys.readouterr().out


def test_eval_tier12_is_a_handler() -> None:
    assert "eval-tier12" in tasks.HANDLERS


def test_approvals_command_line_runs_the_cli_module(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: object) -> object:
        calls.append(cmd)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(tasks.subprocess, "run", fake_run)
    assert tasks.approvals(["list"]) == 0
    assert calls[0][-1] == "list" and "driftgate.approvals_cli" in calls[0]
