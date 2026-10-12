"""Approving, denying, expiring and executing Tier 1/2 requests (PRD N4). Every step is audited.

The rules this module exists to keep:

- An approval is not an authorisation. It is one input. At execution time the request's gate is re-run from fresh
  facts (kill switch re-read from disk, abort ceiling, rate limit seeded from the audit log, Tier 2 holder-death
  proof re-derived from the lock table). Only an `allowed` decision continues, and the gate can only downgrade, so
  no approval can raise a tier or turn a refused or downgraded decision into an allowed one.
- Anything not clearly allowed fails closed: an expired, denied, cancelled or already executed request never runs,
  a gate refusal cancels the request, and a missing gate factory refuses.
- Approving is gated too. `approve` runs the same gate from fresh facts and refuses early (`GateRefused`) when the
  gate would not allow the action right now (kill switch off, abort ceiling, Tier 2 holder alive or unproven, rate
  limit). An early refusal is audited and leaves the request untouched: it stays pending (the condition may be
  temporary; if it is not, the request expires on its own window) and no decision is recorded. `deny` is exempt from
  the gate: denial is the safe direction, so anyone, including the requester, may deny. An approval can never raise
  a tier or turn a refused/downgraded decision into an allowed one: the gate result is only ever used to say no, and
  `execute` runs the full gate again regardless of what happened at approval time.
- The tier, action and execution id come from the immutable request row, never from the stored payload.
- Execution is stubbed: the only target is the synthetic one, and nothing here can reach live infrastructure.

Audit vocabulary (the kinds are pinned, so everything is an existing kind plus a `stage` field):
`proposal`/`approval_requested` (written by the orchestrator), `proposal`/`approval_decision`,
`rejected`/`approval_refused` (an approve/deny that was refused), `rejected`/`approval_denied`,
`expired`/`approval`, `gate_decision`/`approval_precheck` (at approve time) and `approval_recheck` (at execute),
`rejected`/`approval_refused` with error `GateRefused`, `rejected`/`approval_execute_refused`,
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
    normalize_identity,
)
from driftgate.audit import AuditLog, record_decision
from driftgate.domain import (
    DryRunResult,
    ExecutionResult,
    Remediation,
    RemediationTarget,
    VerificationResult,
)
from driftgate.gates import RATE_LIMIT_WINDOW_S, GateDecision, RateLimiter
from driftgate.lock_proof import HolderProof
from driftgate.orchestrator import GateFactory, Orchestrator
from driftgate.remediation_catalog import is_catalog_action


class GateRefused(ApprovalError):
    """The safety gate would not allow this action right now, so the approval was refused and nothing changed."""


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
    """approve / deny / expire-check with audit. The store enforces the rules; this records what happened.

    `orchestrator` supplies the gate inputs and the gate factory for the approve-time gate check. It is required:
    a service that could approve without consulting the gate would be a way around it."""

    def __init__(self, store: ApprovalStore, audit: AuditLog, clock: Clock, *, orchestrator: Orchestrator) -> None:
        self._store = store
        self._audit = audit
        self._clock = clock
        self._orchestrator = orchestrator

    def approve(self, request_id: int, approver: str) -> ApprovalRequest:
        return self._decide(request_id, approver, "approve")

    def deny(self, request_id: int, approver: str) -> ApprovalRequest:
        return self._decide(request_id, approver, "deny")  # exempt from the gate: denial is the safe direction

    def _gate_precheck(self, request_id: int, approver: str) -> None:
        """Raise `GateRefused` if the gate would not allow the request now. Only runs when the approval could
        otherwise be recorded; every other refusal (expired, not pending, self-approval, repeat) is the store's."""
        try:
            req = self._store.get(request_id)
            who = normalize_identity(approver)
        except ApprovalError:
            return  # the store reports it
        if (
            req.status != "pending"
            or self._clock() >= req.expires_at
            or who == req.requested_by
            or any(d.approver == who for d in self._store.decisions(request_id))
        ):
            return
        verdict = _evaluate(self._orchestrator, self._audit, req, self._clock, "approval_precheck")
        if not verdict.ok:
            raise GateRefused(f"the gate would not allow this action now, so it cannot be approved: {verdict.reason}")

    def _decide(self, request_id: int, approver: str, decision: str) -> ApprovalRequest:
        try:
            if decision == "approve":
                self._gate_precheck(request_id, approver)
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
            approver=ascii(approver)[:80],  # as typed, escaped: it may not be a valid identity
            error=type(error).__name__,
            reason=str(error),
        )

    def _audit_expired(self, req: ApprovalRequest) -> None:
        _audit_expired(
            self._audit, req, "approval window closed before the remediation was approved and executed: escalated"
        )

    def expire_check(self) -> list[ApprovalRequest]:
        """Expire every overdue open request. Each one is audited and escalates to a human; none ever executes."""
        expired = self._store.expire_due()
        for req in expired:
            self._audit_expired(req)
        return expired


def _audit_expired(audit: AuditLog, req: ApprovalRequest, reason: str) -> None:
    audit.append(
        "expired",
        req.fingerprint,
        req.execution_id,
        stage="approval",
        request_id=req.id,
        tier=req.tier,
        expires_at=req.expires_at,
        reason=reason + " to a human",
    )


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
            _audit_expired(audit, expired, "approval window closed before execution: escalated")
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

    verdict = _evaluate(orchestrator, audit, req, clock, "approval_recheck", gate_factory=factory)
    if not verdict.ok:
        out = _cancel(audit, store, req, verdict.reason)
        out.gate = verdict.decision
        return out
    decision, proof = verdict.decision, verdict.proof
    assert decision is not None

    action = decision.remediation
    action.dry_run["execution_id"] = req.execution_id
    if req.tier == 2:
        assert proof is not None and proof.lock_id is not None  # allowed implies a proven holder
        action.dry_run["lock_id"] = proof.lock_id  # the lock id derived from the log just now
    return _run(audit, store, target, req, action, decision)


@dataclass
class _Verdict:
    ok: bool
    reason: str
    decision: GateDecision | None = None
    proof: HolderProof | None = None


def _evaluate(
    orchestrator: Orchestrator,
    audit: AuditLog,
    req: ApprovalRequest,
    clock: Clock,
    stage: str,
    *,
    gate_factory: GateFactory | None = None,
) -> _Verdict:
    """Run the request's gate from fresh facts: kill switch re-read from disk, abort ceiling, rate limit seeded from
    the audit log, signature and blast radius re-derived, Tier 2 holder proof re-derived from the lock table. The
    tier and action are the request row's, not the payload's. `ok` only when the gate ALLOWED the same tier. Used
    at approve time (`approval_precheck`) and again at execute time (`approval_recheck`); it only ever says no."""
    factory = gate_factory or orchestrator.gate_factory
    if factory is None:
        return _Verdict(False, "no gate available: refusing")
    if not is_catalog_action(req.tier, req.action):
        return _Verdict(False, f"action {req.action!r} is not in the Tier {req.tier} catalog")
    rem, facts, proof, problem = orchestrator.gate_inputs(
        req.execution_id,
        req.tier,
        req.action,
        str(req.payload.get("rationale", "")),
        bool(req.payload.get("reversible", False)),
        _confidence(req),
    )
    if facts is None:
        return _Verdict(False, f"gate facts could not be re-derived: {problem}")
    if facts.fingerprint != req.fingerprint:
        return _Verdict(False, "the failure fingerprint changed since the request: refusing")
    limiter = RateLimiter(clock)
    _seed_rate_limiter(limiter, audit, req, clock())
    decision = factory(limiter).decide(rem, facts)
    record_decision(audit, decision, req.fingerprint, req.execution_id, stage=stage, request_id=req.id)
    if proof is not None:
        audit.append(
            "proposal", req.fingerprint, req.execution_id, stage="holder_proof", request_id=req.id, **proof.as_record()
        )
    if not decision.allowed or decision.remediation.tier != req.tier:  # tier can never differ; checked anyway
        return _Verdict(False, f"gate {decision.decision}: {decision.reason}", decision, proof)
    return _Verdict(True, "", decision, proof)


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
        # The claim itself recorded the expiry, so `expire_due()` would find nothing: audit it here, like every
        # other expiry path.
        _audit_expired(audit, store.get(req.id), "approval window closed before execution: escalated")
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
