"""The two interfaces every CI provider adapter implements (PRD.md SS9).

The agent never talks to a specific CI provider directly - only through these. Anything
written against ExecutionSource/RemediationTarget works unchanged against
SyntheticSource/Target today and GitHubActionsSource/Target later.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from drift_gate.domain import (
    DryRunResult,
    Execution,
    ExecutionResult,
    ExecutionSummary,
    LogChunk,
    Node,
    Remediation,
    VerificationResult,
)


@runtime_checkable
class ExecutionSource(Protocol):
    def get_execution(self, execution_id: str) -> Execution: ...

    def get_failed_leaf_nodes(self, execution_id: str) -> list[Node]: ...

    def get_step_logs(self, execution_id: str, node_id: str, budget: int) -> LogChunk: ...

    def list_executions(
        self, window: tuple[datetime, datetime], filter: dict | None = None
    ) -> list[ExecutionSummary]: ...


@runtime_checkable
class RemediationTarget(Protocol):
    def dry_run(self, action: Remediation) -> DryRunResult: ...

    def execute(self, action: Remediation) -> ExecutionResult: ...

    def verify(self, action: Remediation) -> VerificationResult: ...
