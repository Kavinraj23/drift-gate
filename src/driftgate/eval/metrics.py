"""The two headline metrics of the verification loop (PRD "Evaluation"), as functions over finished cases.

- Remediation success rate: of the cases where DriftGate attempted a remediation (a Tier 0 action executed or a
  Tier 3 pull request proposed, in either round), how many were verified.
- Recovery rate: of the cases whose first attempt failed verification, how many ended well after re-investigation.
  "Well" means a verified fix, or an escalation where the ground-truth label says a human is the right answer
  (a correct escalation is the right recovery for a failure the agent may not fix). Anything else is not recovered.

Reads ground truth, so it lives in `eval/` (invariant 8). M9a builds the eval table from these functions.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from driftgate.eval.ground_truth import FailureLabel
from driftgate.orchestrator import ESCALATED, EXECUTED, PR_PROPOSED, Outcome

RECOVERED_FIX = "recovered (verified fix)"
RECOVERED_ESCALATION = "recovered (correct escalation)"
NOT_RECOVERED = "not recovered"
GATE_BLOCKED = "first fix blocked by gate"


@dataclass(frozen=True)
class Rate:
    numerator: int
    denominator: int

    @property
    def value(self) -> float | None:
        return self.numerator / self.denominator if self.denominator else None

    def __str__(self) -> str:
        return f"{self.numerator}/{self.denominator}" + (f" = {self.value:.2f}" if self.denominator else "")


Case = tuple[FailureLabel, Outcome]


def attempted_remediation(outcome: Outcome) -> bool:
    return outcome.first_attempt_failed or outcome.kind in (EXECUTED, PR_PROPOSED)


def recovery_text(label: FailureLabel, outcome: Outcome) -> str:
    """Empty when no first attempt failed and nothing was blocked; otherwise how the case ended."""
    if not outcome.first_attempt_failed:
        return GATE_BLOCKED if label.first_fix_fails and outcome.kind == ESCALATED else ""
    if outcome.verified is True:
        return RECOVERED_FIX
    if outcome.kind == ESCALATED and label.disposition == "escalate":
        return RECOVERED_ESCALATION
    return NOT_RECOVERED


def remediation_success_rate(cases: Iterable[Case]) -> Rate:
    attempted = [o for _, o in cases if attempted_remediation(o)]
    return Rate(sum(o.verified is True for o in attempted), len(attempted))


def recovery_rate(cases: Iterable[Case]) -> Rate:
    failed_first = [(label, o) for label, o in cases if o.first_attempt_failed]
    return Rate(sum(recovery_text(label, o) != NOT_RECOVERED for label, o in failed_first), len(failed_first))
