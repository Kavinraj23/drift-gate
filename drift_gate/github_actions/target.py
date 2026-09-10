"""Real RemediationTarget against the GitHub Actions REST API (PRD.md SS11).

Only Tier 0 retry is implemented here - the "never cut" scope per TASKS.md Phase 2.
Tier 3 PR remediation (branch + diff + PR) is a separate target method added when that
phase is built; Tier 1/2 actions are simulated-only for this project (Phase 5) and have
no real-adapter counterpart at all.

dry_run() makes no mutating call - it just confirms the run is in a state a rerun can
apply to (github will 422 a rerun-failed-jobs call on a run that never failed). execute()
is the one real, hard-to-reverse call: POST rerun-failed-jobs actually re-executes CI
on GitHub's infrastructure. verify() re-fetches the run and reports whether the retry
resolved it - "resolved" here means the rerun's conclusion is success, not merely that
it finished.
"""
from __future__ import annotations

from datetime import datetime, timezone

from drift_gate.domain import (
    DryRunResult,
    ExecutionResult,
    ExecutionStatus,
    Remediation,
    VerificationResult,
)
from drift_gate.github_actions.client import GitHubClient
from drift_gate.github_actions.source import GitHubActionsSource

_UNSUPPORTED = "GitHubActionsTarget only implements Tier 0 retry_execution today"


class GitHubActionsTarget:
    def __init__(self, owner: str, repo: str, client: GitHubClient | None = None):
        self.owner = owner
        self.repo = repo
        self.client = client or GitHubClient(owner, repo)
        self.source = GitHubActionsSource(owner, repo, self.client)

    def dry_run(self, action: Remediation) -> DryRunResult:
        execution_id = action.context.get("execution_id")
        if action.action != "retry_execution" or execution_id is None:
            return DryRunResult(
                remediation=action, would_succeed=False, preview=_UNSUPPORTED,
            )
        execution = self.source.get_execution(execution_id)
        would_succeed = execution.status == ExecutionStatus.FAILED
        preview = (
            f"would POST rerun-failed-jobs for run {execution_id} "
            f"(current status={execution.status.value})"
        )
        return DryRunResult(
            remediation=action, would_succeed=would_succeed, preview=preview,
            details={"execution_id": execution_id, "status": execution.status.value},
        )

    def execute(self, action: Remediation) -> ExecutionResult:
        execution_id = action.context.get("execution_id")
        if action.action != "retry_execution" or execution_id is None:
            return ExecutionResult(
                remediation=action, succeeded=False, detail=_UNSUPPORTED,
                executed_at=datetime.now(timezone.utc),
            )
        self.client.post(f"/actions/runs/{execution_id}/rerun-failed-jobs")
        return ExecutionResult(
            remediation=action, succeeded=True,
            detail=f"requested rerun-failed-jobs for run {execution_id}",
            executed_at=datetime.now(timezone.utc),
        )

    def verify(self, action: Remediation) -> VerificationResult:
        execution_id = action.context.get("execution_id")
        if execution_id is None:
            return VerificationResult(
                remediation=action, fingerprint_recurred=True, resolved=False,
                detail=_UNSUPPORTED,
            )
        execution = self.source.get_execution(execution_id)
        resolved = execution.status == ExecutionStatus.SUCCESS
        return VerificationResult(
            remediation=action, fingerprint_recurred=not resolved, resolved=resolved,
            detail=f"run {execution_id} status={execution.status.value} after retry",
        )
