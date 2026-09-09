"""RemediationTarget implementation simulated against the generated Population.

Deliberately minimal - protocol-complete, not the real gate/audit system (that's
Phase 3). What it DOES need to do faithfully is decide whether a remediation would
actually work, which means consulting ground_truth.json internally: that file is the
simulated world's own physics (whether force-unlocking a lock actually clears it),
not an answer key being handed to a classifier. A real GitHubActionsTarget wouldn't
need this because reality itself supplies the answer when the action runs; a
simulated target has to look it up. The rule "don't read ground_truth.json" is about
what the CLASSIFIER/AGENT can see (through ExecutionSource), not about what the
simulated environment is allowed to know about itself.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from drift_gate.dataset_io import load_ground_truth
from drift_gate.domain import DryRunResult, ExecutionResult, Remediation, VerificationResult


class SyntheticTarget:
    def __init__(self, data_dir: Path | str = "data/synthetic"):
        self.data_dir = Path(data_dir)
        self._ground_truth = {g["execution_id"]: g for g in load_ground_truth(self.data_dir)}
        self._resolved: dict[str, bool] = {}

    def _correct_action(self, execution_id: str | None) -> str | None:
        if execution_id is None:
            return None
        gt = self._ground_truth.get(execution_id)
        if gt is None or gt["correct_remediation"] is None:
            return None
        return gt["correct_remediation"]["action"]

    def dry_run(self, action: Remediation) -> DryRunResult:
        execution_id = action.context.get("execution_id")
        would_succeed = action.action == self._correct_action(execution_id)
        return DryRunResult(
            remediation=action,
            would_succeed=would_succeed,
            preview=f"would run '{action.action}' for execution {execution_id}",
            details={"execution_id": execution_id},
        )

    def execute(self, action: Remediation) -> ExecutionResult:
        execution_id = action.context.get("execution_id")
        succeeded = action.action == self._correct_action(execution_id)
        if execution_id is not None:
            self._resolved[execution_id] = succeeded
        detail = (
            f"'{action.action}' resolved execution {execution_id}" if succeeded
            else f"'{action.action}' did not resolve execution {execution_id}"
        )
        return ExecutionResult(
            remediation=action, succeeded=succeeded, detail=detail,
            executed_at=datetime.now(timezone.utc),
        )

    def verify(self, action: Remediation) -> VerificationResult:
        execution_id = action.context.get("execution_id")
        resolved = self._resolved.get(execution_id, False)
        return VerificationResult(
            remediation=action, fingerprint_recurred=not resolved, resolved=resolved,
            detail=f"execution {execution_id} resolved={resolved}",
        )
