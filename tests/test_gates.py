"""Tests for the Phase 3 safety gate (PRD.md SS5): rate limiting, kill switch, abort
ceiling, and audit trail - all against a fake target, no network needed.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from drift_gate.audit import AuditLog
from drift_gate.domain import (
    BlastRadius,
    Classification,
    DryRunResult,
    ExecutionResult,
    Gate,
    Layer,
    Remediation,
    Report,
    Tier,
)
from drift_gate.gates import KillSwitch, SafetyGate


class FakeTarget:
    def __init__(self, would_succeed=True, execute_succeeds=True):
        self.would_succeed = would_succeed
        self.execute_succeeds = execute_succeeds
        self.execute_calls = 0

    def dry_run(self, action):
        return DryRunResult(remediation=action, would_succeed=self.would_succeed,
                             preview="preview")

    def execute(self, action):
        self.execute_calls += 1
        return ExecutionResult(remediation=action, succeeded=self.execute_succeeds,
                                detail="executed", executed_at=datetime.now(timezone.utc))

    def verify(self, action):
        raise NotImplementedError


def _report(fingerprint="fp1", tier=Tier.TIER_0, blast=1) -> Report:
    remediation = Remediation(
        tier=tier, action="retry_execution", rationale="r", reversible=True,
        gate=Gate.AUTO, context={"execution_id": "e1"},
    )
    return Report(
        execution_id="e1", fingerprint=fingerprint, duplicate_of=None,
        classification=Classification.TRANSIENT, layer=Layer.L4, confidence=0.95,
        blast_radius=BlastRadius(blast, None), evidence=(), suspected_change=None,
        remediation=remediation, abstained=False, escalation_reason=None,
    )


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def kill_switch(tmp_path):
    return KillSwitch(tmp_path / "kill_switch.json")


def test_execution_proceeds_and_is_audited(audit, kill_switch):
    target = FakeTarget()
    gate = SafetyGate(target, audit=audit, kill_switch=kill_switch)
    result = gate.propose_and_execute(_report())
    assert result is not None and result.succeeded is True
    outcomes = [r.outcome for r in audit.history_for_fingerprint("fp1")]
    assert outcomes == ["proposed", "executed"]


def test_third_execution_within_an_hour_is_rate_limited(audit, kill_switch):
    target = FakeTarget()
    gate = SafetyGate(target, audit=audit, kill_switch=kill_switch)
    gate.propose_and_execute(_report())
    gate.propose_and_execute(_report())
    result = gate.propose_and_execute(_report())
    assert result is None
    assert target.execute_calls == 2
    outcomes = [r.outcome for r in audit.history_for_fingerprint("fp1")]
    assert outcomes[-1] == "skipped_rate_limited"


def test_rate_limit_is_per_fingerprint_not_global(audit, kill_switch):
    target = FakeTarget()
    gate = SafetyGate(target, audit=audit, kill_switch=kill_switch)
    gate.propose_and_execute(_report(fingerprint="fp1"))
    gate.propose_and_execute(_report(fingerprint="fp1"))
    result = gate.propose_and_execute(_report(fingerprint="fp2"))
    assert result is not None
    assert target.execute_calls == 3


def test_global_kill_switch_blocks_everything(audit, kill_switch):
    kill_switch.set("global", True)
    target = FakeTarget()
    gate = SafetyGate(target, audit=audit, kill_switch=kill_switch)
    result = gate.propose_and_execute(_report())
    assert result is None
    assert target.execute_calls == 0
    assert audit.history_for_fingerprint("fp1")[-1].outcome == "skipped_kill_switch"


def test_per_tier_kill_switch_only_blocks_that_tier(audit, kill_switch):
    kill_switch.set("tier_0", True)
    target = FakeTarget()
    gate = SafetyGate(target, audit=audit, kill_switch=kill_switch)
    blocked = gate.propose_and_execute(_report(tier=Tier.TIER_0))
    allowed = gate.propose_and_execute(_report(fingerprint="fp2", tier=Tier.TIER_3))
    assert blocked is None
    assert allowed is not None


def test_abort_ceiling_blocks_fleet_wide_blast_radius(audit, kill_switch):
    target = FakeTarget()
    gate = SafetyGate(target, audit=audit, kill_switch=kill_switch, abort_ceiling=5)
    result = gate.propose_and_execute(_report(blast=6))
    assert result is None
    assert target.execute_calls == 0
    assert audit.history_for_fingerprint("fp1")[-1].outcome == "skipped_abort_ceiling"


def test_abstained_report_is_audited_and_never_reaches_target(audit, kill_switch):
    target = FakeTarget()
    gate = SafetyGate(target, audit=audit, kill_switch=kill_switch)
    report = _report()
    abstained = Report(
        execution_id=report.execution_id, fingerprint=report.fingerprint,
        duplicate_of=None, classification=Classification.UNKNOWN, layer=Layer.L4,
        confidence=0.0, blast_radius=report.blast_radius, evidence=(),
        suspected_change=None, remediation=None, abstained=True,
        escalation_reason="no safe automated remediation",
    )
    result = gate.propose_and_execute(abstained)
    assert result is None
    assert target.execute_calls == 0
    assert audit.history_for_fingerprint("fp1")[-1].outcome == "skipped_no_remediation"


def test_failed_dry_run_never_calls_execute(audit, kill_switch):
    target = FakeTarget(would_succeed=False)
    gate = SafetyGate(target, audit=audit, kill_switch=kill_switch)
    result = gate.propose_and_execute(_report())
    assert result is None
    assert target.execute_calls == 0
    assert audit.history_for_fingerprint("fp1")[-1].outcome == "dry_run_failed"
