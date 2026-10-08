"""SafetyGate tests: kill switch, abort ceiling, rate limit, Tier 0/2 eligibility, downgrade-only."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from driftgate.domain import BlastRadius, Remediation
from driftgate.gates import (
    TIER_GATE,
    GateFacts,
    KillSwitch,
    RateLimiter,
    SafetyGate,
    Step,
    additive_before_subtractive_violations,
    order_additive_first,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def write_switch(path: Path, glob: bool = True, tiers: dict[str, bool] | None = None) -> None:
    path.write_text(json.dumps({"global": glob, "tiers": tiers or {str(t): True for t in range(4)}}))


@pytest.fixture
def env(tmp_path: Path) -> tuple[SafetyGate, FakeClock, Path]:
    clock = FakeClock()
    sw = tmp_path / "kill_switch.json"
    write_switch(sw)
    return SafetyGate(KillSwitch(sw), RateLimiter(clock)), clock, sw


def prop(tier: int = 0, **kw: object) -> Remediation:
    base = dict(
        tier=tier, action="rerun", rationale="r", reversible=True, gate=TIER_GATE[tier], gate_decision="allowed"
    )
    base.update(kw)
    return Remediation(**base)  # type: ignore[arg-type]


def facts(**kw: object) -> GateFacts:
    base: dict[str, object] = dict(
        fingerprint="fp1", signature_match=True, flake_precedent=True, proposed_set_size=1, resource_set_total=5
    )
    base.update(kw)
    return GateFacts(**base)  # type: ignore[arg-type]


def test_default_kill_switch_file_has_everything_enabled() -> None:
    state = KillSwitch().read()
    assert state.global_enabled and all(state.tiers[t] for t in range(4))


def test_tier0_flake_precedent_path_allowed(env) -> None:
    gate, _, _ = env
    d = gate.decide(prop(0), facts())
    assert d.allowed and d.remediation.gate_decision == "allowed"


def test_tier0_known_transient_path_allowed(env) -> None:
    gate, _, _ = env
    assert gate.decide(prop(0), facts(flake_precedent=False, known_transient_rule=True)).allowed


def test_tier0_ineligible_cases_downgraded(env) -> None:
    gate, _, _ = env
    assert gate.decide(prop(0), facts(signature_match=False)).decision == "downgraded"
    assert gate.decide(prop(0), facts(flake_precedent=False)).decision == "downgraded"
    # signature match alone is not enough, and neither is precedent without a signature match
    assert gate.decide(prop(0), facts(signature_match=False, known_transient_rule=True)).decision == "downgraded"


def test_agent_confidence_never_sufficient(env) -> None:
    gate, _, _ = env
    d = gate.decide(prop(0), facts(signature_match=False, flake_precedent=False, agent_confidence=1.0))
    assert not d.allowed and d.agent_confidence == 1.0
    d2 = gate.decide(prop(2, action="force_unlock"), GateFacts(fingerprint="x", agent_confidence=1.0))
    assert not d2.allowed


def test_second_failure_after_tier0_retry_hard_escalates(env) -> None:
    gate, _, _ = env
    d = gate.decide(prop(0), facts(prior_tier0_failed=True))
    assert d.decision == "refused"


def test_gate_mismatch_downgraded(env) -> None:
    gate, _, _ = env
    assert gate.decide(prop(0, gate="pull_request"), facts()).decision == "downgraded"


def test_kill_switch_global_and_per_tier_reread_every_decision(env) -> None:
    gate, clock, sw = env
    f = lambda: facts(fingerprint=f"fp{clock.now}")  # noqa: E731
    assert gate.decide(prop(0), f()).allowed
    write_switch(sw, glob=False)
    d = gate.decide(prop(0), f())
    assert d.decision == "refused" and "global" in d.reason
    write_switch(sw, tiers={"0": False, "1": True, "2": True, "3": True})
    clock.now += 1
    assert gate.decide(prop(0), f()).decision == "refused"
    assert gate.decide(prop(3), f()).allowed  # other tiers unaffected
    write_switch(sw)
    clock.now += 1
    assert gate.decide(prop(0), f()).allowed


def test_kill_switch_missing_or_corrupt_fails_closed(env) -> None:
    gate, _, sw = env
    sw.write_text("{not json")
    assert gate.decide(prop(0), facts()).decision == "refused"
    sw.unlink()
    assert gate.decide(prop(0), facts()).decision == "refused"


def test_abort_ceiling_blast_radius(env) -> None:
    gate, _, _ = env
    over = facts(blast_radius=BlastRadius(executions_affected=11, shared_dimension="runner_pool"))
    assert gate.decide(prop(0), over).decision == "downgraded"
    at = facts(blast_radius=BlastRadius(executions_affected=10))
    assert gate.decide(prop(0), at).allowed


def test_abort_ceiling_multiple_shared_resources(env) -> None:
    gate, _, _ = env
    assert gate.decide(prop(3), facts(shared_resources=("pool-a", "pool-b"))).decision == "downgraded"
    assert gate.decide(prop(3), facts(shared_resources=("pool-a",))).allowed


def test_refuses_empty_set_and_full_set(env) -> None:
    gate, _, _ = env
    assert gate.decide(prop(0), facts(proposed_set_size=0, resource_set_total=5)).decision == "refused"
    assert gate.decide(prop(0), facts(proposed_set_size=5, resource_set_total=5)).decision == "refused"
    assert gate.decide(prop(0), facts(proposed_set_size=4, resource_set_total=5)).allowed


def test_rate_limit_two_per_hour_with_fake_clock(env) -> None:
    gate, clock, _ = env
    assert gate.decide(prop(0), facts()).allowed
    clock.now += 600
    assert gate.decide(prop(0), facts()).allowed
    clock.now += 600
    d = gate.decide(prop(0), facts())
    assert d.decision == "refused" and "rate limit" in d.reason
    assert gate.decide(prop(0), facts(fingerprint="other")).allowed  # per fingerprint
    clock.now += 2400  # first attempt (t=1000) is now 3600s old
    assert gate.decide(prop(0), facts()).allowed


def test_refused_decisions_do_not_consume_rate_limit(env) -> None:
    gate, _, _ = env
    for _ in range(5):
        assert not gate.decide(prop(0), facts(signature_match=False)).allowed
    assert gate.decide(prop(0), facts()).allowed


def test_tier2_requires_dead_holder_proof_and_dry_run(env) -> None:
    gate, _, _ = env
    p = prop(2, action="force_unlock", reversible=False)
    ok = dict(
        lock_holder="dead",
        holder_death_proof=True,
        dry_run_ok=True,
        fingerprint="l",
        proposed_set_size=1,
        resource_set_total=5,
    )
    assert gate.decide(p, GateFacts(**ok)).allowed  # type: ignore[arg-type]
    assert gate.decide(p, GateFacts(**{**ok, "lock_holder": "running"})).decision == "refused"  # type: ignore[arg-type]
    assert gate.decide(p, GateFacts(**{**ok, "lock_holder": "unknown"})).decision == "downgraded"  # type: ignore[arg-type]
    assert gate.decide(p, GateFacts(**{**ok, "holder_death_proof": False})).decision == "downgraded"  # type: ignore[arg-type]
    assert gate.decide(p, GateFacts(**{**ok, "dry_run_ok": False})).decision == "downgraded"  # type: ignore[arg-type]


def test_tier2_running_holder_refused_even_with_everything_else_true(env) -> None:
    gate, _, _ = env
    f = GateFacts(
        fingerprint="l",
        signature_match=True,
        flake_precedent=True,
        known_transient_rule=True,
        holder_death_proof=True,
        dry_run_ok=True,
        lock_holder="running",
        proposed_set_size=1,
        resource_set_total=5,
        agent_confidence=0.99,
    )
    assert gate.decide(prop(2, action="force_unlock"), f).decision == "refused"


def test_downgrade_only_property(tmp_path: Path) -> None:
    """For random proposals and facts the gate never raises a tier or rewrites anything but gate_decision."""
    rng = random.Random(1234)
    sw = tmp_path / "ks.json"
    for _ in range(600):
        write_switch(sw, glob=rng.random() > 0.1, tiers={str(t): rng.random() > 0.1 for t in range(4)})
        clock = FakeClock()
        gate = SafetyGate(KillSwitch(sw), RateLimiter(clock))
        tier = rng.choice([0, 1, 2, 3])
        incoming = rng.choice(list(RANK))
        p = prop(tier, gate=rng.choice([TIER_GATE[tier], "auto", "pull_request"]), gate_decision=incoming)
        total = rng.choice([None, 0, 1, 5])
        f = GateFacts(
            fingerprint=rng.choice(["a", "b"]),
            signature_match=rng.random() > 0.5,
            flake_precedent=rng.random() > 0.5,
            known_transient_rule=rng.random() > 0.5,
            blast_radius=BlastRadius(rng.randint(0, 30)),
            shared_resources=tuple(rng.sample(["x", "y", "z"], rng.randint(0, 3))),
            resource_set_total=total,
            proposed_set_size=None if total is None else rng.randint(0, 6),
            prior_tier0_failed=rng.random() > 0.8,
            lock_holder=rng.choice([None, "dead", "running", "unknown"]),
            holder_death_proof=rng.random() > 0.5,
            dry_run_ok=rng.random() > 0.5,
            agent_confidence=rng.random(),
        )
        for _ in range(3):
            d = gate.decide(p, f)
            assert d.decision in ("allowed", "downgraded", "refused")
            assert d.remediation.tier == p.tier
            assert d.proposed_tier == p.tier
            assert d.effective_tier is None or d.effective_tier <= p.tier
            assert d.remediation.gate_decision == d.decision
            assert RANK[d.decision] >= RANK[incoming]  # never weaker than the incoming decision
            if incoming != "allowed":
                assert not d.allowed and d.effective_tier is None
            state = KillSwitch(sw).read()
            if state.blocks(tier) or (tier != 3 and (f.proposed_set_size is None or f.resource_set_total is None)):
                assert not d.allowed
            if f.blast_radius.executions_affected > 10 or len(set(f.shared_resources)) > 1:
                assert not d.allowed
            assert d.remediation.action == p.action and d.remediation.gate == p.gate
            if d.allowed and tier == 0:
                assert f.signature_match and (f.flake_precedent or f.known_transient_rule)
            if d.allowed and tier == 2:
                assert f.lock_holder == "dead" and f.holder_death_proof


RANK = {"allowed": 0, "downgraded": 1, "refused": 2}


@pytest.mark.parametrize("incoming", ["downgraded", "refused"])
def test_incoming_non_allowed_decision_is_never_upgraded(env, incoming: str) -> None:
    gate, _, _ = env
    d = gate.decide(prop(0, gate_decision=incoming), facts())
    assert d.decision == incoming and not d.allowed and d.effective_tier is None
    # a stronger outcome may replace a weaker one
    assert gate.decide(prop(0, gate_decision="downgraded"), facts(prior_tier0_failed=True)).decision == "refused"
    # and a non-allowed proposal consumes no rate-limit budget
    assert gate.decide(prop(0), facts()).allowed and gate.decide(prop(0), facts()).allowed


def test_unknown_incoming_decision_fails_closed(env) -> None:
    gate, _, _ = env
    assert gate.decide(prop(0, gate_decision="bogus"), facts()).decision == "refused"


@pytest.mark.parametrize("tier", [0, 1, 2])
def test_missing_set_facts_fail_closed_for_tiers_0_to_2(env, tier: int) -> None:
    gate, _, _ = env
    base = dict(lock_holder="dead", holder_death_proof=True, dry_run_ok=True)
    assert gate.decide(prop(tier), facts(proposed_set_size=None, **base)).decision == "downgraded"
    assert gate.decide(prop(tier), facts(resource_set_total=None, **base)).decision == "downgraded"
    assert gate.decide(prop(tier), facts(**base)).allowed


def test_tier3_missing_set_facts_still_proceeds_to_pr(env) -> None:
    gate, _, _ = env
    assert gate.decide(prop(3), facts(proposed_set_size=None, resource_set_total=None)).allowed


@pytest.mark.parametrize("tier", [0, 1, 2, 3])
def test_rate_limit_two_per_hour_every_tier(env, tier: int) -> None:
    gate, clock, _ = env
    ok = dict(lock_holder="dead", holder_death_proof=True, dry_run_ok=True)
    assert gate.decide(prop(tier), facts(**ok)).allowed
    clock.now += 600
    assert gate.decide(prop(tier), facts(**ok)).allowed
    clock.now += 600
    d = gate.decide(prop(tier), facts(**ok))
    assert d.decision == "refused" and "rate limit" in d.reason
    assert gate.decide(prop(tier), facts(fingerprint="other", **ok)).allowed
    clock.now += 2400  # first attempt is now exactly one hour old
    assert gate.decide(prop(tier), facts(**ok)).allowed
    assert gate.decide(prop(tier), facts(**ok)).decision == "refused"  # new window already holds 2


def test_additive_before_subtractive() -> None:
    bad = [Step("delete-old", "subtractive"), Step("create-new", "additive")]
    assert additive_before_subtractive_violations(bad) == ["create-new"]
    fixed = order_additive_first(bad)
    assert [s.name for s in fixed] == ["create-new", "delete-old"]
    assert additive_before_subtractive_violations(fixed) == []
    with pytest.raises(ValueError):
        additive_before_subtractive_violations([Step("x", "weird")])
