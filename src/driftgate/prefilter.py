"""Deterministic pre-filter that closes governance outcomes and user aborts before any model call."""

from __future__ import annotations

from dataclasses import dataclass

from driftgate.domain import Execution, Node, Report
from driftgate.tools.base import fingerprint

# Terminal states that are normal outcomes, never remediated. Mapped from provider status strings; the
# synthetic generator emits only `approval_rejected`, the rest are the PRD's other closing outcomes.
GOVERNANCE_STATUSES = frozenset({"approval_rejected", "approval_expired"})
ABORT_STATUSES = frozenset({"aborted", "cancelled"})
TERMINAL_NODE_STATUSES = ("rejected", "expired", "aborted", "cancelled")


@dataclass(frozen=True)
class PrefilterResult:
    closed: bool
    report: Report | None = None
    reason: str = ""


def _gated_step(node: Node | None) -> str:
    """Name of the deepest leaf carrying a terminal-outcome status, or '' when there is none."""
    if node is None:
        return ""
    for child in node.children:
        found = _gated_step(child)
        if found:
            return found
    return node.name if node.status in TERMINAL_NODE_STATUSES and not node.children else ""


def prefilter(execution: Execution) -> PrefilterResult:
    """Close governance outcomes (approval rejected/expired) and user aborts. Keyed on execution status only.

    Everything else is `closed=False` and goes on to the investigator (or the baseline). No model is involved.
    """
    status = execution.status
    if status in GOVERNANCE_STATUSES:
        classification, layer = "governance", "L5"
        hypothesis = f"Execution ended in a governance outcome ({status}); this is a normal terminal state."
    elif status in ABORT_STATUSES:
        classification, layer = "user", "L5"
        hypothesis = f"Execution was {status} by a user; this is a normal terminal state."
    else:
        return PrefilterResult(False)
    step = _gated_step(execution.root) or "execution"
    report = Report(
        execution_id=execution.execution_id,
        fingerprint=fingerprint(status, execution.pipeline, step),
        classification=classification,
        layer=layer,
        confidence=1.0,
        hypothesis=hypothesis,
        escalation_reason=f"closed by pre-filter: {status}; never remediated",
    )
    return PrefilterResult(True, report, f"status {status}")
