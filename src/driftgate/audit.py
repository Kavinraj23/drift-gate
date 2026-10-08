"""Append-only audit log of every proposal, gate decision, execution and verification."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

KINDS = (
    "proposal",
    "gate_decision",
    "execution",
    "verification",
    "rejected",
    "expired",
    "failed",
    "budget_truncated",
)


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    ts: float
    kind: str
    fingerprint: str
    execution_id: str
    payload: dict[str, Any]


class AuditLog:
    """JSONL file, opened in append mode only; there is no update or delete API."""

    def __init__(self, path: Path | str, clock: Callable[[], float]) -> None:
        self.path = Path(path)
        self._clock = clock

    def append(self, kind: str, fingerprint: str, execution_id: str, **payload: Any) -> AuditEntry:
        if kind not in KINDS:
            raise ValueError(f"unknown audit kind {kind!r}")
        entry = AuditEntry(len(self.read()), self._clock(), kind, fingerprint, execution_id, payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(entry), default=str, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return entry

    def read(self, kind: str | None = None) -> list[AuditEntry]:
        if not self.path.exists():
            return []
        entries = [
            AuditEntry(**json.loads(line))
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [e for e in entries if kind is None or e.kind == kind]
