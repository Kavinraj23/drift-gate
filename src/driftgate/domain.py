"""Normalized domain types, provider protocols and the Report output contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

CLASSIFICATIONS = ("platform", "user", "transient", "governance", "unknown")
LAYERS = ("L0", "L1", "L2", "L3", "L4", "L5", "L6")
GATES = ("auto", "single_approval", "dual_approval", "pull_request")
GATE_DECISIONS = ("allowed", "downgraded", "refused")
REVIEW_VERDICTS = ("approve", "revise", "reject")
TIERS = (0, 1, 2, 3)


@dataclass
class Node:
    """A node in the execution tree; useful information lives in the deepest failed leaf."""

    node_id: str
    name: str
    status: str
    parent_id: str | None = None
    children: list[Node] = field(default_factory=list)
    error_summary: str = ""


@dataclass
class Execution:
    execution_id: str
    pipeline: str
    status: str
    started_at: str
    finished_at: str | None = None
    root: Node | None = None
    # Join keys for fleet correlation: connector, template version, runner pool, infra definition.
    refs: dict[str, str] = field(default_factory=dict)


@dataclass
class ExecutionSummary:
    execution_id: str
    pipeline: str
    status: str
    started_at: str
    refs: dict[str, str] = field(default_factory=dict)


@dataclass
class LogChunk:
    execution_id: str
    node_id: str
    text: str
    truncated: bool = False


@dataclass
class FileContent:
    repo: str
    path: str
    ref: str
    content: str
    truncated: bool = False


@dataclass
class Remediation:
    tier: int
    action: str
    rationale: str
    reversible: bool
    gate: str
    gate_decision: str = "allowed"
    dry_run: dict[str, Any] = field(default_factory=dict)


@dataclass
class DryRunResult:
    ok: bool
    description: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionResult:
    ok: bool
    description: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationResult:
    verified: bool
    description: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class BlastRadius:
    executions_affected: int = 0
    shared_dimension: str = ""


@dataclass
class Evidence:
    source: str  # must be a tool call id from the same run (invariant 7)
    finding: str
    supports: str


@dataclass
class SuspectedChange:
    kind: str
    ref: str
    at: str
    basis: str


@dataclass
class Review:
    verdict: str
    comments: str


@dataclass
class RunStats:
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0


@dataclass
class Report:
    """The full output contract from the PRD."""

    execution_id: str
    fingerprint: str
    classification: str
    layer: str
    confidence: float
    hypothesis: str
    duplicate_of: str | None = None
    blast_radius: BlastRadius = field(default_factory=BlastRadius)
    evidence: list[Evidence] = field(default_factory=list)
    suspected_change: SuspectedChange | None = None
    remediation: Remediation | None = None
    review: Review | None = None
    prior_attempts: list[dict[str, Any]] = field(default_factory=list)
    abstained: bool = False
    budget_truncated: bool = False
    escalation_reason: str = ""
    run: RunStats = field(default_factory=RunStats)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SourceError(Exception):
    """An ExecutionSource could not satisfy a request (unknown id, missing file, refused path).

    Messages never contain file or log contents.
    """


class ExecutionSource(Protocol):
    def get_execution(self, execution_id: str) -> Execution: ...

    def get_failed_leaf_nodes(self, execution_id: str) -> list[Node]: ...

    def get_step_logs(self, execution_id: str, node_id: str, budget: int) -> LogChunk: ...

    def list_executions(self, window: Any, filter: Any) -> list[ExecutionSummary]: ...

    def read_file(self, repo: str, path: str, ref: str, max_bytes: int) -> FileContent: ...


class RemediationTarget(Protocol):
    def dry_run(self, action: Remediation) -> DryRunResult: ...

    def execute(self, action: Remediation) -> ExecutionResult: ...

    def verify(self, action: Remediation) -> VerificationResult: ...
