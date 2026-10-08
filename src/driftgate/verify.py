"""Verification of a remediation and hand-off of failed attempts back for re-investigation (M7).

Flow, run by the orchestrator's `after_execution` hook for every executed Tier 0 action and every proposed Tier 3
pull request:

1. `Verifier` asks the target to `verify` the action, that is, to observe the next execution of the same pipeline
   (Tier 0) or the CI run on the PR branch (Tier 3), and checks whether the failure fingerprint recurred. The
   result is recorded on the attempt in the `AttemptStore` (so the investigator can read it through
   `get_remediation_attempt`) and audited.
2. Verified: the attempt is marked verified and the case closes. A green Tier 3 CI run is noted as "verified by CI"
   in the PR record (`remediation.dry_run["ci"]`).
3. Not verified: the attempt is marked failed and a reversible, executed action is rolled back (when the target can).
   Then, at most once, the failed attempt goes back to the investigator as new evidence (`Orchestrator.reinvestigate`)
   and the re-proposal is gated, rate limited and kill-switched exactly like the first. A second failure, or any
   failure after an earlier failed attempt on the same fingerprint, hard-escalates with the original evidence, every
   attempt and the revised reasoning (`Report.prior_attempts`).

The agent decides; this module and the gate verify. Nothing here lets a model raise a tier or skip a check.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from driftgate.agents.investigator import Investigation
from driftgate.audit import AuditLog
from driftgate.domain import RemediationTarget, RunStats
from driftgate.orchestrator import ESCALATED, EXECUTED, PR_PROPOSED, Orchestrator, Outcome
from driftgate.tools.attempts import AttemptStore, RemediationAttempt
from driftgate.tools.base import FailureAnalyzer, fingerprint
from driftgate.tools.signatures import UNKNOWN, match_all

LOG_EXCERPT_CHARS = 4000  # how much of the next execution's log is kept on the attempt
VERIFIED_BY_CI = "verified by CI"


@dataclass(frozen=True)
class Verification:
    verified: bool
    description: str
    recurred: bool | None  # did the failure fingerprint recur in the next execution (None: not observable)
    details: dict[str, Any]

    def as_record(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "description": self.description,
            "recurred": self.recurred,
            "details": self.details,
        }


class Verifier:
    def __init__(
        self, target: RemediationTarget, attempts: AttemptStore, audit: AuditLog, analyzer: FailureAnalyzer
    ) -> None:
        self._target = target
        self._attempts = attempts
        self._audit = audit
        self._analyzer = analyzer

    def verify(self, out: Outcome) -> Verification:
        """Observe, record, audit and (if it did not hold) roll back. Never raises on a failed verification."""
        rem = out.report.remediation
        assert rem is not None
        fp, eid = out.report.fingerprint, out.execution_id
        rem.dry_run.setdefault("execution_id", eid)  # tells the target which failure this action answers
        result = self._target.verify(rem)
        details = dict(result.details)
        recurred = self._recurred(eid, fp, details, rem.tier)
        if "next_log" in details:
            details["next_log"] = str(details["next_log"])[:LOG_EXCERPT_CHARS]
        verified = bool(result.verified) and recurred is not True
        v = Verification(verified, result.description, recurred, details)

        attempt_id = self._ensure_attempt(out)
        self._audit.append(
            "verification",
            fp,
            eid,
            stage="result",
            attempt_id=attempt_id,
            tier=rem.tier,
            action=rem.action,
            verified=verified,
            recurred=recurred,
            description=v.description,
        )
        if rem.tier == 3:
            rem.dry_run["ci"] = {"status": "green" if verified else "red", "note": VERIFIED_BY_CI if verified else ""}
        record = v.as_record()
        if verified:
            self._attempts.update(attempt_id, outcome="verified", verification=record)
            self._audit.append("verification", fp, eid, stage="closed", attempt_id=attempt_id)
            return v
        record["rolled_back"] = self._roll_back(out, attempt_id)
        self._attempts.update(attempt_id, outcome="failed", verification=record)
        return v

    # -- helpers -----------------------------------------------------------------------------
    def _recurred(self, eid: str, fp: str, details: dict[str, Any], tier: int) -> bool | None:
        """Fingerprint of the next execution's failure vs the original; needs the next execution's log text."""
        if tier == 3:
            return None  # a CI run on a branch is not the pipeline's next execution
        if details.get("next_status") == "success":
            return False
        text = details.get("next_log")
        analysis = self._analyzer.analyze(eid)
        if not text or analysis is None:
            return None
        hits = match_all(str(text))
        primary = hits[0] if hits else UNKNOWN
        return fingerprint(primary.id, analysis.pipeline, analysis.step) == fp

    def _ensure_attempt(self, out: Outcome) -> str:
        if out.attempt_id and self._attempts.get(out.attempt_id) is not None:
            return out.attempt_id
        rem = out.report.remediation
        assert rem is not None
        fp = out.report.fingerprint
        out.attempt_id = f"{out.execution_id}:attempt-{len(self._attempts.by_fingerprint(fp)) + 1}"
        self._attempts.add(
            RemediationAttempt(out.attempt_id, fp, out.execution_id, rem.tier, rem.action, "pr_proposed")
        )
        return out.attempt_id

    def _roll_back(self, out: Outcome, attempt_id: str) -> bool:
        """Undo a reversible executed action. A Tier 3 PR was never merged, so it has nothing to undo.

        Rolling back is one subtractive step and nothing is added after it, so additive-before-subtractive
        (invariant 13) holds trivially. It undoes an action the gate already allowed; it is not a new decision.
        """
        rem = out.report.remediation
        assert rem is not None
        fp, eid = out.report.fingerprint, out.execution_id
        rollback = getattr(self._target, "rollback", None)
        if rem.tier == 3:
            reason, done = "pull request is not merged: nothing to roll back", False
        elif not rem.reversible:
            reason, done = "action is irreversible: not rolled back", False
        elif rollback is None:
            reason, done = "target cannot roll back", False
        else:
            res = rollback(rem)
            reason, done = res.description, bool(res.ok)
        self._audit.append(
            "verification", fp, eid, stage="rollback", attempt_id=attempt_id, rolled_back=done, description=reason
        )
        return done


# ---------------------------------------------------------------------------------------------
# The after_execution hook: verify, then at most one re-investigation.
# ---------------------------------------------------------------------------------------------


def reinvestigation_context(fp: str, attempt: RemediationAttempt, hypothesis: str) -> str:
    """The extra user message of a re-investigation. It never touches the system prompt, so recorded fixtures of
    first-round requests keep their replay keys; only re-investigation requests differ."""
    v = attempt.verification
    return (
        "RE-INVESTIGATION. A remediation was applied for this failure and did not hold.\n"
        f"Failed attempt {attempt.attempt_id}: Tier {attempt.tier} {attempt.action}; "
        f"verification: not verified ({v.get('description', 'no description')}).\n"
        f"Failure fingerprint: {fp}. Call get_remediation_attempt with this fingerprint to read the attempt and its "
        "verification result; it is new evidence.\n"
        f"Your first hypothesis was: {hypothesis}\n"
        "Before proposing anything, explain in your new hypothesis why the first one was wrong. Do not repeat the "
        "same action. A Tier 0 re-run of this fingerprint will not be allowed again. If the evidence does not "
        "support another action, propose none and escalate."
    )


def _same_text(a: str, b: str) -> bool:
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


class VerificationLoop:
    """The `after_execution` hook. Mutates the outcome it is given into the case's final outcome."""

    def __init__(self, orchestrator: Orchestrator, verifier: Verifier) -> None:
        self._orch = orchestrator
        self._verifier = verifier
        self._attempts = orchestrator.attempts
        self._audit = orchestrator.audit

    def __call__(self, out: Outcome) -> None:
        if out.kind not in (EXECUTED, PR_PROPOSED) or out.report.remediation is None:
            return  # nothing was executed or proposed (Tier 1/2 await approval): nothing to observe
        v = self._verifier.verify(out)
        out.verified = v.verified
        if v.verified:
            return
        out.first_attempt_failed = True
        fp = out.report.fingerprint
        failed_attempt = self._attempts.get(out.attempt_id)
        assert failed_attempt is not None and out.investigation is not None
        earlier_failures = [
            a for a in self._attempts.by_fingerprint(fp) if a.attempt_id != out.attempt_id and a.outcome == "failed"
        ]
        if out.rounds >= 2:
            return self._hard_escalate(out, "second failure: the re-investigated remediation did not verify")
        if earlier_failures:
            return self._hard_escalate(out, "failure after an earlier failed attempt on the same fingerprint")
        if not out.investigation.budget.begin_reinvestigation():
            return self._hard_escalate(out, "re-investigation budget exhausted")
        self._reinvestigate(out, failed_attempt)

    # -- steps -------------------------------------------------------------------------------
    def _reinvestigate(self, out: Outcome, failed: RemediationAttempt) -> None:
        first_hypothesis = out.report.hypothesis
        first_evidence = list(out.report.evidence)
        first_run = out.report.run
        first_calls = out.model_calls
        first_ids = [*out.earlier_tool_call_ids, *(out.investigation.tool_call_ids if out.investigation else [])]
        self._audit.append(
            "proposal",
            out.report.fingerprint,
            out.execution_id,
            stage="reinvestigation_start",
            failed_attempt=failed.attempt_id,
            action=failed.action,
            tier=failed.tier,
        )

        def precheck(inv: Investigation) -> str:
            if inv.proposal is None:
                return ""
            if not any(r.tool == "get_remediation_attempt" and r.ok for r in inv.tool_results):
                return "re-investigation proposed an action without reading the failed attempt"
            if _same_text(inv.report.hypothesis, first_hypothesis):
                return "re-investigation did not revise the first hypothesis"
            return ""

        ctx = reinvestigation_context(out.report.fingerprint, failed, first_hypothesis)
        new = self._orch.reinvestigate(out, ctx, precheck)

        # One report for both rounds: original evidence first, then the revised round's; run stats summed.
        new.report.evidence = first_evidence + new.report.evidence
        new.report.run = _sum_runs(first_run, new.report.run)
        new.report.prior_attempts = self._prior_attempts(new.report.fingerprint, [first_hypothesis])
        new.report.fingerprint = new.report.fingerprint or out.report.fingerprint
        new.first_attempt_failed = True
        new.rounds = 2
        new.earlier_model_calls = first_calls
        new.earlier_tool_call_ids = first_ids
        for f in fields(Outcome):
            setattr(out, f.name, getattr(new, f.name))

    def _hard_escalate(self, out: Outcome, reason: str) -> None:
        out.kind = ESCALATED
        out.reason = f"{reason} (see prior_attempts)"
        out.report.escalation_reason = out.reason
        out.report.prior_attempts = self._prior_attempts(out.report.fingerprint, [])
        self._audit.append(
            "rejected",
            out.report.fingerprint,
            out.execution_id,
            stage="verification_escalation",
            reason=out.reason,
            attempts=len(out.report.prior_attempts),
        )

    def _prior_attempts(self, fp: str, first_hypotheses: list[str]) -> list[dict[str, Any]]:
        """Every attempt on this fingerprint, oldest first; the first carries the hypothesis that led to it."""
        rows = [a.to_dict() for a in self._attempts.by_fingerprint(fp)]
        for i, row in enumerate(rows):
            row["round"] = i + 1
            if i < len(first_hypotheses):
                row["hypothesis"] = first_hypotheses[i]
        return rows


def _sum_runs(a: RunStats, b: RunStats) -> RunStats:
    return RunStats(
        tool_calls=a.tool_calls + b.tool_calls,
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cost_usd=a.cost_usd + b.cost_usd,
        latency_s=a.latency_s + b.latency_s,
    )


def install_verification(orchestrator: Orchestrator) -> VerificationLoop:
    """Wire the verifier and the loop into an orchestrator as its `after_execution` hook."""
    verifier = Verifier(orchestrator.target, orchestrator.attempts, orchestrator.audit, orchestrator.analyzer)
    loop = VerificationLoop(orchestrator, verifier)
    orchestrator.attach_after_execution(loop)
    return loop
