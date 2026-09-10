"""Full audit trail (PRD.md SS5): who/what/why/approver/outcome/timestamps on every
proposal - including rejected, skipped, and failed ones, not just executed ones.
Append-only JSON-lines file so a partial write never corrupts prior history.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class AuditRecord:
    timestamp: str
    execution_id: str
    fingerprint: str
    tier: int
    action: str
    gate: str
    who: str  # "deterministic-classifier" or "agent" - whichever produced the Report
    why: str  # the remediation's rationale, or the report's escalation_reason
    approver: str | None  # None for AUTO gate by design; real human approval is
    # Phase 3 follow-up work for single/dual-approval gates - not wired yet
    outcome: str
    detail: str


class AuditLog:
    def __init__(self, path: Path | str = "data/audit_log.jsonl"):
        self.path = Path(path)

    def record(self, **kwargs) -> AuditRecord:
        rec = AuditRecord(timestamp=datetime.now(timezone.utc).isoformat(), **kwargs)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec)) + "\n")
        return rec

    def history(self) -> list[AuditRecord]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(AuditRecord(**json.loads(line)))
        return out

    def history_for_fingerprint(self, fingerprint: str) -> list[AuditRecord]:
        return [r for r in self.history() if r.fingerprint == fingerprint]
