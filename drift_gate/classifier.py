"""Deterministic Phase 1 classifier (PRD.md SS8 steps 1-4, SS10).

Cheapest-first, early-exit checks over an ExecutionSource, run BEFORE any LLM call:
status gate (step 1), structured signature classification (step 2), fleet correlation
(step 3), flake check (step 4).

The signature -> classification/tier/action table below (RULES) is authored
independently from drift_gate.generator.faults. The generator's table is the answer
key used to inject faults and is off-limits to anything that classifies (see
generator/ground_truth.py's docstring). This one exists because a real ops team would
hand-author exactly this kind of signature runbook from observed error text; it will
often agree with the generator's table since both encode the same real-world
operational judgment, not because one reads the other.

Known gap: the synthetic generator never actually emits ExecutionStatus.REJECTED/
ABORTED (governance/user-abort outcomes are represented as a FAILED execution whose
log matches the policy_denial signature instead), so status_gate has no data to catch
in this dataset today. Left as real, protocol-correct logic rather than special-cased,
since a real ExecutionSource could emit those statuses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from drift_gate.domain import (
    BlastRadius,
    Classification,
    Evidence,
    Execution,
    ExecutionStatus,
    Gate,
    Layer,
    Node,
    Remediation,
    Report,
    Tier,
)
from drift_gate.errors import SIGNATURES, ErrorSignature
from drift_gate.protocols import ExecutionSource

FLEET_WINDOW = timedelta(minutes=30)
FLAKE_WINDOW = timedelta(minutes=10)
LOG_MATCH_BUDGET = 4000


@dataclass(frozen=True)
class SignatureRule:
    fault_id: str
    classification: Classification
    tier: Tier | None
    action: str | None
    gate: Gate | None
    reversible: bool | None
    rationale: str


RULES: tuple[SignatureRule, ...] = (
    SignatureRule("image_pull_backoff", Classification.PLATFORM, Tier.TIER_0,
                  "retry_execution", Gate.AUTO, True,
                  "registry pull failures are frequently a transient blip"),
    SignatureRule("step_timeout", Classification.TRANSIENT, Tier.TIER_0,
                  "retry_execution", Gate.AUTO, True,
                  "no evidence of a real hang; retry cheaply first"),
    SignatureRule("registry_429", Classification.TRANSIENT, Tier.TIER_0,
                  "retry_execution", Gate.AUTO, True,
                  "rate limit clears on its own; retrying is idempotent"),
    SignatureRule("cloud_throttling", Classification.TRANSIENT, Tier.TIER_0,
                  "retry_execution", Gate.AUTO, True,
                  "cloud API throttling is self-resolving on retry"),
    SignatureRule("expired_credential", Classification.PLATFORM, Tier.TIER_1,
                  "refresh_connector_token", Gate.SINGLE_APPROVAL, True,
                  "credential source is still valid; token just needs refreshing"),
    SignatureRule("registry_401", Classification.PLATFORM, Tier.TIER_1,
                  "refresh_connector_token", Gate.SINGLE_APPROVAL, True,
                  "anonymous token expired; refreshing the connector token resolves it"),
    SignatureRule("state_lock_stale_holder", Classification.PLATFORM, Tier.TIER_2,
                  "force_unlock_state", Gate.DUAL_APPROVAL, False,
                  "state lock present; holder-death must be proven before unlocking"),
    SignatureRule("expression_null", Classification.USER, Tier.TIER_3,
                  "fix_undefined_variable_expression", Gate.PULL_REQUEST, True,
                  "expression references a variable that no longer resolves"),
    SignatureRule("lockfile_mismatch", Classification.USER, Tier.TIER_3,
                  "regenerate_lockfile", Gate.PULL_REQUEST, True,
                  "provider version constraint changed; lockfile needs regenerating"),
    SignatureRule("provider_version_drift", Classification.USER, Tier.TIER_3,
                  "pin_provider_version", Gate.PULL_REQUEST, True,
                  "upgrade broke on an unpinned constraint"),
    SignatureRule("policy_denial", Classification.GOVERNANCE, None, None, None, None,
                  "policy denials are a governance outcome, never remediated"),
)
RULES_BY_FAULT_ID: dict[str, SignatureRule] = {r.fault_id: r for r in RULES}
# Signatures with no entry here (oom_killed, assume_role_denied, invalid_yaml,
# template_not_found, iam_denied) are the deliberate residual: matched text tells you
# WHAT failed but not a safe automated response, so classification/remediation for
# these falls through to the agent.


def match_signature(log_text: str) -> ErrorSignature | None:
    """Matches raw log text against known signatures via substring containment on a
    distinguishing line, since exact full-block matches break under truncation."""
    for sig in SIGNATURES:
        needle = sig.text.splitlines()[0][:40]
        if needle and needle in log_text:
            return sig
    return None


def fingerprint(execution: Execution, node: Node, sig: ErrorSignature | None) -> str:
    tag = sig.fault_id if sig else "unmatched"
    return f"{execution.template_ref.name}:{node.step_type}:{tag}"


def status_gate(execution: Execution) -> Classification | None:
    if execution.status == ExecutionStatus.REJECTED:
        return Classification.GOVERNANCE
    if execution.status == ExecutionStatus.ABORTED:
        return Classification.USER
    return None


def flake_check(source: ExecutionSource, execution: Execution) -> bool:
    """True iff the very next execution of this pipeline lands within FLAKE_WINDOW and
    succeeds - i.e. a fail-then-pass retry, not merely some unrelated success nearby.
    A pipeline that just happens to run often would otherwise produce false positives
    on "any success in the window"."""
    window = (execution.started_at, execution.started_at + FLAKE_WINDOW)
    later = source.list_executions(window, {"pipeline_id": execution.pipeline_id})
    candidates = sorted(
        (s for s in later if s.id != execution.id and s.started_at > execution.started_at),
        key=lambda s: s.started_at,
    )
    return bool(candidates) and candidates[0].status == ExecutionStatus.SUCCESS


def fleet_correlate(source: ExecutionSource, execution: Execution) -> BlastRadius:
    start = execution.started_at - FLEET_WINDOW
    end = execution.started_at + FLEET_WINDOW
    peers = source.list_executions((start, end))
    shared_template = [
        s for s in peers
        if s.id != execution.id and s.status == ExecutionStatus.FAILED
        and s.template_ref == execution.template_ref
    ]
    shared_connector = [
        s for s in peers
        if s.id != execution.id and s.status == ExecutionStatus.FAILED
        and s.connector_ref == execution.connector_ref
    ]
    if shared_template:
        return BlastRadius(len(shared_template) + 1, f"template:{execution.template_ref.name}")
    if shared_connector:
        return BlastRadius(len(shared_connector) + 1, f"connector:{execution.connector_ref}")
    return BlastRadius(1, None)


@dataclass(frozen=True)
class DeterministicResult:
    execution_id: str
    fingerprint: str
    matched_fault_id: str | None
    classification: Classification | None
    layer: Layer | None
    blast_radius: BlastRadius
    is_flake: bool
    evidence: tuple[Evidence, ...]
    rule: SignatureRule | None
    resolved: bool  # True => confident, closed-loop deterministic answer; no LLM needed
    escalate_reason: str | None = None


def classify(source: ExecutionSource, execution_id: str) -> DeterministicResult:
    execution = source.get_execution(execution_id)

    gated = status_gate(execution)
    if gated is not None:
        return DeterministicResult(
            execution_id=execution_id, fingerprint=f"status:{execution.status.value}",
            matched_fault_id=None, classification=gated, layer=Layer.L5,
            blast_radius=BlastRadius(1, None), is_flake=False,
            evidence=(Evidence("status_gate", f"execution status={execution.status.value}",
                                gated.value),),
            rule=None, resolved=True,
        )

    leaves = source.get_failed_leaf_nodes(execution_id)
    if not leaves:
        return DeterministicResult(
            execution_id=execution_id, fingerprint="no-failed-leaf", matched_fault_id=None,
            classification=None, layer=None, blast_radius=BlastRadius(1, None),
            is_flake=False, evidence=(), rule=None, resolved=False,
            escalate_reason="no failed leaf node found",
        )
    node = leaves[0]
    log = source.get_step_logs(execution_id, node.id, LOG_MATCH_BUDGET)
    sig = match_signature(log.text)
    fp = fingerprint(execution, node, sig)

    is_flake = flake_check(source, execution)
    blast = fleet_correlate(source, execution)

    evidence = [Evidence("get_failed_leaf_nodes", f"failed at step '{node.step_type}'",
                          "layer/step localization")]
    if sig:
        evidence.append(Evidence("get_step_logs",
                                  f"log matches known signature '{sig.fault_id}'",
                                  "classification"))
    if is_flake:
        evidence.append(Evidence("list_executions",
                                  "same pipeline succeeded within 10 minutes of this failure",
                                  "flake"))
    if blast.executions_affected > 1:
        evidence.append(Evidence("list_executions",
                                  f"{blast.executions_affected} executions failing on "
                                  f"{blast.shared_dimension} within 30 minutes",
                                  "fleet correlation"))

    if is_flake:
        flake_rule = SignatureRule(
            fault_id=sig.fault_id if sig else "unmatched",
            classification=Classification.TRANSIENT, tier=Tier.TIER_0,
            action="retry_execution", gate=Gate.AUTO, reversible=True,
            rationale="fingerprint has a recent fail-then-pass history",
        )
        return DeterministicResult(
            execution_id=execution_id, fingerprint=fp,
            matched_fault_id=sig.fault_id if sig else None,
            classification=Classification.TRANSIENT, layer=sig.layer if sig else Layer.L1,
            blast_radius=blast, is_flake=True, evidence=tuple(evidence), rule=flake_rule,
            resolved=True,
        )

    rule = RULES_BY_FAULT_ID.get(sig.fault_id) if sig else None
    classification = rule.classification if rule else None
    if blast.executions_affected > 1 and classification == Classification.USER:
        classification = Classification.PLATFORM  # fleet correlation flips ownership

    # Tier 0 is the only tier that can close the loop with zero further reasoning: no
    # PR content to draft, no approval evidence bundle to assemble. Tier 1-3 need the
    # agent even when the classification itself is confident.
    resolved = rule is not None and rule.tier == Tier.TIER_0
    if resolved:
        escalate_reason = None
    elif rule is not None and rule.tier is not None:
        escalate_reason = (
            f"deterministic layer identified candidate action '{rule.action}' "
            f"(tier {rule.tier.value}); agent must produce gate-ready evidence/content"
        )
    elif rule is not None:
        escalate_reason = (
            f"'{sig.fault_id}' classified as {rule.classification.value}: "
            f"{rule.rationale} - no automated remediation applies"
        )
    elif sig is not None:
        escalate_reason = f"no confident automated remediation for signature '{sig.fault_id}'"
    else:
        escalate_reason = "log text did not match a known signature"

    return DeterministicResult(
        execution_id=execution_id, fingerprint=fp,
        matched_fault_id=sig.fault_id if sig else None,
        classification=classification, layer=sig.layer if sig else None,
        blast_radius=blast, is_flake=False, evidence=tuple(evidence), rule=rule,
        resolved=resolved, escalate_reason=escalate_reason,
    )


def to_report(det: DeterministicResult) -> Report:
    """Emits the SS10 output contract for a deterministically RESOLVED result. Only
    meaningful when det.resolved is True - callers should route unresolved results to
    the agent loop (drift_gate.agent.loop.run_agent) instead."""
    remediation = None
    if det.rule is not None and det.rule.tier is not None:
        remediation = Remediation(
            tier=det.rule.tier, action=det.rule.action, rationale=det.rule.rationale,
            reversible=bool(det.rule.reversible), gate=det.rule.gate, dry_run={},
            context={"execution_id": det.execution_id},
        )
    return Report(
        execution_id=det.execution_id, fingerprint=det.fingerprint, duplicate_of=None,
        classification=det.classification or Classification.UNKNOWN,
        layer=det.layer or Layer.L4,
        confidence=0.95,
        blast_radius=det.blast_radius, evidence=det.evidence,
        suspected_change=None, remediation=remediation,
        abstained=remediation is None, escalation_reason=det.escalate_reason,
    )
