"""The two headline metrics of the verification loop (PRD "Evaluation"), as functions over finished cases.

- Remediation success rate: of the cases where DriftGate attempted a remediation (a Tier 0 action executed or a
  Tier 3 pull request proposed, in either round), how many were verified.
- Recovery rate (PRD: "after a failed first attempt, did re-investigation FIX it"): of the cases whose first attempt
  failed verification, how many ended in a verified fix. Only a verified fix counts.
- Reported separately, never folded into the recovery rate: correct escalations after a failed attempt (the ground
  truth says a human is the right answer), cases not recovered, and cases where the real gate blocked a wrong first
  fix before it ran (there was no failed attempt to recover from).

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


@dataclass(frozen=True)
class RecoveryBreakdown:
    """Where the cases with a failed first attempt ended, plus the wrong first fixes the gate stopped."""

    fixed: Rate  # recovered by a verified fix: THE recovery rate
    correct_escalation: Rate  # escalated, and the label says a human is the right answer
    not_recovered: Rate
    gate_blocked: Rate  # first_fix_fails scenarios whose wrong first fix the real gate stopped before it ran


def remediation_success_rate(cases: Iterable[Case]) -> Rate:
    attempted = [o for _, o in cases if attempted_remediation(o)]
    return Rate(sum(o.verified is True for o in attempted), len(attempted))


def _failed_first(cases: Iterable[Case]) -> list[Case]:
    return [(label, o) for label, o in cases if o.first_attempt_failed]


def recovery_breakdown(cases: Iterable[Case]) -> RecoveryBreakdown:
    cases = list(cases)
    failed_first = _failed_first(cases)
    n = len(failed_first)
    texts = [recovery_text(label, o) for label, o in failed_first]
    wrong_fix = [(label, o) for label, o in cases if label.first_fix_fails]
    return RecoveryBreakdown(
        Rate(texts.count(RECOVERED_FIX), n),
        Rate(texts.count(RECOVERED_ESCALATION), n),
        Rate(texts.count(NOT_RECOVERED), n),
        Rate(sum(recovery_text(label, o) == GATE_BLOCKED for label, o in wrong_fix), len(wrong_fix)),
    )


def recovery_rate(cases: Iterable[Case]) -> Rate:
    """PRD definition: of the cases whose first attempt failed, how many did re-investigation FIX (verified)."""
    return recovery_breakdown(cases).fixed


def recovery_or_correct_escalation_rate(cases: Iterable[Case]) -> Rate:
    """The earlier, looser figure: a verified fix OR a correct escalation counts. Not the PRD's recovery rate."""
    b = recovery_breakdown(cases)
    return Rate(b.fixed.numerator + b.correct_escalation.numerator, b.fixed.denominator)
