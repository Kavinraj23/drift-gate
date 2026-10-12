"""Tier 1 and Tier 2 never reach the GitHub adapter: it refuses them, with no HTTP call, even when 'allowed'."""

from __future__ import annotations

import pytest
from gh_helpers import FakeSession

from driftgate.adapters.github_actions import GitHubActionsTarget
from driftgate.domain import Remediation
from driftgate.gates import TIER_GATE


@pytest.mark.parametrize(
    "tier, action",
    [
        (1, "recycle_runner"),
        (1, "refresh_connector_token"),
        (1, "bump_pool_capacity"),
        (2, "force_unlock_state"),
        (2, "force_unlock"),
    ],
)
def test_github_target_refuses_tier1_and_tier2_even_when_the_gate_said_allowed(
    target: GitHubActionsTarget, session: FakeSession, tier: int, action: str
) -> None:
    rem = Remediation(
        tier, action, "r", tier == 1, TIER_GATE[tier], "allowed", {"execution_id": "7001.1", "lock_id": "x"}
    )
    assert not target.dry_run(rem).ok
    assert not target.execute(rem).ok
    assert not target.verify(rem).verified
    assert session.calls == []  # not one request left the adapter
