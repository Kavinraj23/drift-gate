"""Real RemediationTarget against the GitHub Actions REST API (PRD.md SS11).

Two tiers are implemented for real, the "never cut" scope per TASKS.md Phase 2:

Tier 0 retry: dry_run() makes no mutating call - it just confirms the run is in a
state a rerun can apply to (github will 422 a rerun-failed-jobs call on a run that
never failed). execute() is the one real, hard-to-reverse call: POST
rerun-failed-jobs actually re-executes CI on GitHub's infrastructure. verify()
re-fetches the run and reports whether the retry resolved it - "resolved" means the
rerun's conclusion is success, not merely that it finished.

Tier 3 PR: PRD.md SS4 - "the agent opens a branch, writes the diff, opens the PR with
the evidence bundle as the description, and stops. No direct mutation." execute()
does exactly that via the Git Data API (blob -> tree -> commit -> ref -> pull), no
local git needed. Unlike Tier 0, this never "resolves" on its own - a human merges it
- so verify() only ever reports whether the PR exists and is still open, never
resolved=True; that's not a limitation, it's the tier's actual safety property.

Known integration gap, stated not hidden: the agent (drift_gate/agent/loop.py) has no
tool to read a target repo's real file contents yet, so it cannot itself discover
which real file to fix - action.context here is expected to already carry
`file_path`/`new_content` (e.g. supplied by a caller that resolved the fix, or a
future repo-read tool). Verified against a real PR using a hand-built Remediation
against demo/lockfile-fixture.txt, not yet by the agent loop end to end.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone

from drift_gate.domain import (
    DryRunResult,
    ExecutionResult,
    ExecutionStatus,
    Gate,
    Remediation,
    VerificationResult,
)
from drift_gate.github_actions.client import GitHubClient
from drift_gate.github_actions.source import GitHubActionsSource

_UNSUPPORTED = "GitHubActionsTarget only implements Tier 0 retry and Tier 3 PR today"


def _pr_branch_name(action: Remediation) -> str:
    slug = action.context.get("execution_id") or action.context.get("fingerprint") or "fix"
    return f"drift-gate/{action.action}-{slug}"


class GitHubActionsTarget:
    def __init__(self, owner: str, repo: str, client: GitHubClient | None = None):
        self.owner = owner
        self.repo = repo
        self.client = client or GitHubClient(owner, repo)
        self.source = GitHubActionsSource(owner, repo, self.client)

    # --- Tier 0: retry_execution -------------------------------------------------

    def _dry_run_retry(self, action: Remediation) -> DryRunResult:
        execution_id = action.context["execution_id"]
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

    def _execute_retry(self, action: Remediation) -> ExecutionResult:
        execution_id = action.context["execution_id"]
        self.client.post(f"/actions/runs/{execution_id}/rerun-failed-jobs")
        return ExecutionResult(
            remediation=action, succeeded=True,
            detail=f"requested rerun-failed-jobs for run {execution_id}",
            executed_at=datetime.now(timezone.utc),
        )

    def _verify_retry(self, action: Remediation) -> VerificationResult:
        execution_id = action.context["execution_id"]
        execution = self.source.get_execution(execution_id)
        resolved = execution.status == ExecutionStatus.SUCCESS
        return VerificationResult(
            remediation=action, fingerprint_recurred=not resolved, resolved=resolved,
            detail=f"run {execution_id} status={execution.status.value} after retry",
        )

    # --- Tier 3: pull_request -----------------------------------------------------

    def _dry_run_pr(self, action: Remediation) -> DryRunResult:
        file_path = action.context.get("file_path")
        new_content = action.context.get("new_content")
        would_succeed = bool(file_path) and new_content is not None
        preview = (
            f"would open a branch + PR against {self.owner}/{self.repo} replacing "
            f"'{file_path}' - {action.rationale}"
        )
        return DryRunResult(
            remediation=action, would_succeed=would_succeed, preview=preview,
            details={"file_path": file_path},
        )

    def _execute_pr(self, action: Remediation) -> ExecutionResult:
        file_path = action.context["file_path"]
        new_content = action.context["new_content"]
        base_branch = action.context.get("base_branch", "main")
        branch = _pr_branch_name(action)

        base_ref = self.client.get(f"/git/ref/heads/{base_branch}")
        base_sha = base_ref["object"]["sha"]
        base_commit = self.client.get(f"/git/commits/{base_sha}")
        base_tree_sha = base_commit["tree"]["sha"]

        blob = self.client.post("/git/blobs", {
            "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
            "encoding": "base64",
        }).json()

        tree = self.client.post("/git/trees", {
            "base_tree": base_tree_sha,
            "tree": [{"path": file_path, "mode": "100644", "type": "blob",
                       "sha": blob["sha"]}],
        }).json()

        commit = self.client.post("/git/commits", {
            "message": f"drift-gate: {action.rationale}",
            "tree": tree["sha"], "parents": [base_sha],
        }).json()

        self.client.post("/git/refs", {
            "ref": f"refs/heads/{branch}", "sha": commit["sha"],
        })

        pr_body = action.context.get(
            "pr_body",
            f"{action.rationale}\n\nOpened automatically by drift-gate for "
            f"remediation action `{action.action}` (Tier {action.tier.value}). "
            "No direct mutation was made - review and merge to apply.",
        )
        pr = self.client.post("/pulls", {
            "title": f"drift-gate: {action.rationale}",
            "head": branch, "base": base_branch, "body": pr_body,
        }).json()

        return ExecutionResult(
            remediation=action, succeeded=True,
            detail=f"opened PR #{pr['number']}: {pr['html_url']}",
            executed_at=datetime.now(timezone.utc),
        )

    def _verify_pr(self, action: Remediation) -> VerificationResult:
        branch = _pr_branch_name(action)
        prs = self.client.get("/pulls", params={
            "head": f"{self.owner}:{branch}", "state": "all",
        })
        if not prs:
            return VerificationResult(
                remediation=action, fingerprint_recurred=True, resolved=False,
                detail=f"no PR found for branch {branch}",
            )
        pr = prs[0]
        # Tier 3 never self-resolves - a human merges it. "Resolved" here can only
        # ever mean "the artifact exists and is open," not that the fix landed.
        return VerificationResult(
            remediation=action, fingerprint_recurred=pr["state"] != "open",
            resolved=False,
            detail=f"PR #{pr['number']} state={pr['state']} - awaiting human review",
        )

    # --- dispatch -------------------------------------------------------------

    def dry_run(self, action: Remediation) -> DryRunResult:
        if action.action == "retry_execution" and action.context.get("execution_id"):
            return self._dry_run_retry(action)
        if action.gate == Gate.PULL_REQUEST and action.context.get("file_path"):
            return self._dry_run_pr(action)
        return DryRunResult(remediation=action, would_succeed=False, preview=_UNSUPPORTED)

    def execute(self, action: Remediation) -> ExecutionResult:
        if action.action == "retry_execution" and action.context.get("execution_id"):
            return self._execute_retry(action)
        if action.gate == Gate.PULL_REQUEST and action.context.get("file_path"):
            return self._execute_pr(action)
        return ExecutionResult(
            remediation=action, succeeded=False, detail=_UNSUPPORTED,
            executed_at=datetime.now(timezone.utc),
        )

    def verify(self, action: Remediation) -> VerificationResult:
        if action.action == "retry_execution" and action.context.get("execution_id"):
            return self._verify_retry(action)
        if action.gate == Gate.PULL_REQUEST:
            return self._verify_pr(action)
        return VerificationResult(
            remediation=action, fingerprint_recurred=True, resolved=False,
            detail=_UNSUPPORTED,
        )
