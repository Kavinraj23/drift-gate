"""Seeded randomised check of the approval path: whatever order things happen in, nothing executes unless every
condition holds at the moment of execution, and the gate still only downgrades."""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from dataclasses import replace

import pytest

from driftgate.approval_flow import execute_approved
from driftgate.approvals import TIER1_TIMEOUT_S, ApprovalError, normalize_identity
from driftgate.eval.ground_truth import FailureLabel
from driftgate.eval.tier12_cases import TRIGGER, Holder, World
from driftgate.gates import RateLimiter

MakeWorld = Callable[..., World]
PEOPLE = ["alice", "Alice ", "BOB", "bob", "carol", TRIGGER, "TRIGGER.USER", "dave"]
RANK = {"allowed": 0, "downgraded": 1, "refused": 2}


def switch_allows(world: World, tier: int) -> bool:
    raw = json.loads(world.kill_switch.read_text(encoding="utf-8"))
    return raw.get("global") is True and raw.get("tiers", {}).get(str(tier)) is True


def holder_dead(world: World) -> bool:
    entry = world.table.locks.get(world.lock_id)
    holder = world.extra.get("holder-ex-1")
    return entry is not None and not entry.alive and holder is not None and holder.status == "failed"


@pytest.mark.parametrize("tier", [1, 2])
@pytest.mark.parametrize("seed", range(12))
def test_nothing_executes_unless_every_condition_holds_at_execution_time(
    seed: int, tier: int, make_world: MakeWorld, tier1_label: FailureLabel, tier2_label: FailureLabel
) -> None:
    rng = random.Random(seed * 10 + tier)
    w = make_world(tier1_label if tier == 1 else tier2_label, Holder() if tier == 2 else None)
    rid = w.request_id
    assert rid is not None
    target = w.orch.target
    executions = 0
    for _ in range(25):
        op = rng.choice(
            ["approve"] * 4 + ["deny", "advance", "kill_off", "kill_on", "live", "dead", "expire", "execute"] * 1
        )
        if op == "approve":
            try:
                w.service.approve(rid, rng.choice(PEOPLE))
            except ApprovalError:
                pass
        elif op == "deny" and rng.random() < 0.3:
            try:
                w.service.deny(rid, rng.choice(PEOPLE))
            except ApprovalError:
                pass
        elif op == "advance":
            w.clock.advance(rng.choice([1, 60, 300, 899, 900, 1800, 3600]))
        elif op == "kill_off":
            off = rng.choice(["global", str(tier), "other"])
            state = {"global": off != "global", "tiers": {str(t): True for t in range(4)}}
            if off == str(tier):
                state["tiers"][str(tier)] = False
            w.kill_switch.write_text(json.dumps(state))
        elif op == "kill_on":
            w.kill_switch.write_text(json.dumps({"global": True, "tiers": {str(t): True for t in range(4)}}))
        elif op == "live" and tier == 2 and w.lock_id in w.table.locks:
            w.table.locks[w.lock_id].alive = True
        elif op == "dead" and tier == 2 and w.lock_id in w.table.locks:
            w.table.locks[w.lock_id].alive = False
        elif op == "expire":
            w.service.expire_check()
        elif op == "execute":
            before = len(target.executed)  # type: ignore[attr-defined]
            req = w.store.get(rid)
            dead_before, switch_before = tier != 2 or holder_dead(w), switch_allows(w, tier)  # execute may unlock
            res = execute_approved(w.orch, w.store, rid, clock=w.clock)
            if len(target.executed) > before:  # type: ignore[attr-defined]
                executions += 1
                assert res.kind in ("executed", "escalated") and res.gate is not None and res.gate.allowed
                decisions = w.store.decisions(rid)
                valid = {
                    d.approver
                    for d in decisions
                    if d.decision == "approve" and d.approver != normalize_identity(TRIGGER)
                }
                assert len(valid) >= req.required, "executed without enough distinct, non-trigger approvals"
                assert not [d for d in decisions if d.decision == "deny"], "executed after a denial"
                assert w.clock.now < req.expires_at, "executed after the approval window closed"
                assert switch_before, "executed while a kill switch was off"
                assert dead_before, "executed while the holder was not provably dead"
                assert w.store.get(rid).status == "executed"
    assert executions <= 1 and len(target.executed) <= 1  # type: ignore[attr-defined]
    if w.store.get(rid).status in ("denied", "expired", "cancelled", "pending"):
        assert target.executed == [] or executions == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize("tier", [1, 2])
def test_the_gate_on_the_approval_path_only_downgrades(
    tier: int, make_world: MakeWorld, tier1_label: FailureLabel, tier2_label: FailureLabel
) -> None:
    w = make_world(tier1_label if tier == 1 else tier2_label, Holder() if tier == 2 else None)
    label = tier1_label if tier == 1 else tier2_label
    rem, facts, _proof, problem = w.orch.gate_inputs(
        label.execution_id, tier, str(label.correct_action), "r", tier == 1, 0.9
    )
    assert facts is not None and not problem
    factory = w.orch.gate_factory
    assert factory is not None
    for incoming in ("allowed", "downgraded", "refused", "bogus"):
        out = factory(RateLimiter(w.clock)).decide(replace(rem, gate_decision=incoming), facts)
        assert RANK[out.decision] >= RANK.get(incoming, RANK["refused"]), (incoming, out.decision)
        assert out.proposed_tier == tier and out.remediation.tier == tier  # the tier never changes
        assert out.effective_tier in (None, tier)


@pytest.mark.parametrize("tier", [1, 2])
def test_a_tier_cannot_be_raised_through_the_payload(
    tier: int, make_world: MakeWorld, tier1_label: FailureLabel, tier2_label: FailureLabel
) -> None:
    """Execution uses the request row's tier and action. A payload that claims another tier changes nothing."""
    import sqlite3

    w = make_world(tier1_label if tier == 1 else tier2_label, Holder() if tier == 2 else None)
    rid = w.request_id
    assert rid is not None
    raw = sqlite3.connect(w.store.path)
    try:
        raw.execute(
            "UPDATE requests SET payload = ? WHERE id = ?", (json.dumps({"tier": 0, "action": "rerun_workflow"}), rid)
        )
        raw.commit()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            raw.execute("UPDATE requests SET tier = 0 WHERE id = ?", (rid,))
    finally:
        raw.close()
    w.service.approve(rid, "alice")
    if tier == 2:
        w.service.approve(rid, "bob")
    res = execute_approved(w.orch, w.store, rid, clock=w.clock)
    assert res.gate is not None and res.gate.proposed_tier == tier
    assert all(a.tier == tier for a in w.orch.target.executed)  # type: ignore[attr-defined]


def test_tier_boundary_constant() -> None:
    assert TIER1_TIMEOUT_S == 900
