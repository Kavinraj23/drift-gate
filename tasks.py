"""Task runner: `python tasks.py <target>`. Same targets as the PRD's `make` targets."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
PY = str(_VENV_PY) if _VENV_PY.exists() else sys.executable

# Targets whose implementation arrives in a later milestone: name -> milestone.
NOT_IMPLEMENTED: dict[str, str] = {
    "e2e": "M5",
    "eval": "M9a",
    "record": "M5",
    "eval-live": "M9b",
    "e2e-live": "M8b",
    "playground-reset": "M8b",
}
HUMAN_ONLY = {"record", "eval-live", "e2e-live", "playground-reset"}

# Milestones whose targets exist; mvp-check treats every other milestone as pending.
IMPLEMENTED_MILESTONES: set[str] = {"M0", "M1", "M2", "M3", "M4"}

# Each MVP acceptance check: (name, milestone, command). Mirrors TASKS.md.
CHECKS: list[tuple[str, str, list[str]]] = [
    ("lint + tests + hook tests", "M0", [PY, "tasks.py", "test"]),
    ("seeded dataset generates", "M1", [PY, "tasks.py", "data"]),
    ("gateway tests (fake clock, no SDK outside gateway)", "M2", [PY, "-m", "pytest", "-q", "tests/gateway"]),
    ("baseline prints scores", "M3", [PY, "tasks.py", "baseline"]),
    ("gate tests", "M4", [PY, "-m", "pytest", "-q", "tests/gates"]),
    ("e2e on replay fixtures", "M5", [PY, "tasks.py", "e2e"]),
    ("tier 3 e2e + reviewer", "M6", [PY, "-m", "pytest", "-q", "tests/tier3"]),
    ("verification + re-investigation", "M7", [PY, "-m", "pytest", "-q", "tests/verify"]),
    ("github adapter on stubbed HTTP (M8a)", "M8a", [PY, "-m", "pytest", "-q", "tests/github"]),
    ("eval table + README (M9a)", "M9a", [PY, "tasks.py", "eval"]),
]


def run(cmd: list[str]) -> int:
    return subprocess.run(cmd, cwd=ROOT).returncode


def install() -> int:
    return run([PY, "-m", "pip", "install", "-e", ".[dev]"])


def lint() -> int:
    paths = [p for p in ("src", "tests", "tasks.py", ".claude/hooks") if (ROOT / p).exists()]
    return run([PY, "-m", "ruff", "format", "--check", *paths]) or run([PY, "-m", "ruff", "check", *paths])


def test() -> int:
    return run([PY, "-m", "pytest", "-q"])


def data() -> int:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run([PY, "-m", "driftgate.generator", "--out", str(ROOT / "data")], cwd=ROOT, env=env).returncode


def baseline() -> int:
    """Score the deterministic baseline against ground truth; generates the dataset first if data/ is missing."""
    if not (ROOT / "data" / "executions.json").exists():
        code = data()
        if code:
            return code
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    cmd = [PY, "-m", "driftgate.eval.baseline_score", "--data", str(ROOT / "data")]
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode


def mvp_check() -> int:
    rows: list[tuple[str, str, str]] = []
    for name, milestone, cmd in CHECKS:
        if milestone not in IMPLEMENTED_MILESTONES:
            rows.append((name, milestone, "pending"))
            continue
        ok = subprocess.run(cmd, cwd=ROOT, capture_output=True).returncode == 0
        rows.append((name, milestone, "pass" if ok else "fail"))
    width = max(len(r[0]) for r in rows)
    print(f"{'check'.ljust(width)}  milestone  status")
    for name, milestone, status in rows:
        print(f"{name.ljust(width)}  {milestone.ljust(9)}  {status}")
    return 0 if all(r[2] == "pass" for r in rows) else 1


HANDLERS: dict[str, Callable[[], int]] = {
    "install": install,
    "lint": lint,
    "test": test,
    "data": data,
    "baseline": baseline,
    "mvp-check": mvp_check,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python tasks.py <target>")
        return 2
    target = argv[1]
    if target in HUMAN_ONLY and (ROOT / ".autonomous").exists():
        print(f"refusing: '{target}' is human-only and .autonomous exists")
        return 1
    if target in HANDLERS:
        return HANDLERS[target]()
    if target in NOT_IMPLEMENTED:
        print(f"not implemented ({NOT_IMPLEMENTED[target]})")
        return 1
    print(f"unknown target: {target}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
