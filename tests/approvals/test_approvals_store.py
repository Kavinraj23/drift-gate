"""ApprovalStore rules: timeout, identities, self-approval, quorum, persistence, forward-only transitions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from driftgate.approvals import (
    TIER1_TIMEOUT_S,
    TIER2_TIMEOUT_S,
    ApprovalError,
    ApprovalStore,
    DuplicateDecision,
    DuplicateRequest,
    Expired,
    IdentityError,
    NotOpen,
    SelfApproval,
    UnknownRequest,
    normalize_identity,
)
from driftgate.eval.tier12_cases import ManualClock


def new_request(store: ApprovalStore, tier: int = 1, by: str = "trigger", eid: str = "ex-1", action: str = "a") -> int:
    return store.create(
        execution_id=eid, fingerprint="fp", tier=tier, action=action, requested_by=by, payload={"k": "v"}
    ).id


# -- identities ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_empty_identity_is_refused(store: ApprovalStore, raw: str) -> None:
    rid = new_request(store)
    with pytest.raises(IdentityError):
        store.approve(rid, raw)
    with pytest.raises(IdentityError):
        store.deny(rid, raw)
    with pytest.raises(IdentityError):
        new_request(store, by=raw, eid="ex-2")
    assert store.get(rid).status == "pending" and store.decisions(rid) == []


def test_identities_are_normalised() -> None:
    assert normalize_identity("  Alice   Smith ") == normalize_identity("alice smith") == "alice smith"
    assert normalize_identity("ALICE") != normalize_identity("bob")


# -- Tier 1: one approval, 15 minutes ------------------------------------------------------------------------------


def test_tier1_needs_exactly_one_approval(store: ApprovalStore) -> None:
    rid = new_request(store)
    assert store.get(rid).required == 1
    assert store.approve(rid, "alice").status == "approved"


def test_tier1_timeout_is_fifteen_minutes() -> None:
    assert TIER1_TIMEOUT_S == 15 * 60


def test_approval_just_before_the_deadline_is_accepted(store: ApprovalStore, clock: ManualClock) -> None:
    rid = new_request(store)
    clock.advance(TIER1_TIMEOUT_S - 0.001)
    assert store.approve(rid, "alice").status == "approved"


def test_approval_at_exactly_fifteen_minutes_is_refused_and_the_request_expires(
    store: ApprovalStore, clock: ManualClock
) -> None:
    rid = new_request(store)
    clock.advance(TIER1_TIMEOUT_S)
    with pytest.raises(Expired):
        store.approve(rid, "alice")
    assert store.get(rid).status == "expired" and store.decisions(rid) == []


def test_approval_after_the_deadline_is_refused(store: ApprovalStore, clock: ManualClock) -> None:
    rid = new_request(store)
    clock.advance(TIER1_TIMEOUT_S + 1)
    with pytest.raises(Expired):
        store.approve(rid, "alice")


def test_an_expired_request_can_never_be_approved_even_with_the_clock_rewound(
    store: ApprovalStore, clock: ManualClock
) -> None:
    rid = new_request(store)
    clock.advance(TIER1_TIMEOUT_S)
    assert [r.id for r in store.expire_due()] == [rid]
    clock.now -= 3600  # a confused clock must not reopen it
    with pytest.raises(NotOpen):
        store.approve(rid, "alice")
    with pytest.raises(NotOpen):
        store.claim_for_execution(rid)
    assert store.get(rid).status == "expired"


def test_expire_due_only_expires_overdue_open_requests(store: ApprovalStore, clock: ManualClock) -> None:
    old = new_request(store, eid="ex-old")
    clock.advance(TIER1_TIMEOUT_S - 1)
    young = new_request(store, eid="ex-young")
    clock.advance(1)
    assert [r.id for r in store.expire_due()] == [old]
    assert store.get(young).status == "pending" and store.expire_due() == []


def test_an_approved_request_also_expires_if_not_executed_in_time(store: ApprovalStore, clock: ManualClock) -> None:
    rid = new_request(store)
    store.approve(rid, "alice")
    clock.advance(TIER1_TIMEOUT_S)
    with pytest.raises(Expired):
        store.claim_for_execution(rid)
    assert store.get(rid).status == "expired"


# -- Tier 2: two different people ----------------------------------------------------------------------------------


def test_tier2_needs_two_different_people(store: ApprovalStore) -> None:
    rid = new_request(store, tier=2, action="force_unlock_state")
    assert store.get(rid).required == 2
    assert store.approve(rid, "alice").status == "pending"
    assert store.approve(rid, "bob").status == "approved"


def test_one_tier2_approval_is_not_enough_to_claim(store: ApprovalStore) -> None:
    rid = new_request(store, tier=2, action="force_unlock_state")
    store.approve(rid, "alice")
    with pytest.raises(NotOpen):
        store.claim_for_execution(rid)
    assert store.get(rid).status == "pending"


def test_tier2_timeout_is_bounded(store: ApprovalStore, clock: ManualClock) -> None:
    rid = new_request(store, tier=2, action="force_unlock_state")
    clock.advance(TIER2_TIMEOUT_S)
    with pytest.raises(Expired):
        store.approve(rid, "alice")


@pytest.mark.parametrize("second", ["alice", "ALICE", "  Alice  ", "aLiCe"])
def test_the_same_person_cannot_approve_twice_under_any_spelling(store: ApprovalStore, second: str) -> None:
    rid = new_request(store, tier=2, action="force_unlock_state")
    store.approve(rid, "alice")
    with pytest.raises(DuplicateDecision):
        store.approve(rid, second)
    assert store.approvers(rid) == ["alice"] and store.get(rid).status == "pending"


def test_a_person_cannot_approve_after_denying_or_deny_after_approving(store: ApprovalStore) -> None:
    rid = new_request(store, tier=2, action="force_unlock_state")
    store.approve(rid, "alice")
    with pytest.raises(DuplicateDecision):
        store.deny(rid, "Alice")


def test_unique_constraint_blocks_a_second_decision_inserted_directly(store: ApprovalStore, tmp_path: Path) -> None:
    rid = new_request(store, tier=2, action="force_unlock_state")
    store.approve(rid, "alice")
    raw = sqlite3.connect(store.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            raw.execute(
                "INSERT INTO decisions (request_id, approver, decision, at) VALUES (?, 'alice', 'approve', 0)", (rid,)
            )
    finally:
        raw.close()
    assert store.approvers(rid) == ["alice"]


# -- self-approval -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tier,action", [(1, "recycle_runner"), (2, "force_unlock_state")])
@pytest.mark.parametrize("spelling", ["trigger", "TRIGGER", "  Trigger "])
def test_the_person_who_triggered_the_run_cannot_approve_it(
    store: ApprovalStore, tier: int, action: str, spelling: str
) -> None:
    rid = new_request(store, tier=tier, action=action, by="trigger")
    with pytest.raises(SelfApproval):
        store.approve(rid, spelling)
    assert store.approvers(rid) == [] and store.get(rid).status == "pending"


def test_self_approval_cannot_be_inserted_directly(store: ApprovalStore) -> None:
    rid = new_request(store, by="trigger")
    raw = sqlite3.connect(store.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="triggered the run"):
            raw.execute(
                "INSERT INTO decisions (request_id, approver, decision, at) VALUES (?, 'trigger', 'approve', 0)",
                (rid,),
            )
    finally:
        raw.close()


def test_a_trigger_plus_one_other_is_still_one_valid_tier2_approval(store: ApprovalStore) -> None:
    rid = new_request(store, tier=2, action="force_unlock_state", by="trigger")
    store.approve(rid, "alice")
    with pytest.raises(SelfApproval):
        store.approve(rid, "trigger")
    assert store.get(rid).status == "pending"


# -- deny, forward-only transitions --------------------------------------------------------------------------------


def test_a_denial_ends_the_request_and_it_can_never_be_approved_later(store: ApprovalStore) -> None:
    rid = new_request(store, tier=2, action="force_unlock_state")
    store.approve(rid, "alice")
    assert store.deny(rid, "bob").status == "denied"
    with pytest.raises(NotOpen):
        store.approve(rid, "carol")
    with pytest.raises(NotOpen):
        store.claim_for_execution(rid)


def test_status_moves_only_forward_even_for_direct_writes(store: ApprovalStore) -> None:
    rid = new_request(store)
    store.deny(rid, "bob")
    raw = sqlite3.connect(store.path)
    try:
        for target in ("pending", "approved", "executed"):
            with pytest.raises(sqlite3.IntegrityError, match="illegal status transition"):
                raw.execute("UPDATE requests SET status = ? WHERE id = ?", (target, rid))
    finally:
        raw.close()
    assert store.get(rid).status == "denied"


def test_decisions_are_append_only_and_only_while_pending(store: ApprovalStore) -> None:
    rid = new_request(store, tier=2, action="force_unlock_state")
    store.approve(rid, "alice")
    other = new_request(store, eid="ex-other")
    store.deny(other, "bob")
    raw = sqlite3.connect(store.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM decisions")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("UPDATE decisions SET decision = 'approve'")
        with pytest.raises(sqlite3.IntegrityError, match="not pending"):
            raw.execute(
                "INSERT INTO decisions (request_id, approver, decision, at) VALUES (?, 'carol', 'approve', 0)", (other,)
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            raw.execute("UPDATE requests SET tier = 1 WHERE id = ?", (rid,))
    finally:
        raw.close()


def test_claim_is_atomic_a_request_executes_once(store: ApprovalStore) -> None:
    rid = new_request(store)
    store.approve(rid, "alice")
    assert store.claim_for_execution(rid).status == "executed"
    with pytest.raises(NotOpen):
        store.claim_for_execution(rid)


def test_only_tier1_and_tier2_requests_exist(store: ApprovalStore) -> None:
    for tier in (0, 3, 4):
        with pytest.raises(ApprovalError):
            new_request(store, tier=tier)


def test_one_open_request_per_execution_and_action(store: ApprovalStore) -> None:
    new_request(store)
    with pytest.raises(DuplicateRequest):
        new_request(store)
    new_request(store, action="other")  # a different action is a different request


def test_unknown_request(store: ApprovalStore) -> None:
    with pytest.raises(UnknownRequest):
        store.approve(999, "alice")


# -- persistence ---------------------------------------------------------------------------------------------------


def test_pending_approvals_and_decisions_survive_a_restart(tmp_path: Path, clock: ManualClock) -> None:
    path = tmp_path / "state" / "approvals.sqlite"
    first = ApprovalStore(path, clock)
    rid = new_request(first, tier=2, action="force_unlock_state")
    first.approve(rid, "alice")
    first.close()

    clock.advance(60)
    second = ApprovalStore(path, clock)  # a new process: nothing in memory
    try:
        req = second.get(rid)
        assert req.status == "pending" and req.tier == 2 and req.requested_by == "trigger"
        assert req.payload == {"k": "v"} and second.approvers(rid) == ["alice"]
        assert [r.id for r in second.list("pending")] == [rid]
        # the quorum and the duplicate rule still hold after the restart
        with pytest.raises(DuplicateDecision):
            second.approve(rid, "ALICE")
        assert second.approve(rid, "bob").status == "approved"
    finally:
        second.close()

    third = ApprovalStore(path, clock)
    try:
        assert third.get(rid).status == "approved" and third.approvers(rid) == ["alice", "bob"]
    finally:
        third.close()


def test_expiry_is_judged_by_the_injected_clock_after_a_restart(tmp_path: Path, clock: ManualClock) -> None:
    path = tmp_path / "approvals.sqlite"
    s = ApprovalStore(path, clock)
    rid = new_request(s)
    s.close()
    clock.advance(TIER1_TIMEOUT_S)
    s = ApprovalStore(path, clock)
    try:
        assert [r.id for r in s.expire_due()] == [rid]
    finally:
        s.close()
