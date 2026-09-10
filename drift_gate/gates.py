"""Safety gate wrapping RemediationTarget (PRD.md SS5): rate limiting per
fingerprint, kill switch (global + per-tier), abort ceiling on blast radius, and a
full audit trail on every proposal - including rejected/skipped ones, not just
executed ones.

Deliberately sits BETWEEN a Report and the real RemediationTarget - the discipline
is the same as ExecutionSource/RemediationTarget sitting between the agent and a
specific CI provider (PRD.md SS9). Nothing downstream of Phase 1/4 should call
target.execute() directly once this exists; scripts/run_github_demo.py was updated
to go through it.

Not implemented here (real Tier 1/2 actions don't exist yet - Phase 5 is
simulated-only): single/dual human approval workflows, and the "deterministic
evidence required for Tier 2+" check (PRD.md SS5) - that's currently only enforced
by the agent's own system prompt (agent/loop.py), not mechanically by this gate.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from drift_gate.audit import AuditLog
from drift_gate.domain import ExecutionResult, Report, Tier
from drift_gate.protocols import RemediationTarget

RATE_LIMIT_WINDOW = timedelta(hours=1)
RATE_LIMIT_MAX_PER_FINGERPRINT = 2  # PRD SS5: "two attempts, then hard escalate"
DEFAULT_ABORT_CEILING = 5  # PRD SS5: blast radius exceeding N stops and escalates


@dataclass
class KillSwitch:
    """Global + per-tier kill switch (PRD.md SS5), backed by a small JSON file so it
    can be flipped without redeploying code. Missing file == everything open."""
    path: Path = field(default_factory=lambda: Path("config/kill_switch.json"))

    def _load(self) -> dict:
        if not self.path.exists():
            return {"global": False, "tier_0": False, "tier_1": False,
                     "tier_2": False, "tier_3": False}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def is_blocked(self, tier: Tier) -> bool:
        state = self._load()
        return bool(state.get("global")) or bool(state.get(f"tier_{tier.value}"))

    def set(self, key: str, blocked: bool) -> None:
        state = self._load()
        state[key] = blocked
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, indent=2), encoding="utf-8")


class SafetyGate:
    def __init__(
        self, target: RemediationTarget, audit: AuditLog | None = None,
        kill_switch: KillSwitch | None = None,
        abort_ceiling: int = DEFAULT_ABORT_CEILING,
    ):
        self.target = target
        self.audit = audit or AuditLog()
        self.kill_switch = kill_switch or KillSwitch()
        self.abort_ceiling = abort_ceiling

    def _record(self, report: Report, outcome: str, detail: str, who: str) -> None:
        remediation = report.remediation
        self.audit.record(
            execution_id=report.execution_id, fingerprint=report.fingerprint,
            tier=remediation.tier.value if remediation else -1,
            action=remediation.action if remediation else "none",
            gate=remediation.gate.value if remediation else "none",
            who=who,
            why=(remediation.rationale if remediation
                 else (report.escalation_reason or "")),
            approver=None, outcome=outcome, detail=detail,
        )

    def _rate_limited(self, fingerprint: str) -> bool:
        cutoff = datetime.now(timezone.utc) - RATE_LIMIT_WINDOW
        recent_executions = [
            r for r in self.audit.history_for_fingerprint(fingerprint)
            if r.outcome == "executed" and datetime.fromisoformat(r.timestamp) >= cutoff
        ]
        return len(recent_executions) >= RATE_LIMIT_MAX_PER_FINGERPRINT

    def propose_and_execute(
        self, report: Report, who: str = "deterministic-classifier",
    ) -> ExecutionResult | None:
        """Runs one remediation through every SS5 safety constraint before touching
        the real target. Returns None on any rejection - the caller's job is then to
        hard-escalate, per SS5's "two attempts, then hard escalate."
        """
        remediation = report.remediation
        self._record(report, "proposed", "remediation proposed", who)

        if remediation is None:
            self._record(report, "skipped_no_remediation", "report abstained", who)
            return None

        if report.blast_radius.executions_affected > self.abort_ceiling:
            self._record(
                report, "skipped_abort_ceiling",
                f"blast radius {report.blast_radius.executions_affected} exceeds "
                f"ceiling {self.abort_ceiling} - fleet-wide, not for an agent to fix",
                who,
            )
            return None

        if self.kill_switch.is_blocked(remediation.tier):
            self._record(
                report, "skipped_kill_switch",
                f"tier {remediation.tier.value} or global kill switch active", who,
            )
            return None

        if remediation.tier == Tier.TIER_0 and self._rate_limited(report.fingerprint):
            self._record(
                report, "skipped_rate_limited",
                f"fingerprint already executed {RATE_LIMIT_MAX_PER_FINGERPRINT}x in "
                f"the last hour - hard escalate per SS5", who,
            )
            return None

        dry = self.target.dry_run(remediation)
        if not dry.would_succeed:
            self._record(report, "dry_run_failed", dry.preview, who)
            return None

        result = self.target.execute(remediation)
        self._record(
            report, "executed" if result.succeeded else "execute_failed",
            result.detail, who,
        )
        return result
