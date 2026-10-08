"""Stop hook: during an autonomous run, don't let the session stop while mvp-check fails unexplained."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ROOT, python_exe, read_payload  # noqa: E402

MAX_BLOCKS = 3
ROW = re.compile(r"^(?P<name>.+?)\s{2,}(?P<milestone>\S+)\s+(?P<status>pass|fail|pending)\s*$")


def failing_checks(mvp_output: str) -> list[tuple[str, str]]:
    rows = (ROW.match(line) for line in mvp_output.splitlines())
    return [(m["name"], m["milestone"]) for m in rows if m and m["status"] != "pass"]


def decide(
    *,
    stop_hook_active: bool,
    autonomous: bool,
    failing: list[tuple[str, str]],
    blockers_text: str,
    head: str,
    state: dict,
) -> tuple[bool, str, dict]:
    """Return (block, reason, new_state)."""
    if not autonomous or stop_hook_active or not failing:
        return False, "", {}
    lowered = blockers_text.lower()
    unexplained = [(n, m) for n, m in failing if n.lower() not in lowered and m.lower() not in lowered]
    if not unexplained:
        return False, "", {}
    blocks = state.get("blocks", 0) if state.get("head") == head else 0
    if blocks >= MAX_BLOCKS:
        return False, "", {}
    names = ", ".join(f"{m}: {n}" for n, m in unexplained)
    reason = f"mvp-check still fails and BLOCKERS.md does not account for: {names}. Continue the run."
    return True, reason, {"head": head, "blocks": blocks + 1}


def _git_head() -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    return out.stdout.strip()


def main() -> int:
    payload = read_payload()
    autonomous = (ROOT / ".autonomous").exists()
    if not autonomous or payload.get("stop_hook_active"):
        return 0
    result = subprocess.run([python_exe(), "tasks.py", "mvp-check"], cwd=ROOT, capture_output=True, text=True)
    blockers = ROOT / "BLOCKERS.md"
    state_file = ROOT / ".claude" / "stop_state.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    block, reason, new_state = decide(
        stop_hook_active=False,
        autonomous=True,
        failing=failing_checks(result.stdout) if result.returncode != 0 else [],
        blockers_text=blockers.read_text(encoding="utf-8") if blockers.exists() else "",
        head=_git_head(),
        state=state,
    )
    state_file.write_text(json.dumps(new_state), encoding="utf-8")
    if block:
        print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
