"""Holdout labels are reachable only from eval/, the eval path is offline, and the task targets are wired."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "driftgate"

_spec = importlib.util.spec_from_file_location("tasks", ROOT / "tasks.py")
tasks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tasks)


def test_holdout_is_referenced_only_in_eval() -> None:
    offenders = []
    for p in sorted(SRC.rglob("*.py")):
        rel = p.relative_to(SRC).as_posix()
        if rel.startswith("eval/"):
            continue
        if "holdout" in p.read_text(encoding="utf-8").lower():
            offenders.append(rel)
    assert offenders == []


def test_agents_tools_and_gateway_cannot_import_the_holdout_modules() -> None:
    for sub in ("agents", "tools", "llm", "gates.py", "orchestrator.py"):
        path = SRC / sub
        files = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for p in files:
            assert not re.search(r"driftgate\.eval|from driftgate import eval", p.read_text(encoding="utf-8")), p


def test_holdout_modules_cannot_reach_the_network_the_sdk_or_the_environment() -> None:
    for name in ("holdout.py", "holdout_eval.py"):
        src = (SRC / "eval" / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|from)\s+(anthropic|requests|urllib|socket|dotenv|http)", src, re.M), name
        assert "load_config" not in src and "environ" not in src and "getenv" not in src and ".env" not in src, name
        assert "driftgate.llm" not in src, name


def test_targets_are_implemented_and_fetch_stays_a_stub() -> None:
    assert {"holdout-validate", "eval-holdout"} <= set(tasks.HANDLERS)
    assert not {"holdout-validate", "eval-holdout"} & set(tasks.NOT_IMPLEMENTED)
    assert tasks.NOT_IMPLEMENTED["holdout-fetch"] == "N1"
    assert not {"holdout-validate", "eval-holdout"} & tasks.HUMAN_ONLY


def test_targets_run_the_holdout_module_offline(monkeypatch) -> None:  # noqa: ANN001
    calls: list[list[str]] = []
    monkeypatch.setattr(
        tasks.subprocess, "run", lambda cmd, **k: calls.append(cmd) or type("R", (), {"returncode": 0})()
    )
    assert tasks.holdout_validate() == 0 and tasks.eval_holdout() == 0
    assert all("driftgate.eval.holdout_eval" in c for c in calls)
    assert [c[c.index("driftgate.eval.holdout_eval") + 1] for c in calls] == ["validate", "eval"]
