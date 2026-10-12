"""Persisted approval requests and decisions for Tier 1 and Tier 2 remediations (PRD N4).

SQLite (stdlib), at `state/approvals.sqlite`. This module is only the store and its rules; it holds no gate logic,
runs nothing and never touches a `RemediationTarget`. `approval_flow` audits and executes.

Identities are plain strings. The CLI is a trusted local operator interface, NOT authentication: nothing here
proves who is typing, and anyone who can write the SQLite file can forge decisions as any name. What the store does
guarantee is bookkeeping, against honest mistakes and against a partial or buggy writer:

- the same person counts once. Identities are normalised (NFKC, case-fold, collapsed spaces) and an identity that
  contains control, format, zero-width, line/paragraph-separator or variation-selector characters, or anything
  outside printable ASCII after normalisation, is REFUSED rather than folded. SQL CHECKs make the stored form
  exactly the normalised form (lower-case printable ASCII, single spaces), so a direct insert of "Alice" beside
  "alice" is rejected and UNIQUE(request_id, approver) really is per person;
- the person who triggered the run never counts as an approver;
- the quorum is derived from the immutable tier (`REQUIRED_APPROVALS`), never from a stored column; `required` is
  CHECK-pinned to the tier and, with `expires_at` (bounded by the longest window), `requested_at`, `fingerprint`,
  tier, action, execution id and requester, frozen by triggers after insert;
- a request can only be INSERTed as `pending`, and can only become `approved` or `executed` while the decisions
  table holds the quorum of distinct non-requester approvals and no denial (checked in SQL by trigger, and again
  in code at claim time). A status written without those decisions is refused;
- a request moves only forward through its statuses.

None of this resists someone who edits the file with the triggers dropped (the CLI is not authentication); the real
defence is that execution never trusts an approval alone: `approval_flow` re-runs the full gate from fresh facts.

Statuses: pending -> approved | denied | expired | cancelled; approved -> executed | expired | cancelled. Every
other status is terminal.

Time: every comparison uses the injected clock. A request is expired when `now >= expires_at` (an approval at
exactly the deadline is refused). Expiry applies to `pending` and to `approved` requests that have not been
executed yet, so an approval does not outlive its window.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_APPROVALS_PATH = Path(__file__).resolve().parents[2] / "state" / "approvals.sqlite"

TIER1_TIMEOUT_S = 15 * 60  # PRD: single approval, 15-minute timeout
TIER2_TIMEOUT_S = 60 * 60  # PRD gives Tier 2 no timeout; an open-ended approval window is not acceptable (BLOCKERS.md)
TIMEOUT_S = {1: TIER1_TIMEOUT_S, 2: TIER2_TIMEOUT_S}
REQUIRED_APPROVALS = {1: 1, 2: 2}  # PRD: Tier 1 single approval, Tier 2 two-person approval

STATUSES = ("pending", "approved", "denied", "expired", "executed", "cancelled")
OPEN_STATUSES = ("pending", "approved")
DECISIONS = ("approve", "deny")

Clock = Callable[[], float]


class ApprovalError(Exception):
    """Base class: the operation was refused and nothing changed (except an expiry that was recorded)."""


class IdentityError(ApprovalError):
    pass


class UnknownRequest(ApprovalError):
    pass


class DuplicateRequest(ApprovalError):
    pass


class DuplicateDecision(ApprovalError):
    """The same person (after normalisation) already decided this request."""


class SelfApproval(ApprovalError):
    """The person who triggered the run cannot approve it."""


class NotOpen(ApprovalError):
    """The request is not in a status that allows this operation (denied, executed, cancelled, already approved)."""


class Expired(ApprovalError):
    """The request's deadline passed; it is now marked expired and can never be approved or executed."""


_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
_EXTRA_INVISIBLE = frozenset({0x115F, 0x1160, 0x3164, 0xFFA0})  # hangul fillers render as nothing


def _is_invisible(ch: str) -> bool:
    cp = ord(ch)
    return (
        unicodedata.category(ch) in _INVISIBLE_CATEGORIES
        or 0x180B <= cp <= 0x180F  # mongolian variation selectors
        or 0xFE00 <= cp <= 0xFE0F  # variation selectors
        or 0xE0100 <= cp <= 0xE01EF  # variation selectors supplement
        or cp in _EXTRA_INVISIBLE
    )


def normalize_identity(raw: str) -> str:
    """The one canonical form of an identity, or `IdentityError`. Invisible characters (control, format, zero-width,
    separators, variation selectors) are refused, not stripped: "alice\u200b" must not become a second person.
    Then NFKC, case-fold, collapse spaces; the result must be non-empty printable ASCII (what the SQL CHECKs
    enforce)."""
    if not isinstance(raw, str):
        raise IdentityError("identity must be a string")
    if any(_is_invisible(c) for c in raw):
        raise IdentityError("identity contains control, zero-width or other invisible characters: refused")
    norm = unicodedata.normalize("NFKC", raw)
    norm = unicodedata.normalize("NFKC", norm.casefold())
    norm = " ".join(norm.split())
    if not norm:
        raise IdentityError("an identity is required (empty identities are refused)")
    if any(_is_invisible(c) for c in norm) or not all(" " <= c <= "~" for c in norm):
        raise IdentityError("identity must be printable ASCII after normalisation: refused")
    return norm


@dataclass(frozen=True)
class ApprovalRequest:
    id: int
    execution_id: str
    fingerprint: str
    tier: int
    action: str
    requested_by: str  # normalised identity of the person who triggered the run
    requested_at: float
    expires_at: float
    status: str
    required: int
    payload: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    gate_seq: int | None = None  # audit seq of the request-time gate decision (excluded from rate-limit re-seeding)


@dataclass(frozen=True)
class Decision:
    request_id: int
    approver: str  # normalised
    decision: str
    at: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    tier INTEGER NOT NULL CHECK (tier IN (1, 2)),
    action TEXT NOT NULL,
    requested_by TEXT NOT NULL CHECK (length(requested_by) > 0 AND requested_by = lower(requested_by)
        AND requested_by = trim(requested_by) AND requested_by NOT LIKE '%  %'
        AND requested_by NOT GLOB '*[^ -~]*'),
    requested_at REAL NOT NULL,
    expires_at REAL NOT NULL CHECK (expires_at > requested_at AND expires_at <= requested_at + 3600),
    status TEXT NOT NULL CHECK (status IN ('pending','approved','denied','expired','executed','cancelled')),
    required INTEGER NOT NULL CHECK (required = CASE tier WHEN 1 THEN 1 ELSE 2 END),
    payload TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    gate_seq INTEGER
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES requests(id),
    approver TEXT NOT NULL CHECK (length(approver) > 0 AND approver = lower(approver)
        AND approver = trim(approver) AND approver NOT LIKE '%  %' AND approver NOT GLOB '*[^ -~]*'),
    decision TEXT NOT NULL CHECK (decision IN ('approve','deny')),
    at REAL NOT NULL,
    UNIQUE (request_id, approver)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_request ON requests (execution_id, action)
    WHERE status IN ('pending', 'approved');
CREATE TRIGGER IF NOT EXISTS requests_insert_pending_only BEFORE INSERT ON requests
WHEN NEW.status != 'pending'
BEGIN SELECT RAISE(ABORT, 'a request can only be created pending'); END;
CREATE TRIGGER IF NOT EXISTS requests_forward_only BEFORE UPDATE OF status ON requests
WHEN NEW.status != OLD.status AND NOT (
    (OLD.status = 'pending' AND NEW.status IN ('approved','denied','expired','cancelled'))
    OR (OLD.status = 'approved' AND NEW.status IN ('executed','expired','cancelled')))
BEGIN SELECT RAISE(ABORT, 'illegal status transition'); END;
CREATE TRIGGER IF NOT EXISTS requests_approved_needs_decisions BEFORE UPDATE OF status ON requests
WHEN ((OLD.status = 'pending' AND NEW.status = 'approved') OR (OLD.status = 'approved' AND NEW.status = 'executed'))
    AND (
    EXISTS (SELECT 1 FROM decisions WHERE request_id = OLD.id AND decision = 'deny')
    OR (SELECT count(*) FROM decisions
        WHERE request_id = OLD.id AND decision = 'approve' AND approver != OLD.requested_by)
       < (CASE OLD.tier WHEN 1 THEN 1 ELSE 2 END))
BEGIN SELECT RAISE(ABORT, 'approved/executed needs the approvals that justify it'); END;
CREATE TRIGGER IF NOT EXISTS requests_identity_frozen
BEFORE UPDATE OF requested_by, tier, action, execution_id, fingerprint, requested_at, expires_at, required ON requests
BEGIN SELECT RAISE(ABORT, 'request identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS decisions_only_while_pending BEFORE INSERT ON decisions
WHEN (SELECT status FROM requests WHERE id = NEW.request_id) != 'pending'
BEGIN SELECT RAISE(ABORT, 'request is not pending'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_self_approval BEFORE INSERT ON decisions
WHEN NEW.decision = 'approve' AND NEW.approver = (SELECT requested_by FROM requests WHERE id = NEW.request_id)
BEGIN SELECT RAISE(ABORT, 'the person who triggered the run cannot approve it'); END;
CREATE TRIGGER IF NOT EXISTS decisions_append_only_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_append_only_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
"""


def _request(row: sqlite3.Row) -> ApprovalRequest:
    return ApprovalRequest(
        id=row["id"],
        execution_id=row["execution_id"],
        fingerprint=row["fingerprint"],
        tier=row["tier"],
        action=row["action"],
        requested_by=row["requested_by"],
        requested_at=row["requested_at"],
        expires_at=row["expires_at"],
        status=row["status"],
        required=REQUIRED_APPROVALS[row["tier"]],  # from the immutable tier; the stored column is never trusted
        payload=json.loads(row["payload"]),
        note=row["note"],
        gate_seq=row["gate_seq"],
    )


class ApprovalStore:
    """SQLite-backed approvals. Each public method is one transaction; a refused operation changes nothing, except
    that discovering an overdue request records its expiry (and then raises `Expired`)."""

    def __init__(self, path: Path | str, clock: Clock) -> None:
        self.path = Path(path)
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, isolation_level=None)  # explicit transactions below
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> ApprovalStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    # -- reads -------------------------------------------------------------------------------
    def get(self, request_id: int) -> ApprovalRequest:
        row = self._db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise UnknownRequest(f"no approval request {request_id}")
        return _request(row)

    def list(self, status: str | None = None) -> list[ApprovalRequest]:
        if status is not None and status not in STATUSES:
            raise ApprovalError(f"unknown status {status!r}")
        sql = "SELECT * FROM requests" + (" WHERE status = ?" if status else "") + " ORDER BY id"
        return [_request(r) for r in self._db.execute(sql, (status,) if status else ())]

    def decisions(self, request_id: int) -> list[Decision]:
        rows = self._db.execute("SELECT * FROM decisions WHERE request_id = ? ORDER BY id", (request_id,))
        return [Decision(r["request_id"], r["approver"], r["decision"], r["at"]) for r in rows]

    def approvers(self, request_id: int) -> list[str]:
        """Distinct approvers in the normalised form, excluding the requester. A stored identity that does not
        normalise is not counted (fail closed)."""
        requester = self.get(request_id).requested_by
        seen: list[str] = []
        for d in self.decisions(request_id):
            if d.decision != "approve":
                continue
            try:
                who = normalize_identity(d.approver)
            except IdentityError:
                continue
            if who != requester and who not in seen:
                seen.append(who)
        return seen

    # -- writes ------------------------------------------------------------------------------
    def create(
        self,
        *,
        execution_id: str,
        fingerprint: str,
        tier: int,
        action: str,
        requested_by: str,
        payload: dict[str, Any],
        gate_seq: int | None = None,
    ) -> ApprovalRequest:
        if tier not in TIMEOUT_S:
            raise ApprovalError(f"tier {tier} cannot be approved: only Tier 1 and Tier 2 use approval requests")
        who = normalize_identity(requested_by)
        now = self._clock()
        try:
            with self._tx() as db:
                cur = db.execute(
                    "INSERT INTO requests (execution_id, fingerprint, tier, action, requested_by, requested_at,"
                    " expires_at, status, required, payload, gate_seq) VALUES (?,?,?,?,?,?,?,'pending',?,?,?)",
                    (
                        execution_id,
                        fingerprint,
                        tier,
                        action,
                        who,
                        now,
                        now + TIMEOUT_S[tier],
                        REQUIRED_APPROVALS[tier],
                        json.dumps(payload, sort_keys=True, default=str),
                        gate_seq,
                    ),
                )
                new_id = int(cur.lastrowid or 0)
        except sqlite3.IntegrityError as e:
            raise DuplicateRequest(f"an approval request for {execution_id} / {action} is already open") from e
        return self.get(new_id)

    def expire_due(self) -> list[ApprovalRequest]:
        """Mark every open request whose deadline has passed as expired; returns those that were just expired."""
        now = self._clock()
        with self._tx() as db:
            rows = db.execute(
                "SELECT * FROM requests WHERE status IN ('pending','approved') AND ? >= expires_at ORDER BY id",
                (now,),
            ).fetchall()
            for r in rows:
                db.execute("UPDATE requests SET status = 'expired' WHERE id = ?", (r["id"],))
        return [self.get(r["id"]) for r in rows]

    def _expire_if_due(self, db: sqlite3.Connection, req: ApprovalRequest) -> bool:
        if req.status in OPEN_STATUSES and self._clock() >= req.expires_at:
            db.execute("UPDATE requests SET status = 'expired' WHERE id = ?", (req.id,))
            return True
        return False

    def decide(self, request_id: int, approver: str, decision: str) -> ApprovalRequest:
        """Record one person's approve/deny. Approving to quorum moves the request to `approved`; any denial
        moves it to `denied` (fail closed). Raises (and changes nothing) on self-approval, a repeat decision by the
        same person, an empty identity, or a request that is not pending; raises `Expired` after recording expiry."""
        if decision not in DECISIONS:
            raise ApprovalError(f"unknown decision {decision!r}")
        who = normalize_identity(approver)
        expired = False
        with self._tx() as db:
            req = self.get(request_id)
            if self._expire_if_due(db, req):
                expired = True
            elif req.status != "pending":
                raise NotOpen(f"request {request_id} is {req.status}, not pending")
            elif decision == "approve" and who == req.requested_by:
                raise SelfApproval("the person who triggered the run cannot approve it")
            elif any(d.approver == who for d in self.decisions(request_id)):
                raise DuplicateDecision(f"{who!r} already decided request {request_id}")
            else:
                try:
                    db.execute(
                        "INSERT INTO decisions (request_id, approver, decision, at) VALUES (?,?,?,?)",
                        (request_id, who, decision, self._clock()),
                    )
                except sqlite3.IntegrityError as e:
                    raise DuplicateDecision(f"{who!r} already decided request {request_id}") from e
                if decision == "deny":
                    db.execute("UPDATE requests SET status = 'denied' WHERE id = ?", (request_id,))
                elif len(self.approvers(request_id)) >= req.required:
                    db.execute("UPDATE requests SET status = 'approved' WHERE id = ?", (request_id,))
        if expired:
            raise Expired(f"request {request_id} expired at {req.expires_at}")
        return self.get(request_id)

    def approve(self, request_id: int, approver: str) -> ApprovalRequest:
        return self.decide(request_id, approver, "approve")

    def deny(self, request_id: int, approver: str) -> ApprovalRequest:
        return self.decide(request_id, approver, "deny")

    def claim_for_execution(self, request_id: int) -> ApprovalRequest:
        """approved -> executed, atomically: a second caller (or process) cannot execute the same request. Call it
        only after the execution-time gate allowed; the returned request is the one claimed."""
        expired = False
        with self._tx() as db:
            req = self.get(request_id)
            if self._expire_if_due(db, req):
                expired = True
            elif req.status != "approved":
                raise NotOpen(f"request {request_id} is {req.status}, not approved")
            elif len(self.approvers(request_id)) < req.required:
                raise NotOpen(f"request {request_id} has fewer than {req.required} approvals")
            else:
                db.execute("UPDATE requests SET status = 'executed' WHERE id = ?", (request_id,))
        if expired:
            raise Expired(f"request {request_id} expired at {req.expires_at}")
        return self.get(request_id)

    def cancel(self, request_id: int, note: str) -> ApprovalRequest:
        """Close an open request without executing it (the execution-time gate refused it)."""
        with self._tx() as db:
            req = self.get(request_id)
            if req.status not in OPEN_STATUSES:
                raise NotOpen(f"request {request_id} is {req.status}")
            db.execute("UPDATE requests SET status = 'cancelled', note = ? WHERE id = ?", (note[:500], request_id))
        return self.get(request_id)

    def set_note(self, request_id: int, note: str) -> None:
        with self._tx() as db:
            db.execute("UPDATE requests SET note = ? WHERE id = ?", (note[:500], request_id))
