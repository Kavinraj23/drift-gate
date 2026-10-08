"""SafetyGate, kill switch, abort ceiling and per-fingerprint rate limit; can only downgrade a proposal."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from driftgate.domain import BlastRadius, Remediation

TIER_GATE = {0: "auto", 1: "single_approval", 2: "dual_approval", 3: "pull_request"}
DEFAULT_KILL_SWITCH_PATH = Path(__file__).resolve().parents[2] / "config" / "kill_switch.json"
RATE_LIMIT_MAX = 2
RATE_LIMIT_WINDOW_S = 3600.0
DEFAULT_ABORT_BLAST_CEILING = 10

Clock = Callable[[], float]
_RANK = {"allowed": 0, "downgraded": 1, "refused": 2}


@dataclass
class GateFacts:
    """Deterministic facts the proposal depends on. Agent confidence is recorded, never consulted."""

    fingerprint: str
    signature_match: bool = False
    flake_precedent: bool = False
    known_transient_rule: bool = False
    blast_radius: BlastRadius = field(default_factory=BlastRadius)
    shared_resources: tuple[str, ...] = ()
    resource_set_total: int | None = None  # size of the whole resource set the action draws from
    proposed_set_size: int | None = None  # how many of them the action would touch
    prior_tier0_failed: bool = False  # a Tier 0 retry already failed for this fingerprint
    lock_holder: str | None = None  # None, "dead", "running" or "unknown"
    holder_death_proof: bool = False
    dry_run_ok: bool = False
    agent_confidence: float | None = None


@dataclass(frozen=True)
class GateDecision:
    decision: str  # allowed | downgraded | refused
    reason: str
    remediation: Remediation  # same tier as proposed, with gate_decision applied
    proposed_tier: int
    agent_confidence: float | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == "allowed"

    @property
    def effective_tier(self) -> int | None:
        """The tier that may proceed; None means escalate to a human. Never above proposed_tier."""
        return self.proposed_tier if self.allowed else None


@dataclass(frozen=True)
class KillSwitchState:
    global_enabled: bool
    tiers: dict[int, bool]

    def blocks(self, tier: int) -> str | None:
        if not self.global_enabled:
            return "global kill switch is off"
        if not self.tiers.get(tier, False):
            return f"tier {tier} kill switch is off"
        return None


class KillSwitch:
    """Reads a JSON file on every call; an unreadable or malformed file fails closed."""

    def __init__(self, path: Path | str = DEFAULT_KILL_SWITCH_PATH) -> None:
        self.path = Path(path)

    def read(self) -> KillSwitchState:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            tiers = {int(k): v is True for k, v in raw["tiers"].items()}
            return KillSwitchState(raw["global"] is True, tiers)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return KillSwitchState(False, {})


class RateLimiter:
    """At most `limit` allowed attempts per fingerprint in a sliding window, on an injected clock."""

    def __init__(self, clock: Clock, limit: int = RATE_LIMIT_MAX, window_s: float = RATE_LIMIT_WINDOW_S) -> None:
        self._clock = clock
        self._limit = limit
        self._window = window_s
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, fingerprint: str) -> deque[float]:
        q = self._attempts[fingerprint]
        cutoff = self._clock() - self._window
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def exhausted(self, fingerprint: str) -> bool:
        return len(self._prune(fingerprint)) >= self._limit

    def record(self, fingerprint: str) -> None:
        self._prune(fingerprint).append(self._clock())


class SafetyGate:
    def __init__(
        self,
        kill_switch: KillSwitch,
        rate_limiter: RateLimiter,
        abort_blast_ceiling: int = DEFAULT_ABORT_BLAST_CEILING,
    ) -> None:
        self._kill = kill_switch
        self._rate = rate_limiter
        self._ceiling = abort_blast_ceiling

    def decide(self, proposal: Remediation, facts: GateFacts) -> GateDecision:
        """Return the proposal with a gate decision. Only ever allowed, downgraded or refused."""
        incoming = proposal.gate_decision
        decision, reason = self._evaluate(proposal, facts)
        # Downgrade-only by construction: an outcome may only be replaced by a stronger one
        # (allowed < downgraded < refused). An unknown incoming value fails closed.
        in_rank = _RANK.get(incoming, _RANK["refused"])
        if in_rank > _RANK[decision]:
            decision = incoming if incoming in _RANK else "refused"
            reason = f"incoming gate decision {incoming!r} is not allowed and is never upgraded"
        if decision == "allowed":
            self._rate.record(facts.fingerprint)
        out = replace(proposal, gate_decision=decision)
        return GateDecision(decision, reason, out, proposal.tier, facts.agent_confidence)

    def _evaluate(self, p: Remediation, f: GateFacts) -> tuple[str, str]:
        if p.tier not in TIER_GATE:
            return "refused", f"unknown tier {p.tier}"
        # Kill switch is re-read on every decision (invariant 5).
        blocked = self._kill.read().blocks(p.tier)
        if blocked:
            return "refused", blocked
        if p.gate != TIER_GATE[p.tier]:
            return "downgraded", f"gate {p.gate!r} does not match tier {p.tier} ({TIER_GATE[p.tier]!r})"

        # Abort ceiling. Missing set facts fail closed for tiers 0-2; Tier 3's gate is the PR plus reviewer.
        if p.tier != 3 and (f.proposed_set_size is None or f.resource_set_total is None):
            return "downgraded", "proposed/total resource set size unknown: abort ceiling cannot be checked"
        if f.proposed_set_size is not None and f.proposed_set_size <= 0:
            return "refused", "empty proposed set"
        if f.proposed_set_size is not None and f.resource_set_total is not None:
            if f.proposed_set_size >= f.resource_set_total:
                return "refused", "action would touch 100% of the resource set"
        if f.blast_radius.executions_affected > self._ceiling:
            return "downgraded", (
                f"blast radius {f.blast_radius.executions_affected} exceeds ceiling {self._ceiling}: fleet-wide"
            )
        if len(set(f.shared_resources)) > 1:
            return "downgraded", "remediation touches more than one shared resource"

        # Rate limit: hard escalate (invariant 6).
        if self._rate.exhausted(f.fingerprint):
            return "refused", f"rate limit: {RATE_LIMIT_MAX} attempts per fingerprint per hour reached"

        if p.tier == 0:
            return self._tier0(f)
        if p.tier == 2:
            return self._tier2(f)
        return "allowed", f"tier {p.tier} proceeds to its gate ({p.gate})"

    @staticmethod
    def _tier0(f: GateFacts) -> tuple[str, str]:
        if f.prior_tier0_failed:
            return "refused", "second failure after a Tier 0 retry: hard escalate"
        if not f.signature_match:
            return "downgraded", "no deterministic signature match"
        if not (f.flake_precedent or f.known_transient_rule):
            return "downgraded", "no flake precedent and no Tier 0 known-transient rule"
        path = "flake precedent" if f.flake_precedent else "known-transient rule"
        return "allowed", f"deterministic signature match and {path}"

    @staticmethod
    def _tier2(f: GateFacts) -> tuple[str, str]:
        if f.lock_holder == "running":
            return "refused", "lock holder is running"
        if f.lock_holder != "dead" or not f.holder_death_proof:
            return "downgraded", "holder death not proven deterministically"
        if not f.dry_run_ok:
            return "downgraded", "mandatory dry-run missing or failed"
        return "allowed", "holder provably dead; proceeds to dual approval"


@dataclass(frozen=True)
class Step:
    name: str
    kind: str  # "additive" | "subtractive"


def additive_before_subtractive_violations(steps: Sequence[Step]) -> list[str]:
    """Names of additive steps that appear after a subtractive one (invariant 13)."""
    seen_subtractive = False
    bad: list[str] = []
    for s in steps:
        if s.kind not in ("additive", "subtractive"):
            raise ValueError(f"unknown step kind {s.kind!r}")
        if s.kind == "subtractive":
            seen_subtractive = True
        elif seen_subtractive:
            bad.append(s.name)
    return bad


def order_additive_first(steps: Sequence[Step]) -> list[Step]:
    """Stable reorder: all additive steps, then all subtractive ones."""
    return [s for s in steps if s.kind == "additive"] + [s for s in steps if s.kind == "subtractive"]
