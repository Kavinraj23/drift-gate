"""The closed set of remediation actions per tier (PRD "Remediation catalog"). Anything else is not proposable."""

from __future__ import annotations

CATALOG: dict[int, tuple[str, ...]] = {
    0: ("rerun_failed_job", "rerun_workflow", "clear_stale_cache"),
    1: ("recycle_runner", "refresh_connector_token", "reschedule_pod", "bump_pool_capacity"),
    2: ("force_unlock_state",),
    3: (
        "regenerate_lockfile",
        "pin_provider_version",
        "revert_template_bump",
        "fix_undefined_variable",
        "fix_secret_scope",
        "fix_requirement_pin",
    ),
}

#: Tier 1 actions act on one shared platform resource; this names the execution ref that identifies it.
TIER1_DIMENSION: dict[str, str] = {
    "recycle_runner": "runner_pool",
    "refresh_connector_token": "connector",
    "reschedule_pod": "runner_pool",
    "bump_pool_capacity": "runner_pool",
}


def is_catalog_action(tier: int, action: str) -> bool:
    return action in CATALOG.get(tier, ())
