"""tasks.py: `trace-report` is implemented and is the only new target that takes arguments; no rule loosened."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("tasks_n5", Path(__file__).resolve().parents[2] / "tasks.py")
tasks = importlib.util.module_from_spec(SPEC)  # type: ignore[arg-type]
SPEC.loader.exec_module(tasks)  # type: ignore[union-attr]


def test_trace_report_is_implemented_offline_and_not_human_only() -> None:
    assert "trace-report" not in tasks.NOT_IMPLEMENTED
    assert "trace-report" not in tasks.HUMAN_ONLY and "trace-report" not in tasks.LIVE_TARGETS


def test_argument_exceptions_are_exactly_approvals_and_trace_report() -> None:
    assert tasks.ARG_TARGETS == {"approvals", "trace-report"}
    assert tasks.HUMAN_ONLY == {"record", "eval-live", "e2e-live", "playground-reset", "holdout-fetch", "record-holdout"}
    assert tasks.FETCH_TARGETS == {"holdout-fetch"}
    assert tasks.LIVE_TARGETS == {"record", "eval-live", "e2e-live"}


def test_trace_report_forwards_its_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(tasks, "trace_report", lambda extra: seen.append(extra) or 0)
    assert tasks.main(["tasks.py", "trace-report"]) == 0
    assert tasks.main(["tasks.py", "trace-report", "--audit", "a.jsonl", "--out", "o"]) == 0
    assert seen == [[], ["--audit", "a.jsonl", "--out", "o"]]


def test_trace_report_runs_the_report_module_with_no_injected_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: object) -> object:
        calls.append(cmd)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(tasks.subprocess, "run", fake_run)
    assert tasks.trace_report([]) == 0
    assert calls[0][-2:] == ["-m", "driftgate.report"]


@pytest.mark.parametrize("target", ["lint", "test", "data", "e2e", "eval", "mvp-check"])
def test_other_targets_still_reject_arguments(target: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert tasks.main(["tasks.py", target, "--out", "x"]) == 2
