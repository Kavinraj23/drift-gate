"""The approval flow end to end on the synthetic target: request, approve, deny, expire, execute, audit."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from driftgate.approval_flow import execute_approved
from driftgate.approvals import TIER1_TIMEOUT_S, ApprovalError, ApprovalStore, DuplicateDecision, Expired, SelfApproval
from driftgate.eval.ground_truth import FailureLabel, GroundTruth
from driftgate.eval.tier12_cases import (
    DEDICATED_CASES,
    TRIGGER,
    Holder,
    ManualClock,
    World,
    run_case,
    run_scenario,
    scenario_expectation,
    tier12_scenarios,
)
from driftgate.gates import GateFacts, KillSwitch, RateLimiter, SafetyGate
from driftgate.orchestrator import AWAITING_APPROVAL, ESCALATED

MakeWorld = Callable[..., World]
DEAD = Holder()


def stages(world: World, kind: str) -> list[str]:
    return [str(e.payload.get("stage")) for e in world.orch.audit.read(kind)]


def approve_all(world: World, *people: str) -> None:
    for p in people:
        world.service.approve(world.request_id, p)  # type: ignore[arg-type]


def run_execute(world: World):  # type: ignore[no-untyped-def]
    return execute_approved(world.orch, world.store, world.request_id, clock=world.clock)  # type: ignore[arg-type]


# -- the eval cases, with expectations written from the PRD (see tier12_cases) ---------------------------------------


@pytest.mark.parametrize("case", DEDICATED_CASES, ids=lambda c: c.case_id)
def test_dedicated_case_matches_the_prd_rules(case, dataset_dir: Path, truth: GroundTruth, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from driftgate.eval.tier12_cases import pick_label

    r = run_case(case, pick_label(truth, 1 if case.scenario == "tier1" else 2), dataset_dir, tmp_path)
    assert r.ok, f"{r.expected} != {r.actual} ({r.notes})"


def test_every_labelled_tier1_and_tier2_scenario_gets_the_gate_decision_the_prd_demands(
    truth: GroundTruth, dataset_dir: Path, tmp_path: Path
) -> None:
    labels = tier12_scenarios(truth)
    assert sorted(s.correct_tier for s in labels) == [1, 1, 1, 1, 2, 2, 2, 2]  # 4 Tier 1 and 4 Tier 2
    for i, label in enumerate(labels):
        work = tmp_path / f"s{i}"
        work.mkdir()
        r = run_scenario(label, dataset_dir, work)
        assert r.ok, f"{r.case_id}: {r.expected} != {r.actual}"
    # and the expectation itself is the PRD rule, not an echo of the label's 'escalate' disposition:
    assert [scenario_expectation(s)[0] for s in labels if s.correct_tier == 2] == ["downgraded"] * 4
    assert {scenario_expectation(s)[0] for s in labels if s.correct_tier == 1 and s.fleet_wide} == {"downgraded"}
    assert {scenario_expectation(s)[0] for s in labels if s.correct_tier == 1 and not s.fleet_wide} == {"allowed"}


# -- requesting ------------------------------------------------------------


def test_a_tier1_proposal_that_passes_the_gate_becomes_a_pending_request_and_nothing_executes(
    make_world: MakeWorld, tier1_label: FailureLabel
) -> None:
    w = make_world(tier1_label)
    out = w.outcome
    assert out.kind == AWAITING_APPROVAL and out.gate is not None and out.gate.allowed
    req = w.store.get(out.approval_id)  # type: ignore[arg-type]
    assert req.status == "pending" and req.tier == 1 and req.required == 1 and req.requested_by == TRIGGER
    assert req.expires_at - req.requested_at == TIER1_TIMEOUT_S
    assert w.orch.target.executed == [] and [c[0] for c in w.orch.target.calls] == ["dry_run"]  # type: ignore[attr-defined]
    assert stages(w, "proposal").count("approval_requested") == 1


def test_approving_does_not_execute_only_execute_does(make_world: MakeWorld, tier1_label: FailureLabel) -> None:
    w = make_world(tier1_label)
    approve_all(w, "alice")
    assert w.store.get(w.request_id).status == "approved"  # type: ignore[arg-type]
    assert w.orch.target.executed == []  # type: ignore[attr-defined]
    res = run_execute(w)
    assert res.kind == "executed" and len(w.orch.target.executed) == 1  # type: ignore[attr-defined]


def test_a_run_whose_trigger_is_unknown_opens_no_request(
    dataset_dir: Path, tmp_path: Path, tier1_label: FailureLabel
) -> None:
    from driftgate.eval import tier12_cases as t

    w = t.build_world(tier1_label, dataset_dir, tmp_path)
    w.close()
    # Same wiring, but nobody can say who triggered the run: self-approval could not be prevented, so fail closed.
    from driftgate.orchestrator import build_synthetic_orchestrator

    store = ApprovalStore(tmp_path / "other.sqlite", ManualClock())
    try:
        orch = build_synthetic_orchestrator(
            dataset_dir,
            lambda _e, _b: t._proposal_model(tier1_label, 0.9),
            audit_path=tmp_path / "audit2.jsonl",
            clock=ManualClock(),
            kill_switch_path=w.kill_switch,
            approvals=store,
            triggered_by=lambda _e: None,
        )
        out = orch.handle(tier1_label.execution_id)
        assert out.kind == ESCALATED and "approval request refused" in out.reason and store.list() == []
        assert orch.target.executed == []  # type: ignore[attr-defined]
    finally:
        store.close()


# -- model confidence, the lock id and the proof ------------------------------------------------------------


def test_tier2_ignores_model_confidence_in_both_directions(make_world: MakeWorld, tier2_label: FailureLabel) -> None:
    no_proof = make_world(tier2_label, Holder(in_table=False), confidence=1.0)
    assert no_proof.outcome.gate.decision == "downgraded"  # type: ignore[union-attr]
    assert no_proof.request_id is None and no_proof.orch.target.calls == []  # type: ignore[attr-defined]
    assert no_proof.outcome.gate.agent_confidence == 1.0  # type: ignore[union-attr]  # recorded, never consulted
    proof = make_world(tier2_label, DEAD, confidence=0.0)
    assert proof.outcome.gate.allowed and proof.request_id is not None  # type: ignore[union-attr]


def test_the_gate_never_reads_confidence(tmp_path: Path) -> None:
    sw = tmp_path / "k.json"
    sw.write_text(json.dumps({"global": True, "tiers": {"2": True}}))
    gate = SafetyGate(KillSwitch(sw), RateLimiter(lambda: 0.0), 2)
    from driftgate.domain import Remediation

    p = Remediation(2, "force_unlock_state", "r", False, "dual_approval")
    base = {"fingerprint": "fp", "proposed_set_size": 1, "resource_set_total": 5}
    proven = {**base, "lock_holder": "dead", "holder_death_proof": True, "dry_run_ok": True}
    for conf in (None, 0.0, 0.5, 1.0):
        assert (
            not SafetyGate(KillSwitch(sw), RateLimiter(lambda: 0.0), 2)
            .decide(p, GateFacts(**base, agent_confidence=conf))
            .allowed
        )
        assert (
            SafetyGate(KillSwitch(sw), RateLimiter(lambda: 0.0), 2)
            .decide(p, GateFacts(**proven, agent_confidence=conf))
            .allowed
        )
    assert gate is not None


def test_the_lock_id_comes_from_the_log_not_from_the_model(make_world: MakeWorld, tier2_label: FailureLabel) -> None:
    w = make_world(tier2_label, DEAD)
    approve_all(w, "alice", "bob")
    assert run_execute(w).kind == "executed"
    executed = w.orch.target.executed[0]  # type: ignore[attr-defined]
    assert executed.dry_run["lock_id"] == w.lock_id and executed.dry_run["lock_id"] != "model-invented-lock"
    assert w.table.locks == {}  # the simulated lock was released


# -- an approval can never skip a gate ------------------------------------------------------------


def force_request(world: World, label: FailureLabel, *, action: str, tier: int) -> int:
    """Open a request for `label`'s failure by hand, as if the gate had allowed it earlier."""
    return world.store.create(
        execution_id=label.execution_id,
        fingerprint=world.orch.analyzer.analyze(label.execution_id).fingerprint,  # type: ignore[union-attr]
        tier=tier,
        action=action,
        requested_by=TRIGGER,
        payload={"rationale": "r", "reversible": tier == 1},
    ).id


def test_two_approvals_cannot_stand_in_for_a_missing_holder_proof(
    make_world: MakeWorld, tier2_label: FailureLabel
) -> None:
    w = make_world(tier2_label, Holder(in_table=False))  # no proof at request time: no request
    rid = force_request(w, tier2_label, action="force_unlock_state", tier=2)
    w.service.approve(rid, "alice")
    w.service.approve(rid, "bob")
    res = execute_approved(w.orch, w.store, rid, clock=w.clock)
    assert res.kind == "escalated" and res.gate is not None and res.gate.decision == "downgraded"
    assert w.store.get(rid).status == "cancelled" and w.orch.target.executed == []  # type: ignore[attr-defined]


def test_an_approval_cannot_override_the_abort_ceiling(
    make_world: MakeWorld, truth: GroundTruth, tier1_label: FailureLabel
) -> None:
    fleet = next(s for s in tier12_scenarios(truth) if s.correct_tier == 1 and s.fleet_wide)
    w = make_world(tier1_label)
    rid = force_request(w, fleet, action=str(fleet.correct_action), tier=1)
    w.service.approve(rid, "alice")
    res = execute_approved(w.orch, w.store, rid, clock=w.clock)
    assert res.gate is not None and res.gate.decision == "downgraded" and "fleet-wide" in res.reason
    assert w.store.get(rid).status == "cancelled" and w.orch.target.executed == []  # type: ignore[attr-defined]


def test_a_kill_switch_flipped_after_approval_still_blocks_and_flipping_back_does_not_revive_it(
    make_world: MakeWorld, tier1_label: FailureLabel
) -> None:
    w = make_world(tier1_label)
    approve_all(w, "alice")
    state = {"global": False, "tiers": {"0": True, "1": True, "2": True, "3": True}}
    w.kill_switch.write_text(json.dumps(state))
    res = run_execute(w)
    assert res.kind == "escalated" and res.gate is not None and res.gate.decision == "refused"
    assert "kill switch" in res.reason and w.orch.target.executed == []  # type: ignore[attr-defined]
    state["global"] = True
    w.kill_switch.write_text(json.dumps(state))
    assert run_execute(w).kind == "escalated"  # the request was closed; a new one is needed
    assert w.store.get(w.request_id).status == "cancelled"  # type: ignore[arg-type]


def test_a_malformed_kill_switch_file_fails_closed_at_execution(
    make_world: MakeWorld, tier1_label: FailureLabel
) -> None:
    w = make_world(tier1_label)
    approve_all(w, "alice")
    w.kill_switch.write_text("{not json")
    assert run_execute(w).kind == "escalated" and w.orch.target.executed == []  # type: ignore[attr-defined]


def test_a_holder_that_became_live_after_approval_is_refused_not_downgraded(
    make_world: MakeWorld, tier2_label: FailureLabel
) -> None:
    w = make_world(tier2_label, DEAD)
    approve_all(w, "alice", "bob")
    w.table.locks[w.lock_id].alive = True
    res = run_execute(w)
    assert res.gate is not None and res.gate.decision == "refused" and "running" in res.reason
    assert w.table.locks and w.orch.target.executed == []  # type: ignore[attr-defined]


def test_holder_proof_is_re_derived_not_taken_from_the_stored_payload(
    make_world: MakeWorld, tier2_label: FailureLabel
) -> None:
    w = make_world(tier2_label, DEAD)
    assert w.store.get(w.request_id).payload["holder_proof"]["proven"] is True  # type: ignore[arg-type]
    approve_all(w, "alice", "bob")
    del w.table.locks[w.lock_id]  # the lock vanished: the stored proof is stale
    res = run_execute(w)
    assert res.kind == "escalated" and res.gate is not None and not res.gate.allowed


def _write_attempts(world: World, n: int, age_s: float) -> None:
    fp = world.store.get(world.request_id).fingerprint  # type: ignore[arg-type]
    now = world.clock.now
    world.clock.now = now - age_s
    for i in range(n):
        world.orch.audit.append("gate_decision", fp, f"ex-other-{i}", decision="allowed", reason="earlier attempt")
    world.clock.now = now


def test_the_rate_limit_is_re_applied_at_execution(make_world: MakeWorld, tier1_label: FailureLabel) -> None:
    w = make_world(tier1_label)
    approve_all(w, "alice")
    _write_attempts(w, 2, age_s=60)  # two other attempts on the same fingerprint within the hour
    res = run_execute(w)
    assert res.gate is not None and res.gate.decision == "refused" and "rate limit" in res.reason
    assert w.orch.target.executed == []  # type: ignore[attr-defined]


def test_one_other_attempt_is_within_the_limit_and_old_attempts_do_not_count(
    make_world: MakeWorld, tier1_label: FailureLabel
) -> None:
    w = make_world(tier1_label)
    approve_all(w, "alice")
    _write_attempts(w, 1, age_s=60)  # own request-time attempt + this one other = 2 in the hour: allowed
    assert run_execute(w).kind == "executed"
    w2 = make_world(tier1_label)
    approve_all(w2, "alice")
    _write_attempts(w2, 5, age_s=TIER1_TIMEOUT_S * 10)  # 150 minutes old: outside the window
    assert run_execute(w2).kind == "executed"


def test_the_executions_own_gate_rechecks_do_not_count_as_attempts(
    make_world: MakeWorld, tier1_label: FailureLabel
) -> None:
    w = make_world(tier1_label)
    approve_all(w, "alice")
    run_execute(w)
    recheck = [e for e in w.orch.audit.read("gate_decision") if e.payload.get("stage") == "approval_recheck"]
    assert len(recheck) == 1
    plain = [e for e in w.orch.audit.read("gate_decision") if "stage" not in e.payload]
    assert len(plain) == 1  # only the request-time decision is an attempt


# -- terminal states never run ------------------------------------------------------------


def test_a_denied_request_never_executes_even_with_enough_other_approvals(
    make_world: MakeWorld, tier2_label: FailureLabel
) -> None:
    w = make_world(tier2_label, DEAD)
    approve_all(w, "alice")
    w.service.deny(w.request_id, "carol")  # type: ignore[arg-type]
    with pytest.raises(ApprovalError):
        w.service.approve(w.request_id, "bob")  # type: ignore[arg-type]
    res = run_execute(w)
    assert res.kind == "escalated" and w.orch.target.executed == [] and w.table.locks  # type: ignore[attr-defined]
    assert [c[0] for c in w.orch.target.calls] == ["dry_run"]  # type: ignore[attr-defined]  # only the request-time dry run


def test_an_executed_request_cannot_be_executed_again(make_world: MakeWorld, tier1_label: FailureLabel) -> None:
    w = make_world(tier1_label)
    approve_all(w, "alice")
    assert run_execute(w).kind == "executed"
    assert run_execute(w).kind == "escalated"
    assert len(w.orch.target.executed) == 1  # type: ignore[attr-defined]


def test_expiry_escalates_and_is_audited(make_world: MakeWorld, tier1_label: FailureLabel) -> None:
    w = make_world(tier1_label)
    w.clock.advance(TIER1_TIMEOUT_S)
    expired = w.service.expire_check()
    assert [r.id for r in expired] == [w.request_id]
    entries = w.orch.audit.read("expired")
    assert len(entries) == 1 and "escalated to a human" in str(entries[0].payload["reason"])
    with pytest.raises(ApprovalError):
        w.service.approve(w.request_id, "alice")  # type: ignore[arg-type]
    assert run_execute(w).kind == "escalated" and w.orch.target.executed == []  # type: ignore[attr-defined]


def test_approving_an_expired_request_is_refused_and_audited(make_world: MakeWorld, tier1_label: FailureLabel) -> None:
    w = make_world(tier1_label)
    w.clock.advance(TIER1_TIMEOUT_S)
    with pytest.raises(Expired):
        w.service.approve(w.request_id, "alice")  # type: ignore[arg-type]
    assert len(w.orch.audit.read("expired")) == 1


# -- restart ------------------------------------------------------------


def test_a_pending_request_survives_a_restart_and_can_then_be_approved_and_executed(
    make_world: MakeWorld, tier1_label: FailureLabel
) -> None:
    w = make_world(tier1_label)
    rid = w.request_id
    path = w.store.path
    w.store.close()  # the process that opened the request ends
    reopened = ApprovalStore(path, w.clock)
    try:
        assert [r.id for r in reopened.list("pending")] == [rid]
        from driftgate.approval_flow import ApprovalService

        ApprovalService(reopened, w.orch.audit, w.clock).approve(rid, "alice")  # type: ignore[arg-type]
        res = execute_approved(w.orch, reopened, rid, clock=w.clock)  # type: ignore[arg-type]
        assert res.kind == "executed"
    finally:
        reopened.close()


# -- audit ------------------------------------------------------------


def test_an_execution_is_fully_audited(make_world: MakeWorld, tier2_label: FailureLabel) -> None:
    w = make_world(tier2_label, DEAD)
    approve_all(w, "alice", "bob")
    run_execute(w)
    log = [(e.kind, e.payload.get("stage")) for e in w.orch.audit.read()]
    for expected in (
        ("proposal", "holder_proof"),
        ("gate_decision", None),
        ("execution", "dry_run"),
        ("proposal", "approval_requested"),
        ("proposal", "approval_decision"),
        ("gate_decision", "approval_recheck"),
        ("execution", "execute"),
        ("verification", "result"),
        ("verification", "closed"),
    ):
        assert expected in log, expected
    assert log.index(("gate_decision", "approval_recheck")) < log.index(("execution", "execute"))


def test_refused_approvals_are_audited(make_world: MakeWorld, tier2_label: FailureLabel) -> None:
    w = make_world(tier2_label, DEAD)
    rid = w.request_id
    w.service.approve(rid, "alice")  # type: ignore[arg-type]
    with pytest.raises(DuplicateDecision):
        w.service.approve(rid, " ALICE ")  # type: ignore[arg-type]
    with pytest.raises(SelfApproval):
        w.service.approve(rid, TRIGGER)  # type: ignore[arg-type]
    refused = [e.payload for e in w.orch.audit.read("rejected") if e.payload.get("stage") == "approval_refused"]
    assert [r["error"] for r in refused] == ["DuplicateDecision", "SelfApproval"]


def test_refusal_at_execution_is_audited_with_the_gate_reason(make_world: MakeWorld, tier1_label: FailureLabel) -> None:
    w = make_world(tier1_label)
    approve_all(w, "alice")
    w.kill_switch.write_text(json.dumps({"global": True, "tiers": {"1": False}}))
    run_execute(w)
    assert "kill switch" in str([e.payload for e in w.orch.audit.read("rejected")][-1])
    assert any(
        e.payload.get("stage") == "approval_recheck" and e.payload["decision"] == "refused"
        for e in w.orch.audit.read("gate_decision")
    )


def test_clock_is_the_injected_one(make_world: MakeWorld, tier1_label: FailureLabel) -> None:
    w = make_world(tier1_label)
    assert isinstance(w.clock, ManualClock)
    assert {e.ts for e in w.orch.audit.read()} == {w.clock.now}  # nothing read the wall clock
