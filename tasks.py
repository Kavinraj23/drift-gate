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
NOT_IMPLEMENTED: dict[str, str] = {}
LIVE_TARGETS = {"record", "eval-live", "e2e-live"}  # implemented in driftgate.eval.live; human-only
#: The only offline targets that take extra arguments: `approvals` (a sub-command and its options) and `trace-report`
#: (`--audit`, `--reports`, `--out`; argparse rejects anything else). Every other target still takes none.
ARG_TARGETS = {"approvals", "trace-report"}
#: `holdout-fetch` also takes arguments (`--yes-fetch`, `--skeleton`, ...), validated by its own argparse; it is the
#: only other target that does, and it is human-only (read-only GET-only fetch of public logs; plan mode by default).
FETCH_TARGETS = {"holdout-fetch"}
HUMAN_ONLY = {"record", "eval-live", "e2e-live", "playground-reset", "holdout-fetch"}
# N1 holdout runner. Both take options, validated by their own argparse, and neither is in LIVE_TARGETS or ARG_TARGETS
# (existing tests pin those sets exactly, as with holdout-fetch). `main` dispatches them explicitly:
#   run-holdout     offline: driftgate.eval.holdout_run (replay fixtures or the plumbing double; no key, no spend)
#   record-holdout  HUMAN ONLY: driftgate.eval.live record-holdout (--estimate | --yes-spend; real API under the caps)
RUNNER_TARGETS = {"run-holdout", "record-holdout"}
RECORD_HOLDOUT = "record-holdout"
HUMAN_ONLY |= {RECORD_HOLDOUT}

# Milestones whose targets exist; mvp-check treats every other milestone as pending.
IMPLEMENTED_MILESTONES: set[str] = {"M0", "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8a", "M9a"}

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


def e2e() -> int:
    """Every synthetic scenario end to end on the scripted fake model (or replay fixtures where they exist)."""
    if not (ROOT / "data" / "executions.json").exists():
        code = data()
        if code:
            return code
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    cmd = [PY, "-m", "driftgate.eval.e2e", "--data", str(ROOT / "data"), "--mode", "auto"]
    cmd += ["--fixtures", str(ROOT / "fixtures" / "replay")]
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode


def eval_() -> int:
    """Agent vs. baseline metrics table, offline (scripted model or replay fixtures); writes data/eval/report.*."""
    if not (ROOT / "data" / "executions.json").exists():
        code = data()
        if code:
            return code
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    cmd = [PY, "-m", "driftgate.eval.report", "--data", str(ROOT / "data"), "--out", str(ROOT / "data" / "eval")]
    cmd += ["--mode", "auto", "--fixtures", str(ROOT / "fixtures" / "replay")]
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode


def eval_retrieval() -> int:
    """similar_history on vs. off over the synthetic scenarios, offline (scripted model; plumbing, not quality)."""
    if not (ROOT / "data" / "executions.json").exists():
        code = data()
        if code:
            return code
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    cmd = [
        PY,
        "-m",
        "driftgate.eval.retrieval_eval",
        "--data",
        str(ROOT / "data"),
        "--out",
        str(ROOT / "data" / "eval"),
    ]
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode


def eval_tier12() -> int:
    """Tier 1/2 approval cases on the synthetic lock table: expected decisions from the PRD rules vs. the code."""
    if not (ROOT / "data" / "executions.json").exists():
        code = data()
        if code:
            return code
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    cmd = [PY, "-m", "driftgate.eval.tier12_cases", "--data", str(ROOT / "data")]
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode


def approvals(extra: list[str]) -> int:
    """Tier 1/2 approvals CLI (synthetic target only): list | show | approve | deny | expire-check | execute."""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    # Nothing is injected and nothing can be overridden: the CLI pins the gate inputs and rejects unknown options.
    cmd = [PY, "-m", "driftgate.approvals_cli", *extra]
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode


def trace_report(extra: list[str]) -> int:
    """Static HTML trace pages from an audit log, offline. No arguments: the committed sample into data/report/."""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    cmd = [PY, "-m", "driftgate.report", *extra]
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode


def _holdout(command: str) -> int:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    cmd = [PY, "-m", "driftgate.eval.holdout_eval", command, "--dir", str(ROOT / "holdout")]
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode


def holdout_validate() -> int:
    """Schema, quotas, hashes, redaction and label timing of the real-log holdout (offline; fails until it is full)."""
    return _holdout("validate")


def eval_holdout() -> int:
    """Agent vs. baseline on the stored real logs, offline: dev per case, test aggregate only (no API key)."""
    return _holdout("eval")


def holdout_fetch(extra: list[str]) -> int:
    """Human-only: store the failed step's log of the runs in holdout/candidates.json. Plan mode unless --yes-fetch."""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    cmd = [PY, "-m", "driftgate.eval.holdout_fetch", *extra]
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode


def playground_reset() -> int:
    """Human-only: force scenario branches back to their baseline tags (see scripts/playground.py)."""
    return run([PY, "scripts/playground.py", "reset", "--yes"])


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
    "e2e": e2e,
    "eval": eval_,
    "holdout-validate": holdout_validate,
    "eval-holdout": eval_holdout,
    "holdout-fetch": lambda: holdout_fetch([]),
    "run-holdout": lambda: run_holdout([]),
    "record-holdout": lambda: _live(RECORD_HOLDOUT, []),
    "eval-retrieval": eval_retrieval,
    "eval-tier12": eval_tier12,
    "mvp-check": mvp_check,
    "playground-reset": playground_reset,
}


def _live(target: str, extra: list[str]) -> int:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    cmd = [PY, "-m", "driftgate.eval.live", target, *extra]
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode


def run_holdout(extra: list[str]) -> int:
    """Offline: run the agent on the stored real-log holdout (replay fixtures or the deterministic double); no key."""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    cmd = [PY, "-m", "driftgate.eval.holdout_run", "--dir", str(ROOT / "holdout"), *extra]
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode


def autonomous_present() -> bool:
    """`.autonomous` at the repo root, or at the main checkout when this is a worktree; a dangling symlink counts."""
    src = str(Path(__file__).resolve().parent / "src")
    if not sys.path or sys.path[0] != src:
        sys.path.insert(0, src)
    from driftgate.autonomy import autonomous_present_at

    return autonomous_present_at(ROOT)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python tasks.py <target> [options]")
        return 2
    target = argv[1]
    if len(argv) > 2 and target not in LIVE_TARGETS | ARG_TARGETS | FETCH_TARGETS | RUNNER_TARGETS:
        print("usage: python tasks.py <target>")
        return 2
    if target in HUMAN_ONLY and autonomous_present():
        print(f"refusing: '{target}' is human-only and .autonomous exists")
        return 1
    if target == RECORD_HOLDOUT:  # not in LIVE_TARGETS (pinned by tests); the live module still enforces its own caps
        return _live(RECORD_HOLDOUT, argv[2:])
    if target == "run-holdout":
        return run_holdout(argv[2:])
    if target in LIVE_TARGETS:
        return _live(target, argv[2:])
    if target in FETCH_TARGETS:
        return holdout_fetch(argv[2:])
    if target == "approvals":
        return approvals(argv[2:])
    if target == "trace-report":
        return trace_report(argv[2:])
    if target in HANDLERS:
        return HANDLERS[target]()
    if target in NOT_IMPLEMENTED:
        print(f"not implemented ({NOT_IMPLEMENTED[target]})")
        return 1
    print(f"unknown target: {target}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
