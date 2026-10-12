"""Approving, denying, expiring and executing Tier 1/2 requests (PRD N4). Every step is audited.

The rules this module exists to keep:

- An approval is not an authorisation. It is one input. At execution time the request's gate is re-run from fresh
  facts (kill switch re-read from disk, abort ceiling, rate limit seeded from the audit log, Tier 2 holder-death
  proof re-derived from the lock table). Only an `allowed` decision continues, and the gate can only downgrade, so
  no approval can raise a tier or turn a refused or downgraded decision into an allowed one.
- Anything not clearly allowed fails closed: an expired, denied, cancelled or already executed request never runs,
  a gate refusal cancels the request, and a missing gate factory refuses.
- The tier, action and execution id come from the immutable request row, never from the stored payload.
- Execution is stubbed: the only target is the synthetic one, and nothing here can reach live infrastructure.

Audit vocabulary (the kinds are pinned, so everything is an existing kind plus a `stage` field):
`proposal`/`approval_requested` (written by the orchestrator), `proposal`/`approval_decision`,
`rejected`/`approval_refused` (an approve/deny that was refused), `rejected`/`approval_denied`,
`expired`/`approval`, `gate_decision`/`approval_recheck`, `rejected`/`approval_execute_refused`,
then `execution` (`dry_run`, `execute`), `verification`/`result` and `failed`, each carrying `request_id`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from driftgate.approvals import (
    ApprovalError,
    ApprovalRequest,
    ApprovalStore,
    Expired,
    NotOpen,
)
from driftgate.audit import AuditLog, record_decision
from driftgate.domain import (
    DryRunResult,
    ExecutionResult,
    Remediation,
    RemediationTarget,
    VerificationResult,
)
from driftgate.gates import RATE_LIMIT_WINDOW_S, GateDecision, RateLimiter, SafetyGate
from driftgate.orchestrator import GateFactory, Orchestrator
from driftgate.remediation_catalog import is_catalog_action

EXECUTED = "executed"
ESCALATED = "escalated"
Clock = Callable[[], float]


@dataclass
class ApprovalExecution:
    """What `execute_approved` did. `kind` is `executed` only when the stubbed action ran AND verified."""

    request_id: int
    kind: str  # executed | escalated
    reason: str
    status: str  # the request's status afterwards
    gate: GateDecision | None = None
    dry_run: DryRunResult | None = None
    execution: ExecutionResult | None = None
    verification: VerificationResult | None = None
    notes: list[str] = field(default_factory=list)


class ApprovalService:
    """approve / deny / expire-check with audit. The store enforces the rules; this records what happened."""

    def __init__(self, store: ApprovalStore, audit: AuditLog, clock: Clock) -> None:
        self._store = store
        self._audit = audit
        self._clock = clock

    def approve(self, request_id: int, approver: str) -> ApprovalRequest:
        return self._decide(request_id, approver, "approve")

    def deny(self, request_id: int, approver: str) -> ApprovalRequest:
        return self._decide(request_id, approver, "deny")

    def _decide(self, request_id: int, approver: str, decision: str) -> ApprovalRequest:
        try:
            req = self._store.decide(request_id, approver, decision)
        except ApprovalError as e:
            self._audit_refusal(request_id, approver, decision, e)
            raise
        decisions = self._store.decisions(request_id)
        self._audit.append(
            "proposal",
            req.fingerprint,
            req.execution_id,
            stage="approval_decision",
            request_id=req.id,
            approver=decisions[-1].approver,
            decision=decision,
            status=req.status,
            approvals=len(self._store.approvers(req.id)),
            required=req.required,
        )
        if req.status == "denied":
            self._audit.append(
                "rejected",
                req.fingerprint,
                req.execution_id,
                stage="approval_denied",
                request_id=req.id,
                reason="denied by an approver; the remediation will not run",
            )
        return req

    def _audit_refusal(self, request_id: int, approver: str, decision: str, error: ApprovalError) -> None:
        try:
            req = self._store.get(request_id)
        except ApprovalError:
            return  # unknown request: nothing to attach an audit entry to
        if isinstance(error, Expired):
            self._audit_expired(req)
            return
        self._audit.append(
            "rejected",
            req.fingerprint,
            req.execution_id,
            stage="approval_refused",
            request_id=req.id,
            decision=decision,
            approver=approver.strip().casefold(),
            error=type(error).__name__,
            reason=str(error),
        )

    def _audit_expired(self, req: ApprovalRequest) -> None:
        self._audit.append(
            "expired",
            req.fingerprint,
            req.execution_id,
            stage="approval",
            request_id=req.id,
            tier=req.tier,
            expires_at=req.expires_at,
            reason="approval window closed before the remediation was approved and executed: escalated to a human",
        )

    def expire_check(self) -> list[ApprovalRequest]:
        """Expire every overdue open request. Each one is audited and escalates to a human; none ever executes."""
        expired = self._store.expire_due()
        for req in expired:
            self._audit_expired(req)
        return expired


def _refuse(
    audit: AuditLog, req: ApprovalRequest, reason: str, *, status: str, notes: list[str] | None = None
) -> ApprovalExecution:
    audit.append(
        "rejected",
        req.fingerprint,
        req.execution_id,
        stage="approval_execute_refused",
        request_id=req.id,
        status=status,
        reason=reason,
    )
    return ApprovalExecution(req.id, ESCALATED, reason, status, notes=notes or [])


def _seed_rate_limiter(limiter: RateLimiter, audit: AuditLog, req: ApprovalRequest, now: float) -> None:
    """Preload the fingerprint's earlier allowed attempts from the audit log (a new process starts with an empty
    in-memory limiter). The request's own request-time decision and execution-time re-checks are not attempts."""
    times = [
        e.ts
        for e in audit.read("gate_decision")
        if e.fingerprint == req.fingerprint
        and e.payload.get("decision") == "allowed"
        and "stage" not in e.payload
        and e.seq != req.gate_seq
        and e.ts > now - RATE_LIMIT_WINDOW_S
    ]
    limiter.seed(req.fingerprint, times)


def execute_approved(
    orchestrator: Orchestrator,
    store: ApprovalStore,
    request_id: int,
    *,
    clock: Clock,
    target: RemediationTarget | None = None,
    gate_factory: GateFactory | None = None,
) -> ApprovalExecution:
    """Execute an approved request through the gate. Never raises for an ordinary refusal; returns what happened.

    Order: status/expiry check, fresh-facts gate re-run (refusal cancels the request), stubbed dry run, atomic claim
    (approved -> executed), stubbed execute, stubbed verify. Each step is audited.
    """
    audit = orchestrator.audit
    target = target or orchestrator.target
    factory = gate_factory or orchestrator.gate_factory
    req = store.get(request_id)

    if req.status in ("pending", "approved") and clock() >= req.expires_at:
        for expired in store.expire_due():
            audit.append(
                "expired",
                expired.fingerprint,
                expired.execution_id,
                stage="approval",
                request_id=expired.id,
                tier=expired.tier,
                expires_at=expired.expires_at,
                reason="approval window closed before execution: escalated to a human",
            )
        req = store.get(request_id)
    if req.status != "approved":
        have = len(store.approvers(req.id))
        detail = f" ({have} of {req.required} approvals)" if req.status == "pending" else ""
        return _refuse(
            audit, req, f"request is {req.status}{detail}, not approved: nothing executed", status=req.status
        )
    if len(store.approvers(req.id)) < req.required:  # defence in depth behind the store's own quorum rule
        return _refuse(audit, req, "fewer approvals than required: nothing executed", status=req.status)
    if factory is None:
        return _refuse(audit, req, "no execution-time gate available: refusing", status=req.status)
    if not is_catalog_action(req.tier, req.action):
        return _cancel(audit, store, req, f"action {req.action!r} is not in the Tier {req.tier} catalog")

    # -- the gate, re-run from fresh facts. The tier and action are the request row's, not the payload's. --------
    rem, facts, proof, problem = orchestrator.gate_inputs(
        req.execution_id,
        req.tier,
        req.action,
        str(req.payload.get("rationale", "")),
        bool(req.payload.get("reversible", False)),
        _confidence(req),
    )
    if facts is None:
        return _cancel(audit, store, req, f"gate facts could not be re-derived: {problem}")
    if facts.fingerprint != req.fingerprint:
        return _cancel(audit, store, req, "the failure fingerprint changed since the request: refusing")
    limiter = RateLimiter(clock)
    _seed_rate_limiter(limiter, audit, req, clock())
    gate: SafetyGate = factory(limiter)
    decision = gate.decide(rem, facts)
    record_decision(audit, decision, req.fingerprint, req.execution_id, stage="approval_recheck", request_id=req.id)
    if proof is not None:
        audit.append(
            "proposal", req.fingerprint, req.execution_id, stage="holder_proof", request_id=req.id, **proof.as_record()
        )
    if not decision.allowed or decision.remediation.tier != req.tier:  # tier can never differ; checked anyway
        out = _cancel(audit, store, req, f"gate {decision.decision} at execution time: {decision.reason}")
        out.gate = decision
        return out

    action = decision.remediation
    action.dry_run["execution_id"] = req.execution_id
    if req.tier == 2:
        assert proof is not None and proof.lock_id is not None  # allowed implies a proven holder
        action.dry_run["lock_id"] = proof.lock_id  # the lock id derived from the log just now
    return _run(audit, store, target, req, action, decision)


def _confidence(req: ApprovalRequest) -> float | None:
    c = req.payload.get("agent_confidence")
    return float(c) if isinstance(c, (int, float)) else None


def _cancel(audit: AuditLog, store: ApprovalStore, req: ApprovalRequest, reason: str) -> ApprovalExecution:
    try:
        closed = store.cancel(req.id, reason)
        status = closed.status
    except NotOpen:
        status = store.get(req.id).status
    return _refuse(audit, req, reason, status=status)


def _run(
    audit: AuditLog,
    store: ApprovalStore,
    target: RemediationTarget,
    req: ApprovalRequest,
    action: Remediation,
    decision: GateDecision,
) -> ApprovalExecution:
    fp, eid = req.fingerprint, req.execution_id
    result = ApprovalExecution(req.id, ESCALATED, "", req.status, gate=decision)

    dry = target.dry_run(action)
    result.dry_run = dry
    audit.append(
        "execution", fp, eid, stage="dry_run", ok=dry.ok, description=dry.description, tier=req.tier, request_id=req.id
    )
    if not dry.ok:
        audit.append("failed", fp, eid, stage="dry_run", description=dry.description, request_id=req.id)
        return _cancel_result(audit, store, req, result, f"dry run failed: {dry.description}")

    try:
        store.claim_for_execution(req.id)  # atomic: only one caller can move approved -> executed
    except Expired:
        store.expire_due()
        return _cancel_result(audit, store, req, result, "approval window closed before execution", cancel=False)
    except ApprovalError as e:
        return _cancel_result(audit, store, req, result, f"could not claim the request: {e}", cancel=False)

    done = target.execute(action)
    result.execution = done
    audit.append(
        "execution",
        fp,
        eid,
        stage="execute",
        ok=done.ok,
        description=done.description,
        tier=req.tier,
        request_id=req.id,
    )
    if not done.ok:
        audit.append("failed", fp, eid, stage="execute", description=done.description, request_id=req.id)
        store.set_note(req.id, f"execution failed: {done.description}")
        result.reason, result.status = f"execution failed: {done.description}", "executed"
        return result

    check = target.verify(action)
    result.verification = check
    audit.append(
        "verification",
        fp,
        eid,
        stage="result",
        request_id=req.id,
        tier=req.tier,
        action=req.action,
        verified=check.verified,
        description=check.description,
    )
    result.status = "executed"
    if check.verified:
        audit.append("verification", fp, eid, stage="closed", request_id=req.id)
        store.set_note(req.id, f"executed and verified: {done.description}")
        result.kind, result.reason = EXECUTED, done.description
    else:
        store.set_note(req.id, f"executed, verification failed: {check.description}")
        result.reason = f"verification failed: {check.description}"
    return result


def _cancel_result(
    audit: AuditLog,
    store: ApprovalStore,
    req: ApprovalRequest,
    result: ApprovalExecution,
    reason: str,
    *,
    cancel: bool = True,
) -> ApprovalExecution:
    if cancel:
        try:
            store.cancel(req.id, reason)
        except NotOpen:
            pass
    status = store.get(req.id).status
    refused = _refuse(audit, req, reason, status=status)
    result.kind, result.reason, result.status = ESCALATED, refused.reason, status
    return result
