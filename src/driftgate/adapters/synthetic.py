"""SyntheticTarget: a RemediationTarget with injectable simulated outcomes and a simulated lock table.

SyntheticSource (the simulated CI provider fed by the seeded generator) arrives with M1/M3.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from driftgate.domain import DryRunResult, ExecutionResult, Remediation, VerificationResult


@dataclass
class LockEntry:
    holder: str
    alive: bool


@dataclass
class LockTable:
    """Simulated state-lock table with holder liveness."""

    locks: dict[str, LockEntry] = field(default_factory=dict)

    def holder_status(self, lock_id: str) -> str:
        entry = self.locks.get(lock_id)
        if entry is None:
            return "none"
        return "running" if entry.alive else "dead"


@dataclass
class SimulatedOutcome:
    dry_run_ok: bool = True
    execute_ok: bool = True
    verified: bool = True


class SyntheticTarget:
    """Outcomes are keyed by action name; unlisted actions use `default`. Records every call.

    Tier 2 `force_unlock` reads `dry_run["lock_id"]` and acts on the simulated lock table, and
    independently refuses a running holder (defense in depth behind SafetyGate).
    """

    def __init__(
        self,
        outcomes: dict[str, SimulatedOutcome] | None = None,
        default: SimulatedOutcome | None = None,
        lock_table: LockTable | None = None,
    ) -> None:
        self.outcomes = outcomes or {}
        self.default = default or SimulatedOutcome()
        self.locks = lock_table or LockTable()
        self.calls: list[tuple[str, str]] = []  # (method, action)
        self.executed: list[Remediation] = []

    def _outcome(self, action: Remediation) -> SimulatedOutcome:
        return self.outcomes.get(action.action, self.default)

    def _lock_id(self, action: Remediation) -> str | None:
        return action.dry_run.get("lock_id") if action.action == "force_unlock" else None

    def dry_run(self, action: Remediation) -> DryRunResult:
        self.calls.append(("dry_run", action.action))
        lock_id = self._lock_id(action)
        if lock_id is not None:
            status = self.locks.holder_status(lock_id)
            entry = self.locks.locks.get(lock_id)
            details = {"lock_id": lock_id, "holder_status": status, "holder": entry.holder if entry else None}
            ok = status == "dead" and self._outcome(action).dry_run_ok
            return DryRunResult(ok, f"holder {status}", details)
        out = self._outcome(action)
        return DryRunResult(out.dry_run_ok, f"simulated dry run of {action.action}", {"tier": action.tier})

    def execute(self, action: Remediation) -> ExecutionResult:
        self.calls.append(("execute", action.action))
        lock_id = self._lock_id(action)
        if lock_id is not None:
            if self.locks.holder_status(lock_id) != "dead":
                return ExecutionResult(False, "refused: lock holder is not provably dead", {"lock_id": lock_id})
            if self._outcome(action).execute_ok:
                del self.locks.locks[lock_id]
                self.executed.append(action)
                return ExecutionResult(True, "lock released", {"lock_id": lock_id})
            return ExecutionResult(False, "simulated failure", {"lock_id": lock_id})
        out = self._outcome(action)
        if out.execute_ok:
            self.executed.append(action)
        return ExecutionResult(out.execute_ok, f"simulated execution of {action.action}", {"tier": action.tier})

    def verify(self, action: Remediation) -> VerificationResult:
        self.calls.append(("verify", action.action))
        lock_id = self._lock_id(action)
        if lock_id is not None:
            gone = lock_id not in self.locks.locks
            return VerificationResult(gone and self._outcome(action).verified, "lock table checked")
        out = self._outcome(action)
        return VerificationResult(out.verified, f"simulated verification of {action.action}")
