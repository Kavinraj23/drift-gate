"""Deterministic holder-death proof for the Tier 2 state-lock force-unlock (PRD N4).

Everything here is derived from the failed step's log and the (simulated) lock table plus the execution source;
none of it comes from the model. The proof holds only when ALL of these are true:

- the failed log names exactly one lock id;
- the lock table has an entry for that id, and the entry names its holder as an execution id;
- that execution exists in the source and its status is terminal (the holder's execution is over);
- the table does not mark the holder alive.

If any source says the holder is still running the status is `running` (a hard refusal in the gate). Anything
missing, unknown or contradictory is `unknown` (a downgrade to escalation). The proof is re-derived at execution
time with fresh data; a stored proof is never trusted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from driftgate.adapters.synthetic import LockTable
from driftgate.domain import ExecutionSource, SourceError
from driftgate.tools.logtext import strip_ansi

#: Execution statuses that mean the execution is over. The synthetic generator emits exactly these.
TERMINAL_STATUSES = frozenset({"success", "failed", "approval_rejected", "cancelled", "canceled", "timed_out"})
#: Statuses that mean the execution is still going.
RUNNING_STATUSES = frozenset({"running", "in_progress", "queued", "pending", "waiting"})

_LOCK_ID = re.compile(r"Lock Info:.{0,200}?\bID:\s+([0-9A-Za-z][0-9A-Za-z-]{7,})", re.DOTALL)


def extract_lock_id(log_text: str) -> str | None:
    """The one lock id in a Terraform "Error acquiring the state lock" block; None if absent or ambiguous."""
    found = {m.group(1) for m in _LOCK_ID.finditer(strip_ansi(log_text))}
    return next(iter(found)) if len(found) == 1 else None


@dataclass(frozen=True)
class HolderProof:
    lock_id: str | None
    status: str  # "dead" | "running" | "unknown" (the values SafetyGate's `lock_holder` takes)
    proven: bool
    holder_execution_id: str | None
    reason: str

    def as_record(self) -> dict[str, object]:
        return {
            "lock_id": self.lock_id,
            "status": self.status,
            "proven": self.proven,
            "holder_execution_id": self.holder_execution_id,
            "reason": self.reason,
        }


def _unknown(lock_id: str | None, reason: str, holder: str | None = None) -> HolderProof:
    return HolderProof(lock_id, "unknown", False, holder, reason)


def derive_holder_proof(lock_id: str | None, locks: LockTable | None, source: ExecutionSource) -> HolderProof:
    if not lock_id:
        return _unknown(None, "no unambiguous lock id in the failed log")
    if locks is None:
        return _unknown(lock_id, "no lock table available: holder liveness cannot be observed")
    entry = locks.locks.get(lock_id)
    if entry is None:
        return _unknown(lock_id, "the lock is not in the lock table")
    if not entry.holder:
        return _unknown(lock_id, "the lock entry names no holder execution")
    try:
        holder = source.get_execution(entry.holder)
    except SourceError:
        return _unknown(lock_id, f"holder execution {entry.holder!r} is unknown to the execution source", entry.holder)
    if holder.execution_id != entry.holder:  # the id on the execution must be the id on the lock
        return _unknown(lock_id, "holder execution id does not match the lock", entry.holder)
    status = holder.status.strip().lower()
    if entry.alive or status in RUNNING_STATUSES:
        return HolderProof(lock_id, "running", False, entry.holder, "the lock holder is still running")
    if status not in TERMINAL_STATUSES:
        return _unknown(
            lock_id, f"holder execution status {holder.status!r} is not a known terminal status", entry.holder
        )
    return HolderProof(lock_id, "dead", True, entry.holder, f"holder execution {entry.holder} is {status}")
