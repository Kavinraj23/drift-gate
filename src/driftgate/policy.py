"""Operator policy read from `config/policy.json`: currently only the abort ceiling.

The abort ceiling N means "a blast radius over N executions is fleet-wide and is escalated to a human". The
PRD gives no value, so it lives in config where the human can change it. A missing, unreadable or invalid file
fails closed to the strictest value (0: any correlated failure escalates), never to a permissive default.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "policy.json"
STRICTEST_ABORT_CEILING = 0
MAX_ABORT_CEILING = 1000


@dataclass(frozen=True)
class Policy:
    abort_ceiling: int
    source: str  # "file" or "fail_closed: <why>"


def load_policy(path: Path | str = DEFAULT_POLICY_PATH) -> Policy:
    def closed(why: str) -> Policy:
        return Policy(STRICTEST_ABORT_CEILING, f"fail_closed: {why}")

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return closed(type(exc).__name__)
    if not isinstance(raw, dict):
        return closed("not an object")
    n = raw.get("abort_ceiling")
    if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= MAX_ABORT_CEILING:
        return closed(f"abort_ceiling must be an integer 0..{MAX_ABORT_CEILING}")
    return Policy(n, "file")
