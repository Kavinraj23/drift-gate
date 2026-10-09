"""Append-only audit log of every proposal, gate decision, execution and verification."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from driftgate.redaction import redact_value

if TYPE_CHECKING:
    from driftgate.gates import GateDecision

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
        self._seq = len(self.read())  # initialised once; the file is append-only

    def append(self, kind: str, fingerprint: str, execution_id: str, **payload: Any) -> AuditEntry:
        if kind not in KINDS:
            raise ValueError(f"unknown audit kind {kind!r}")
        safe = {k: redact_value(v) for k, v in payload.items()}  # free text never lands in the log with a credential
        entry = AuditEntry(self._seq, self._clock(), kind, fingerprint, execution_id, safe)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(entry), default=str, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self._seq += 1
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


def record_decision(audit: AuditLog, decision: GateDecision, fingerprint: str, execution_id: str) -> AuditEntry:
    """Append a gate_decision entry for a SafetyGate result."""
    return audit.append(
        "gate_decision",
        fingerprint,
        execution_id,
        decision=decision.decision,
        reason=decision.reason,
        action=decision.remediation.action,
        proposed_tier=decision.proposed_tier,
        agent_confidence=decision.agent_confidence,
        abort_ceiling=decision.abort_ceiling,
    )
