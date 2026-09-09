"""Maps each error signature to its ground-truth classification and remediation.

This is the answer key the generator injects deliberately (PRD.md SS11 "every
generated failure carries its ground-truth label and its correct remediation") and
which lives only in ground_truth.json, never in the data ExecutionSource serves.

Not every fault has a safe automatic remediation - some correctly resolve to
escalation (remediation=None), which matters for the "escalation precision" metric
(PRD.md SS12): declining to act is sometimes the right call, not a gap to fill in.
"""
from __future__ import annotations

from dataclasses import dataclass

from drift_gate.domain import Classification, Gate, Tier


@dataclass(frozen=True)
class FaultProfile:
    fault_id: str
    classification: Classification
    tier: Tier | None
    action: str | None
    gate: Gate | None
    reversible: bool | None
    rationale: str | None


PROFILES: tuple[FaultProfile, ...] = (
    # --- Tier 0: idempotent, auto-executable retries ---
    FaultProfile(
        "image_pull_backoff", Classification.PLATFORM, Tier.TIER_0, "retry_execution",
        Gate.AUTO, True, "registry pull failures are frequently a transient blip",
    ),
    FaultProfile(
        "step_timeout", Classification.TRANSIENT, Tier.TIER_0, "retry_execution",
        Gate.AUTO, True, "no evidence of a real hang; retry cheaply first",
    ),
    FaultProfile(
        "registry_429", Classification.TRANSIENT, Tier.TIER_0, "retry_execution",
        Gate.AUTO, True, "rate limit clears on its own; retrying is idempotent",
    ),
    FaultProfile(
        "cloud_throttling", Classification.TRANSIENT, Tier.TIER_0, "retry_execution",
        Gate.AUTO, True, "cloud API throttling is self-resolving on retry",
    ),
    # --- Tier 1: bounded platform actions ---
    FaultProfile(
        "expired_credential", Classification.PLATFORM, Tier.TIER_1,
        "refresh_connector_token", Gate.SINGLE_APPROVAL, True,
        "credential source is still valid, token just needs refreshing",
    ),
    FaultProfile(
        "registry_401", Classification.PLATFORM, Tier.TIER_1,
        "refresh_connector_token", Gate.SINGLE_APPROVAL, True,
        "anonymous token expired, refreshing the connector token resolves it",
    ),
    # --- Tier 2: state-touching, high risk ---
    FaultProfile(
        "state_lock_stale_holder", Classification.PLATFORM, Tier.TIER_2,
        "force_unlock_state", Gate.DUAL_APPROVAL, False,
        "lock holder execution is in a terminal state; provable dead holder",
    ),
    # --- Tier 3: code changes as pull requests ---
    FaultProfile(
        "expression_null", Classification.USER, Tier.TIER_3,
        "fix_undefined_variable_expression", Gate.PULL_REQUEST, True,
        "expression references a variable that no longer exists; fix is a diff",
    ),
    FaultProfile(
        "lockfile_mismatch", Classification.USER, Tier.TIER_3,
        "regenerate_lockfile", Gate.PULL_REQUEST, True,
        "provider version constraint changed; lockfile just needs regenerating",
    ),
    FaultProfile(
        "provider_version_drift", Classification.USER, Tier.TIER_3,
        "pin_provider_version", Gate.PULL_REQUEST, True,
        "upgrade broke on an unpinned constraint; pinning the prior version fixes it",
    ),
    # --- No safe automatic remediation: escalate ---
    FaultProfile(
        "oom_killed", Classification.USER, None, None, None, None,
        "resource sizing is an owner decision, not a platform action",
    ),
    FaultProfile(
        "assume_role_denied", Classification.PLATFORM, None, None, None, None,
        "trust policy is broken, not just an expired token; needs human fix",
    ),
    FaultProfile(
        "invalid_yaml", Classification.USER, None, None, None, None,
        "syntax error needs a human edit; too ambiguous to auto-patch safely",
    ),
    FaultProfile(
        "template_not_found", Classification.PLATFORM, None, None, None, None,
        "template registry issue; needs platform investigation, not a guess",
    ),
    FaultProfile(
        "policy_denial", Classification.GOVERNANCE, None, None, None, None,
        "policy denials are a governance outcome, never remediated",
    ),
    FaultProfile(
        "iam_denied", Classification.USER, None, None, None, None,
        "requires a permission grant only the resource owner can authorize",
    ),
)

BY_FAULT_ID: dict[str, FaultProfile] = {p.fault_id: p for p in PROFILES}

# Fault pools by classification, used to hit the 60/25/15 user/platform/transient
# split among non-governance failures (PRD.md SS11).
USER_FAULTS = tuple(p.fault_id for p in PROFILES if p.classification == Classification.USER)
PLATFORM_FAULTS = tuple(
    p.fault_id for p in PROFILES if p.classification == Classification.PLATFORM
)
TRANSIENT_FAULTS = tuple(
    p.fault_id for p in PROFILES if p.classification == Classification.TRANSIENT
)
GOVERNANCE_FAULTS = tuple(
    p.fault_id for p in PROFILES if p.classification == Classification.GOVERNANCE
)
