"""Per-execution pipeline: pre-filter -> investigator -> deterministic gate facts -> SafetyGate -> target -> audit.

This is the only module that wires a `RemediationTarget` implementation (here the synthetic one); nothing
under `agents/` or `tools/` imports one (invariant 1). The agent decides, this code verifies:

- the facts SafetyGate needs (signature match, flake precedent, known-transient rule, blast radius, attempt
  history, shared resources, set sizes) are derived here by calling the deterministic tools directly, never
  from what the model said it saw;
- the model's blast radius and fingerprint are not used; the report carries the ones computed here;
- only an `allowed` decision reaches the target; Tier 3 stops at "PR proposed" (M6 adds the reviewer and the
  real PR path). Agents never merge; branches are prefixed `driftgate/`.

Extension points, deliberately empty in M5:
- `tier3_review` (M6): called with the investigation and the allowed Tier 3 proposal, returns a `Review`.
  `approve` lets the PR proposal stand; anything else escalates.
- `after_execution` (M7): called with the finished `Outcome` of every executed or proposed remediation, the
  place to verify the next execution and start the single re-investigation round.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from driftgate.adapters.synthetic import SimulatedOutcome, SyntheticSource, SyntheticTarget
from driftgate.agents.investigator import Investigation, Investigator, Proposal
from driftgate.audit import AuditLog, record_decision
from driftgate.domain import (
    BlastRadius,
    DryRunResult,
    ExecutionResult,
    ExecutionSource,
    Remediation,
    RemediationTarget,
    Report,
    Review,
    RunStats,
)
from driftgate.gates import DEFAULT_KILL_SWITCH_PATH, GateDecision, GateFacts, KillSwitch, RateLimiter, SafetyGate
from driftgate.llm.budget import InvestigationBudget
from driftgate.llm.config import InvestigationLimits
from driftgate.llm.types import ModelClient
from driftgate.prefilter import prefilter
from driftgate.remediation_catalog import TIER1_DIMENSION, is_catalog_action
from driftgate.tier3 import (
    PR_BASE_BRANCH,
    DiffCheck,
    PullRequest,
    PullRequestRecord,
    PullRequestTarget,
    build_pr_description,
    check_diff,
    is_protected,
)
from driftgate.tools import ToolContext, ToolResult, dispatch
from driftgate.tools.attempts import AttemptStore, RemediationAttempt
from driftgate.tools.base import FailureAnalyzer
from driftgate.tools.fleet_correlate import FLEET_MIN_EXECUTIONS

#: SafetyGate's abort ceiling is "blast radius over N executions". N is a human decision (BLOCKERS.md); the
#: PRD says fleet-wide incidents escalate, and fleet_correlate defines fleet-wide as 3+ executions, so the
#: orchestrator constructs the gate with N = that threshold minus one. Callers may override.
FLEET_ABORT_CEILING = FLEET_MIN_EXECUTIONS - 1
BRANCH_PREFIX = "driftgate/"

# Outcome kinds.
CLOSED = "closed"  # the pre-filter closed it; no model call
EXECUTED = "executed"  # Tier 0 passed the gate, dry run and execution
AWAITING_APPROVAL = "awaiting_approval"  # Tier 1/2 passed the gate and dry run; approval is outside the MVP
PR_PROPOSED = "pr_proposed"  # Tier 3 passed the gate; a human reviews and merges
ESCALATED = "escalated"

ModelFactory = Callable[[str, InvestigationBudget], ModelClient]
Tier3Review = Callable[[Investigation, Remediation], Review | None]
AfterExecution = Callable[["Outcome"], None]
Precheck = Callable[[Investigation], str]  # M7: a non-empty return refuses the re-investigation's proposal


@dataclass
class Outcome:
    execution_id: str
    kind: str
    report: Report
    reason: str = ""
    investigation: Investigation | None = None
    gate: GateDecision | None = None
    dry_run: DryRunResult | None = None
    execution: ExecutionResult | None = None
    gate_tool_results: list[ToolResult] = field(default_factory=list)
    # M7 (verification and re-investigation). `rounds` is 2 once a re-investigation ran; `attempt_id` names the
    # attempt recorded for this outcome; `first_attempt_failed` stays True on the final outcome of a two-round run.
    attempt_id: str = ""
    rounds: int = 1
    verified: bool | None = None
    first_attempt_failed: bool = False
    earlier_model_calls: int = 0
    earlier_tool_call_ids: list[str] = field(default_factory=list)  # round-1 ids that round-1 evidence cites
    earlier_tool_results: list[ToolResult] = field(default_factory=list)  # round-1 tool results, kept (not dropped)
    first_round: Outcome | None = None  # snapshot of the round-1 outcome once a re-investigation replaced it
    pull_request: PullRequest | None = None  # Tier 3 only, set when a PR was opened
    pr_record: PullRequestRecord | None = None
    revisions: int = 0  # Tier 3 revision rounds used (max 1)
    #: True only when a pull request really exists on a provider (a non-simulated record). A simulated target, or no
    #: PR-capable target, leaves this False and the outcome is a PR *proposal* (kind `pr_proposed`) only.
    pr_opened: bool = False

    @property
    def model_calls(self) -> int:
        return self.earlier_model_calls + (self.investigation.model_calls if self.investigation else 0)

    @property
    def tool_calls(self) -> int:
        return self.report.run.tool_calls

    @property
    def tokens(self) -> int:
        return self.report.run.input_tokens + self.report.run.output_tokens


_SHA = re.compile(r"[0-9a-f]{40}")


def branch_name(fingerprint: str, action: str) -> str:
    return f"{BRANCH_PREFIX}{fingerprint}-{action}"


def _unsafe_path(path: str) -> bool:
    """Empty, absolute, drive-lettered or parent-escaping paths are not repository-relative."""
    p = PurePosixPath(path.replace("\\", "/"))
    drive = len(path) > 1 and path[1] == ":" and path[0].isalpha()
    return not path or p.is_absolute() or ".." in p.parts or drive


class Orchestrator:
    def __init__(
        self,
        source: ExecutionSource,
        target: RemediationTarget,
        gate: SafetyGate,
        audit: AuditLog,
        model_factory: ModelFactory,
        *,
        limits: InvestigationLimits | None = None,
        attempts: AttemptStore | None = None,
        model: str = "",
        tier3_review: Tier3Review | None = None,
        after_execution: AfterExecution | None = None,
        allow_unreviewed_tier3: bool = False,
    ) -> None:
        self._source = source
        self._target = target
        self._gate = gate
        self._audit = audit
        self._model_factory = model_factory
        self._limits = limits or InvestigationLimits()
        self.attempts = attempts or AttemptStore()
        self._ctx = ToolContext(source, self.attempts)
        self._model = model
        self._tier3_review = tier3_review
        self._after_execution = after_execution
        # Explicit opt-out used only by the offline sandbox/tests that exercise the M5 path. Production builders
        # (`build_github_orchestrator`) never set it: with no independent reviewer, Tier 3 escalates.
        self._allow_unreviewed_tier3 = allow_unreviewed_tier3
        self._distinct: dict[str, int] = {}

    # -- public ------------------------------------------------------------------------------
    @property
    def audit(self) -> AuditLog:
        return self._audit

    @property
    def target(self) -> RemediationTarget:
        return self._target

    @property
    def kill_switch(self) -> KillSwitch:
        return self._gate.kill_switch

    @property
    def analyzer(self) -> FailureAnalyzer:
        return self._ctx.analyzer

    def handle(self, execution_id: str) -> Outcome:
        ex = self._source.get_execution(execution_id)
        pre = prefilter(ex)
        if pre.closed and pre.report is not None:
            self._audit.append(
                "rejected", pre.report.fingerprint, execution_id, stage="prefilter", reason=pre.report.escalation_reason
            )
            return Outcome(execution_id, CLOSED, pre.report, pre.report.escalation_reason)
        if ex.status != "failed":
            raise ValueError(f"{execution_id} has status {ex.status!r}; only failed executions are investigated")

        return self._run_round(execution_id, InvestigationBudget(self._limits))

    def reinvestigate(self, first: Outcome, context: str, precheck: Precheck | None = None) -> Outcome:
        """The single re-investigation round (M7): a fresh investigation of the same execution with `context` (the
        failed attempt as new evidence) appended to the user message. The round shares round 1's budget object: the
        whole case, both rounds together, is capped at the per-investigation tool-call and token limits, and round 2
        gets only what round 1 left (the caller already counted the round with `begin_reinvestigation`). It goes
        through the same gate, shared rate limiter, kill switch and abort ceiling as the first. The caller merges
        the two outcomes."""
        if first.investigation is not None:
            budget = first.investigation.budget
        else:  # no round-1 investigation to share with: a fresh budget that still cannot start a third round
            budget = InvestigationBudget(self._limits)
            budget.reinvestigations = 1
        return self._run_round(
            first.execution_id, budget, context, rounds=2, precheck=precheck, revisions=first.revisions
        )

    def _run_round(
        self,
        execution_id: str,
        budget: InvestigationBudget,
        context: str = "",
        *,
        rounds: int = 1,
        precheck: Precheck | None = None,
        revisions: int = 0,
    ) -> Outcome:
        investigator = Investigator(self._model_factory(execution_id, budget), self._ctx, budget, model=self._model)
        if rounds >= 2 and budget.exhausted:  # the case's allowance is spent: no model call is made, fail closed
            inv = investigator.spent(execution_id, _spent_stop(budget))
        else:
            inv = investigator.investigate(execution_id, context)
        report = inv.report

        gate_results, analysis, fleet, problem = self._deterministic_facts(execution_id)
        if analysis is not None:
            report.fingerprint = str(analysis["fingerprint"])
        if fleet is not None:
            report.blast_radius = BlastRadius(int(fleet["executions_affected"]), str(fleet["shared_dimension"]))
        fingerprint = report.fingerprint or f"unclassified:{execution_id}"
        out = Outcome(
            execution_id,
            ESCALATED,
            report,
            investigation=inv,
            gate_tool_results=gate_results,
            rounds=rounds,
            revisions=revisions,
        )

        self._audit_proposal(inv, fingerprint, None)
        if inv.report.budget_truncated:
            self._audit.append(
                "budget_truncated",
                fingerprint,
                execution_id,
                stop_reason=inv.stop_reason,
                tool_calls=report.run.tool_calls,
            )
        if precheck is not None and (why := precheck(inv)):
            report.remediation = replace(inv.proposal.remediation, gate_decision="refused") if inv.proposal else None
            return self._escalate(out, fingerprint, why, "reinvestigation")
        if inv.proposal is None or report.abstained:
            return self._escalate(
                out, fingerprint, report.escalation_reason or "the agent proposed no remediation", "agent"
            )
        if problem:
            report.remediation = replace(inv.proposal.remediation, gate_decision="refused")
            return self._escalate(out, fingerprint, problem, "facts")
        assert analysis is not None and fleet is not None

        proposal = inv.proposal
        invalid = self._validate(proposal)
        if invalid:
            report.remediation = replace(proposal.remediation, gate_decision="refused")
            return self._escalate(out, fingerprint, invalid, "validation")

        facts = self._gate_facts(execution_id, proposal, analysis, fleet, fingerprint, report, gate_results)
        self._audit_proposal(inv, fingerprint, facts)
        decision = self._gate.decide(proposal.remediation, facts)
        out.gate = decision
        record_decision(self._audit, decision, fingerprint, execution_id)
        report.remediation = decision.remediation
        if not decision.allowed:
            return self._escalate(out, fingerprint, decision.reason, "gate")
        return self._act(out, proposal, decision, fingerprint)

    def attach_after_execution(self, hook: AfterExecution) -> None:
        """Install the M7 hook after construction (it needs the orchestrator it re-enters)."""
        self._after_execution = hook

    # -- steps -------------------------------------------------------------------------------
    def _act(self, out: Outcome, proposal: Proposal, decision: GateDecision, fingerprint: str) -> Outcome:
        rem = decision.remediation
        eid = out.execution_id
        if rem.tier == 3:
            return self._propose_pr(out, proposal, rem, fingerprint)

        rem.dry_run["execution_id"] = eid  # set by code, so a target never relies on a model-supplied id
        dry = self._target.dry_run(rem)
        out.dry_run = dry
        self._audit.append(
            "execution", fingerprint, eid, stage="dry_run", ok=dry.ok, description=dry.description, tier=rem.tier
        )
        rem.dry_run["dry_run"] = {"ok": dry.ok, "description": dry.description, "details": dry.details}
        if not dry.ok:
            self._audit.append("failed", fingerprint, eid, stage="dry_run", description=dry.description)
            return self._escalate(out, fingerprint, f"dry run failed: {dry.description}", None)
        if rem.tier != 0:
            # Tier 1/2: gated, dry-run and audited with stubbed execution; the approval step is outside the MVP.
            out.kind = AWAITING_APPROVAL
            out.reason = f"awaiting {rem.gate} (dry run ok)"
            return self._after(out)

        result = self._target.execute(rem)
        out.execution = result
        self._audit.append(
            "execution", fingerprint, eid, stage="execute", ok=result.ok, description=result.description, tier=0
        )
        rem.dry_run["execution"] = {"ok": result.ok, "description": result.description}
        out.attempt_id = f"{eid}:attempt-{len(self.attempts.by_fingerprint(fingerprint)) + 1}"
        self.attempts.add(
            RemediationAttempt(
                attempt_id=out.attempt_id,
                fingerprint=fingerprint,
                execution_id=eid,
                tier=0,
                action=rem.action,
                outcome="executed" if result.ok else "failed",
            )
        )
        if not result.ok:
            self._audit.append("failed", fingerprint, eid, stage="execute", description=result.description)
            return self._escalate(out, fingerprint, f"execution failed: {result.description}", None)
        out.kind = EXECUTED
        out.reason = result.description
        return self._after(out)

    def _propose_pr(self, out: Outcome, proposal: Proposal, rem: Remediation, fingerprint: str) -> Outcome:
        eid = out.execution_id
        branch = branch_name(fingerprint, rem.action)
        assert branch.startswith(BRANCH_PREFIX)
        rem.dry_run.update({"state": "pr_proposed", "branch": branch, "paths": list(proposal.paths)})
        if self._tier3_review is None and not self._allow_unreviewed_tier3:
            # Fail closed: a Tier 3 change is never opened without the independent reviewer.
            reason = "no independent Tier 3 reviewer is wired; refusing to open a PR"
            return self._escalate(out, fingerprint, reason, "review")
        if self._tier3_review is None or out.investigation is None:  # explicit sandbox opt-out: the M5 behaviour
            self._audit.append(
                "execution",
                fingerprint,
                eid,
                stage="pr_proposed",
                branch=branch,
                paths=list(proposal.paths),
                merged=False,
            )
            out.kind = PR_PROPOSED
            out.reason = f"pull request proposed on {branch}; a human reviews and merges"
            return self._after(out)
        return self._tier3_flow(out, proposal, rem, fingerprint)

    # -- Tier 3: deterministic diff checks, independent review, one revision round, then the PR ---------------
    def _tier3_flow(self, out: Outcome, proposal: Proposal, rem: Remediation, fingerprint: str) -> Outcome:
        eid = out.execution_id
        hook = self._tier3_review
        assert hook is not None and out.investigation is not None
        reset = getattr(hook, "reset_round", None)
        if callable(reset) and out.revisions == 0:  # a revision already used in round 1 is not forgotten in round 2
            reset()
        while True:
            check = self._check_diff(out, proposal, rem, fingerprint)
            if not check.ok:
                return self._escalate(out, fingerprint, f"diff check failed: {check.reason}", "diff_check")
            review = hook(out.investigation, rem)
            if review is None:  # fail closed: no verdict is a rejection, never an approval
                review = Review("reject", "the reviewer returned no verdict")
            out.report.run = RunStats(**out.investigation.budget.run_fields())  # reviewer usage is on the shared budget
            out.report.review = review
            self._audit.append(
                "proposal",
                fingerprint,
                eid,
                stage="tier3_review",
                round=out.revisions,
                verdict=review.verdict,
                comments=review.comments,
            )
            if review.verdict == "approve":
                break
            if review.verdict == "revise" and out.revisions == 0:
                out.revisions = 1  # the single revision round (PRD): a second `revise` escalates below
                revised = self._revise(out, proposal, review, fingerprint)
                if revised is None:
                    return out
                proposal, rem = revised
                continue
            capped = " (revision limit reached)" if review.verdict == "revise" else ""
            return self._escalate(
                out, fingerprint, f"reviewer verdict {review.verdict}{capped}: {review.comments}", "review"
            )
        return self._open_pr(out, proposal, rem, fingerprint, check)

    def _check_diff(self, out: Outcome, proposal: Proposal, rem: Remediation, fingerprint: str) -> DiffCheck:
        ex = self._source.get_execution(out.execution_id)
        check = check_diff(
            proposal.diff,
            action=rem.action,
            declared_paths=proposal.paths,
            source=self._source,
            repo=ex.pipeline,
            ref=ex.refs.get("commit", "main"),
        )
        rem.dry_run["diff_text"] = proposal.diff
        if check.diff is not None:
            rem.dry_run["diff"] = check.diff.to_dict()
        self._audit.append(
            "proposal",
            fingerprint,
            out.execution_id,
            stage="tier3_diff",
            ok=check.ok,
            violations=[{"code": v.code, "message": v.message, "path": v.path} for v in check.violations],
            files=list(check.diff.paths) if check.diff else [],
            added=check.diff.added if check.diff else 0,
            removed=check.diff.removed if check.diff else 0,
        )
        return check

    def _revise(
        self, out: Outcome, proposal: Proposal, review: Review, fingerprint: str
    ) -> tuple[Proposal, Remediation] | None:
        """Send the reviewer's feedback to the investigator once; re-validate and re-gate what comes back."""
        eid = out.execution_id
        self._audit.append("proposal", fingerprint, eid, stage="tier3_revision", feedback=review.comments)
        assert out.investigation is not None
        budget = out.investigation.budget  # the one case budget: the revision draws on what round 1 left
        investigator = Investigator(self._model_factory(eid, budget), self._ctx, budget, model=self._model)
        if budget.exhausted:  # nothing left for a revision: no model call is made, fail closed
            inv = investigator.spent(eid, _spent_stop(budget))
        else:
            inv = investigator.investigate(eid, revision_context(review.comments, proposal.diff))
        report = inv.report
        report.review = out.report.review
        out.report, out.investigation = report, inv
        gate_results, analysis, fleet, problem = self._deterministic_facts(eid)
        if analysis is not None:
            report.fingerprint = str(analysis["fingerprint"])
        if fleet is not None:
            report.blast_radius = BlastRadius(int(fleet["executions_affected"]), str(fleet["shared_dimension"]))
        out.gate_tool_results = gate_results
        self._audit_proposal(inv, fingerprint, None)
        if inv.proposal is None or report.abstained:
            reason = report.escalation_reason or "the revision proposed no remediation"
            self._escalate(out, fingerprint, reason, "agent")
            return None
        if problem or analysis is None or fleet is None:
            report.remediation = replace(inv.proposal.remediation, gate_decision="refused")
            self._escalate(out, fingerprint, problem, "facts")
            return None
        new = inv.proposal
        invalid = self._validate(new) or ("" if new.remediation.tier == 3 else "the revision is not a Tier 3 change")
        if invalid:
            report.remediation = replace(new.remediation, gate_decision="refused")
            self._escalate(out, fingerprint, invalid, "validation")
            return None
        facts = self._gate_facts(eid, new, analysis, fleet, fingerprint, report, gate_results)
        self._audit_proposal(inv, fingerprint, facts)
        decision = self._gate.decide(new.remediation, facts)
        out.gate = decision
        record_decision(self._audit, decision, fingerprint, eid)
        report.remediation = decision.remediation
        if not decision.allowed:
            self._escalate(out, fingerprint, decision.reason, "gate")
            return None
        rem = decision.remediation
        branch = branch_name(fingerprint, rem.action)
        rem.dry_run.update({"state": "pr_proposed", "branch": branch, "paths": list(new.paths)})
        return new, rem

    def _open_pr(
        self, out: Outcome, proposal: Proposal, rem: Remediation, fingerprint: str, check: DiffCheck
    ) -> Outcome:
        assert check.diff is not None
        eid = out.execution_id
        branch = str(rem.dry_run["branch"])
        body = build_pr_description(
            execution_id=eid,
            fingerprint=fingerprint,
            action=rem.action,
            hypothesis=out.report.hypothesis,
            rationale=rem.rationale,
            evidence=out.report.evidence,
            diff=check.diff,
            review=out.report.review,
            revision_rounds=out.revisions,
            branch=branch,
        )
        title = f"DriftGate: {rem.action} ({fingerprint})"
        ex = self._source.get_execution(eid)
        commit = ex.refs.get("commit", "")
        checked = tuple((p, c) for p, c in check.new_contents.items() if c is not None)
        if not checked or len(checked) != len(check.new_contents):
            return self._escalate(out, fingerprint, "no checked file contents to commit", "diff_check")
        rem.dry_run["execution_id"] = eid
        pr = PullRequest(
            branch,
            ex.refs.get("branch", PR_BASE_BRANCH),
            title,
            body,
            proposal.diff,
            proposal.paths,
            files=checked,
            base_sha=commit if _SHA.fullmatch(commit) else "",
        )
        record: PullRequestRecord | None = None
        if isinstance(self._target, PullRequestTarget):
            try:
                record = self._target.open_pull_request(pr)
            except ValueError as e:
                self._audit.append("failed", fingerprint, eid, stage="pr_proposed", description=str(e))
                return self._escalate(out, fingerprint, f"pull request refused: {e}", None)
        out.pull_request, out.pr_record = pr, record
        out.pr_opened = record is not None and not record.simulated
        rem.dry_run["pull_request"] = {
            "number": record.number if record else None,
            "url": record.url if record else None,
            "merged": False,
            "description": body,
        }
        self._audit.append(
            "execution",
            fingerprint,
            eid,
            stage="pr_proposed",
            branch=branch,
            paths=list(proposal.paths),
            merged=False,
            pr_number=record.number if record else None,
            opened=out.pr_opened,
            reviewer_verdict=out.report.review.verdict if out.report.review else None,
            revisions=out.revisions,
        )
        out.kind = PR_PROPOSED
        verb = "opened" if out.pr_opened else "proposed"
        out.reason = f"pull request {verb} on {branch}; a human reviews and merges"
        return self._after(out)

    def _after(self, out: Outcome) -> Outcome:
        if self._after_execution is not None:
            self._after_execution(out)
        return out

    def _escalate(self, out: Outcome, fingerprint: str, reason: str, stage: str | None) -> Outcome:
        out.kind = ESCALATED
        out.reason = reason
        out.report.escalation_reason = reason
        if stage is not None:
            self._audit.append("rejected", fingerprint, out.execution_id, stage=stage, reason=reason)
        return out

    # -- deterministic facts -----------------------------------------------------------------
    def _deterministic_facts(
        self, execution_id: str
    ) -> tuple[list[ToolResult], dict[str, Any] | None, dict[str, Any] | None, str]:
        """Classification and fleet correlation, run here with their own call ids (not the agent's)."""
        results: list[ToolResult] = []
        parsed: dict[str, dict[str, Any] | None] = {}
        for tool in ("classify_signature", "fleet_correlate"):
            r = dispatch(self._ctx, tool, {"execution_id": execution_id}, f"gate:{execution_id}:{tool}")
            results.append(r)
            parsed[tool] = r.data if r.ok else None
        analysis, fleet = parsed["classify_signature"], parsed["fleet_correlate"]
        if analysis is None or not analysis.get("signature"):
            return results, None, fleet, "the failure could not be analyzed deterministically"
        if fleet is None:
            return results, analysis, None, "blast radius could not be determined"
        return results, analysis, fleet, ""

    def _count_distinct(self, dimension: str) -> int:
        if dimension not in self._distinct:
            values = {s.refs.get(dimension) for s in self._source.list_executions(None, None)}
            self._distinct[dimension] = len({v for v in values if v and v != "none"})
        return self._distinct[dimension]

    def _gate_facts(
        self,
        execution_id: str,
        proposal: Proposal,
        analysis: dict[str, Any],
        fleet: dict[str, Any],
        fingerprint: str,
        report: Report,
        gate_results: list[ToolResult],
    ) -> GateFacts:
        rem = proposal.remediation
        ex = self._source.get_execution(execution_id)
        deterministic = bool(analysis["deterministic_match"])
        precedent = False
        if rem.tier == 0:
            flake = dispatch(self._ctx, "flake_history", {"execution_id": execution_id}, f"gate:{execution_id}:flake")
            gate_results.append(flake)
            precedent = bool(flake.ok and flake.data.get("precedent"))

        shared: tuple[str, ...] = ()
        proposed: int | None = None
        total: int | None = None
        if rem.tier == 0:  # a re-run touches the failed jobs of this execution, out of all of its jobs
            proposed = len(self._source.get_failed_leaf_nodes(execution_id))
            total = _count_leaves(ex.root)
        elif rem.tier == 1:
            dim = TIER1_DIMENSION.get(rem.action, "")
            value = ex.refs.get(dim, "")
            if value and value != "none":
                shared, proposed, total = (f"{dim}:{value}",), 1, self._count_distinct(dim)
        elif rem.tier == 2:
            lock_id = proposal.params.get("lock_id")
            shared = (f"state_lock:{lock_id}",) if lock_id else ()
            proposed, total = 1, self._count_distinct("infra_def")
        else:
            proposed = len(proposal.paths)

        history = self.attempts.by_fingerprint(fingerprint)
        return GateFacts(
            fingerprint=fingerprint,
            signature_match=deterministic,
            flake_precedent=precedent,
            known_transient_rule=deterministic and bool(analysis["tier0_rule_exists"]),
            blast_radius=BlastRadius(int(fleet["executions_affected"]), str(fleet["shared_dimension"])),
            shared_resources=shared,
            resource_set_total=total,
            proposed_set_size=proposed,
            prior_tier0_failed=any(a.tier == 0 and a.verification.get("verified") is False for a in history),
            lock_holder=None,  # no lock table is reachable from here, so holder death is never proven
            holder_death_proof=False,
            dry_run_ok=False,  # nothing runs before the gate
            agent_confidence=report.confidence,
        )

    @staticmethod
    def _validate(proposal: Proposal) -> str:
        rem = proposal.remediation
        if not is_catalog_action(rem.tier, rem.action):
            return f"action {rem.action!r} is not in the Tier {rem.tier} catalog"
        if rem.tier == 3 and any(_unsafe_path(p) for p in proposal.paths):
            return "proposed paths must be relative paths inside the repository"
        if rem.tier == 3 and any(is_protected(p) for p in proposal.paths):
            return "proposed paths include protected files (secrets, credentials or guard configuration)"
        return ""

    # -- audit -------------------------------------------------------------------------------
    def _audit_proposal(self, inv: Investigation, fingerprint: str, facts: GateFacts | None) -> None:
        """Without facts: the agent's output. With facts: what the gate was given for that proposal."""
        rem = inv.proposal.remediation if inv.proposal else None
        if facts is not None:
            self._audit.append(
                "proposal",
                fingerprint,
                inv.report.execution_id,
                stage="gate_facts",
                action=rem.action if rem else None,
                facts=asdict(facts),
            )
            return
        self._audit.append(
            "proposal",
            fingerprint,
            inv.report.execution_id,
            stage="agent",
            action=rem.action if rem else None,
            tier=rem.tier if rem else None,
            gate=rem.gate if rem else None,
            paths=list(inv.proposal.paths) if inv.proposal else [],
            confidence=inv.report.confidence,
            abstained=inv.report.abstained,
            budget_truncated=inv.report.budget_truncated,
            stop_reason=inv.stop_reason,
            evidence_kept=len(inv.report.evidence),
            evidence_dropped=inv.evidence_dropped,
            duplicate_ids_rejected=inv.duplicate_ids_rejected,
            tool_call_ids=inv.tool_call_ids,
        )


def _spent_stop(budget: InvestigationBudget) -> str:
    return "budget_tokens" if budget.tokens_exhausted else "budget_tool_calls"


def revision_context(feedback: str, previous_diff: str) -> str:
    """Appended to the investigator's first message when the reviewer asks for a revision."""
    return (
        "An independent reviewer asked for a revision of your previous Tier 3 diff.\n"
        f"Reviewer comments: {feedback}\n"
        f"Your previous diff:\n{previous_diff}\n"
        "Investigate again if you need to, then submit a corrected report with a revised `diff`. "
        "If no safe change exists, propose no remediation."
    )


def _count_leaves(node: Any) -> int:
    if node is None:
        return 0
    if not node.children:
        return 1
    return sum(_count_leaves(c) for c in node.children)


def make_synthetic_target(data_dir: Path | str, *, verified: bool = True) -> RemediationTarget:
    """A synthetic target backed by the dataset's follow-ups. `verified=False` makes every verification fail, the
    way a re-run that does not hold would. The one place outside `adapters/` that constructs the target."""
    return SyntheticTarget(default=SimulatedOutcome(verified=verified), data_dir=data_dir)


def build_synthetic_orchestrator(
    data_dir: Path | str,
    model_factory: ModelFactory,
    *,
    audit_path: Path | str,
    clock: Callable[[], float],
    kill_switch_path: Path | str = DEFAULT_KILL_SWITCH_PATH,
    abort_ceiling: int = FLEET_ABORT_CEILING,
    limits: InvestigationLimits | None = None,
    target: SyntheticTarget | None = None,
    model: str = "",
    tier3_review: Tier3Review | None = None,
    after_execution: AfterExecution | None = None,
    allow_unreviewed_tier3: bool = False,
    verification: bool = False,
) -> Orchestrator:
    """Wire the synthetic sandbox: SyntheticSource + SyntheticTarget, a fresh rate limiter, audit log and gate.

    `verification=True` installs the M7 loop (verify the next execution, one re-investigation round) as the
    `after_execution` hook and gives the default target the dataset's follow-ups. It is opt-in so callers that
    count target calls (the M5 tests) keep their exact behaviour; `eval/e2e.py` turns it on.
    """
    gate = SafetyGate(KillSwitch(kill_switch_path), RateLimiter(clock), abort_ceiling)
    orch = Orchestrator(
        SyntheticSource(data_dir),
        target or SyntheticTarget(data_dir=data_dir if verification else None),
        gate,
        AuditLog(audit_path, clock),
        model_factory,
        limits=limits,
        model=model,
        tier3_review=tier3_review,
        after_execution=after_execution,
        allow_unreviewed_tier3=allow_unreviewed_tier3,
    )
    if verification and after_execution is None:
        from driftgate.verify import install_verification  # local import: verify depends on this module

        install_verification(orch)
    return orch


def build_github_orchestrator(
    source: ExecutionSource,
    target: RemediationTarget,
    model_factory: ModelFactory,
    *,
    audit_path: Path | str,
    clock: Callable[[], float],
    tier3_review: Tier3Review,
    kill_switch_path: Path | str = DEFAULT_KILL_SWITCH_PATH,
    abort_ceiling: int = FLEET_ABORT_CEILING,
    limits: InvestigationLimits | None = None,
    model: str = "",
    after_execution: AfterExecution | None = None,
) -> Orchestrator:
    """Wire a real provider (the GitHub adapter). A reviewer is mandatory and the unreviewed opt-out is never set."""
    gate = SafetyGate(KillSwitch(kill_switch_path), RateLimiter(clock), abort_ceiling)
    return Orchestrator(
        source,
        target,
        gate,
        AuditLog(audit_path, clock),
        model_factory,
        limits=limits,
        model=model,
        tier3_review=tier3_review,
        after_execution=after_execution,
    )


__all__ = [
    "AWAITING_APPROVAL",
    "BRANCH_PREFIX",
    "CLOSED",
    "EXECUTED",
    "ESCALATED",
    "FLEET_ABORT_CEILING",
    "PR_PROPOSED",
    "Orchestrator",
    "Outcome",
    "branch_name",
    "build_github_orchestrator",
    "build_synthetic_orchestrator",
    "make_synthetic_target",
    "revision_context",
]
