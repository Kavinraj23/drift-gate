"""Audit log completeness and SyntheticTarget behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from driftgate.adapters.synthetic import LockEntry, LockTable, SimulatedOutcome, SyntheticTarget
from driftgate.audit import KINDS, AuditLog, record_decision
from driftgate.domain import Remediation, RemediationTarget


def rem(tier: int = 0, action: str = "rerun", **dry: object) -> Remediation:
    gates = {0: "auto", 1: "single_approval", 2: "dual_approval", 3: "pull_request"}
    return Remediation(tier, action, "r", True, gates[tier], "allowed", dict(dry))


def test_audit_records_every_kind_in_order_and_is_append_only(tmp_path: Path) -> None:
    t = iter(range(100))
    log = AuditLog(tmp_path / "sub" / "audit.jsonl", clock=lambda: float(next(t)))
    for kind in KINDS:
        log.append(kind, "fp", "ex1", note=kind)
    entries = log.read()
    assert [e.kind for e in entries] == list(KINDS)
    assert [e.seq for e in entries] == list(range(len(KINDS)))
    assert [e.ts for e in entries] == sorted(e.ts for e in entries)
    assert log.read("rejected")[0].payload == {"note": "rejected"}
    before = (tmp_path / "sub" / "audit.jsonl").read_text()
    log.append("failed", "fp", "ex2")
    assert (tmp_path / "sub" / "audit.jsonl").read_text().startswith(before)


def test_audit_survives_reopen_and_rejects_unknown_kind(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    AuditLog(p, lambda: 1.0).append("proposal", "fp", "e", action="rerun")
    log2 = AuditLog(p, lambda: 2.0)
    assert log2.append("gate_decision", "fp", "e", decision="downgraded").seq == 1
    with pytest.raises(ValueError):
        log2.append("bogus", "fp", "e")
    assert len(log2.read()) == 2


def test_synthetic_target_satisfies_protocol() -> None:
    target: RemediationTarget = SyntheticTarget()
    assert target.dry_run(rem()).ok and target.execute(rem()).ok and target.verify(rem()).verified


def test_injected_outcomes_per_action() -> None:
    t = SyntheticTarget(
        outcomes={"recycle_runner": SimulatedOutcome(dry_run_ok=True, execute_ok=False)},
        default=SimulatedOutcome(verified=False),
    )
    assert not t.execute(rem(1, "recycle_runner")).ok
    assert t.execute(rem(0, "rerun")).ok
    assert not t.verify(rem(0, "rerun")).verified
    assert [r.action for r in t.executed] == ["rerun"]
    assert ("execute", "recycle_runner") in t.calls


def test_force_unlock_dead_holder_releases_lock() -> None:
    table = LockTable({"state-1": LockEntry("runner-9", alive=False)})
    t = SyntheticTarget(lock_table=table)
    a = rem(2, "force_unlock", lock_id="state-1")
    dr = t.dry_run(a)
    assert dr.ok and dr.details["holder_status"] == "dead"
    assert t.execute(a).ok
    assert "state-1" not in table.locks
    assert t.verify(a).verified


def test_force_unlock_running_holder_refused_by_target_too() -> None:
    table = LockTable({"state-1": LockEntry("runner-9", alive=True)})
    t = SyntheticTarget(lock_table=table)
    a = rem(2, "force_unlock", lock_id="state-1")
    assert not t.dry_run(a).ok
    assert not t.execute(a).ok
    assert "state-1" in table.locks
    assert not t.verify(a).verified


def test_force_unlock_missing_lock() -> None:
    t = SyntheticTarget()
    a = rem(2, "force_unlock", lock_id="nope")
    assert not t.dry_run(a).ok and not t.execute(a).ok


@pytest.mark.parametrize("gate_decision", ["downgraded", "refused"])
def test_target_refuses_actions_not_allowed_by_gate(gate_decision: str) -> None:
    t = SyntheticTarget()
    a = Remediation(0, "rerun", "r", True, "auto", gate_decision, {})
    assert not t.dry_run(a).ok
    assert not t.execute(a).ok
    assert t.executed == []


def test_force_unlock_not_allowed_leaves_lock() -> None:
    table = LockTable({"s": LockEntry("r", alive=False)})
    t = SyntheticTarget(lock_table=table)
    a = Remediation(2, "force_unlock", "r", False, "dual_approval", "downgraded", {"lock_id": "s"})
    assert not t.execute(a).ok and "s" in table.locks


def test_record_decision_writes_audit_entry(tmp_path: Path) -> None:
    from driftgate.gates import GateFacts, KillSwitch, RateLimiter, SafetyGate

    sw = tmp_path / "ks.json"
    sw.write_text('{"global": true, "tiers": {"0": true, "1": true, "2": true, "3": true}}')
    gate = SafetyGate(KillSwitch(sw), RateLimiter(lambda: 1.0), 10)
    log = AuditLog(tmp_path / "a.jsonl", lambda: 5.0)
    facts = GateFacts(
        "fp",
        signature_match=True,
        flake_precedent=True,
        proposed_set_size=1,
        resource_set_total=3,
        agent_confidence=0.4,
    )
    entry = record_decision(log, gate.decide(rem(0), facts), "fp", "ex1")
    [read] = log.read("gate_decision")
    assert read == entry and read.payload["decision"] == "allowed" and read.payload["agent_confidence"] == 0.4


def test_audit_seq_counter_does_not_reread_file(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "a.jsonl", lambda: 0.0)
    assert [log.append("proposal", "f", "e").seq for _ in range(3)] == [0, 1, 2]
    calls: list[int] = []
    orig = log.read

    def spy(kind: str | None = None):  # noqa: ANN202
        calls.append(1)
        return orig(kind)

    log.read = spy  # type: ignore[method-assign]
    assert log.append("failed", "f", "e").seq == 3 and calls == []
