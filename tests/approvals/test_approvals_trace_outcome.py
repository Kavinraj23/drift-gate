"""N5 x N4: the trace report reads the real audit entries of each way `execute_approved` cancels a request."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from test_approvals_flow import DEAD, approve_all, run_execute
from test_approvals_round2 import FailingDryRun

from driftgate.approval_flow import execute_approved
from driftgate.eval.ground_truth import FailureLabel
from driftgate.eval.tier12_cases import World
from driftgate.report import build_pages, group_traces
from driftgate.report.trace import derive_outcome

MakeWorld = Callable[..., World]
CANCELLED_LABEL = "Approval cancelled: gate refused at execution (not executed)"


def outcome_of(world: World) -> Any:
    traces = group_traces(world.orch.audit.read())
    assert len(traces) == 1
    return derive_outcome(traces[0])


def test_kill_switch_flipped_after_approval(make_world: MakeWorld, tier1_label: FailureLabel) -> None:
    w = make_world(tier1_label)
    approve_all(w, "alice")
    assert outcome_of(w).kind == "approved"
    w.kill_switch.write_text(json.dumps({"global": False, "tiers": {"0": True, "1": False, "2": True, "3": True}}))
    run_execute(w)
    assert w.store.get(w.request_id).status == "cancelled"  # type: ignore[arg-type]
    assert outcome_of(w).label == CANCELLED_LABEL
    run_execute(w)  # a duplicate execute on the cancelled request changes nothing
    assert outcome_of(w).label == CANCELLED_LABEL


def test_holder_became_live_after_approval(make_world: MakeWorld, tier2_label: FailureLabel) -> None:
    w = make_world(tier2_label, DEAD)
    approve_all(w, "alice", "bob")
    w.table.locks[w.lock_id].alive = True
    run_execute(w)
    assert outcome_of(w).label == CANCELLED_LABEL


def test_dry_run_failure(make_world: MakeWorld, tier1_label: FailureLabel) -> None:
    w = make_world(tier1_label)
    approve_all(w, "alice")
    execute_approved(
        w.orch,
        w.store,
        w.request_id,  # type: ignore[arg-type]
        clock=w.clock,
        target=FailingDryRun(w.orch.target),  # type: ignore[arg-type]
    )
    assert outcome_of(w).label == CANCELLED_LABEL


def test_execute_on_a_still_pending_request_stays_awaiting_approval(
    make_world: MakeWorld, tier1_label: FailureLabel
) -> None:
    w = make_world(tier1_label)
    run_execute(w)
    assert w.store.get(w.request_id).status == "pending"  # type: ignore[arg-type]
    assert outcome_of(w).label == "Awaiting human approval"


def test_duplicate_execute_after_executed_stays_executed(make_world: MakeWorld, tier1_label: FailureLabel) -> None:
    w = make_world(tier1_label)
    approve_all(w, "alice")
    assert run_execute(w).status == "executed"
    first = outcome_of(w)
    run_execute(w)
    assert outcome_of(w) == first and first.kind in ("verified", "executed")


def test_index_page_shows_the_cancelled_label(make_world: MakeWorld, tier1_label: FailureLabel) -> None:
    w = make_world(tier1_label)
    approve_all(w, "alice")
    w.kill_switch.write_text(json.dumps({"global": False, "tiers": {"0": True, "1": True, "2": True, "3": True}}))
    run_execute(w)
    assert w.store.get(w.request_id).status == "cancelled"  # type: ignore[arg-type]
    idx = build_pages(w.orch.audit.read())["index.html"]
    assert CANCELLED_LABEL in idx
