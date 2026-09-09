"""Domain model shared by every ExecutionSource/RemediationTarget implementation.

Real GitHub Actions data and synthetic generated data both get normalized into these
same types before anything else in the system sees them (PRD.md SS9).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Layer(str, Enum):
    L0 = "L0"  # never started
    L1 = "L1"  # step infrastructure
    L2 = "L2"  # identity and secrets
    L3 = "L3"  # pipeline definition
    L4 = "L4"  # the actual work
    L5 = "L5"  # governance
    L6 = "L6"  # post-execution / stalled


class Classification(str, Enum):
    PLATFORM = "platform"
    USER = "user"
    TRANSIENT = "transient"
    GOVERNANCE = "governance"
    UNKNOWN = "unknown"


class Tier(int, Enum):
    TIER_0 = 0  # idempotent, auto-executable
    TIER_1 = 1  # bounded platform actions
    TIER_2 = 2  # state-touching, high risk
    TIER_3 = 3  # code changes as pull requests


class Gate(str, Enum):
    AUTO = "auto"
    SINGLE_APPROVAL = "single_approval"
    DUAL_APPROVAL = "dual_approval"
    PULL_REQUEST = "pull_request"


class NodeStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    RUNNING = "running"


class ExecutionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    RUNNING = "running"
    ABORTED = "aborted"
    REJECTED = "rejected"  # governance: approval rejected


@dataclass(frozen=True)
class TemplateRef:
    name: str
    version: str


@dataclass(frozen=True)
class Node:
    """One node in an execution's step tree. Leaves are the actual work; interior
    nodes are stages that only report pass/fail based on their children."""

    id: str
    name: str
    step_type: str
    status: NodeStatus
    parent_id: str | None
    started_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True)
class Execution:
    id: str
    pipeline_id: str
    status: ExecutionStatus
    started_at: datetime
    ended_at: datetime | None
    connector_ref: str
    template_ref: TemplateRef
    runner_pool: str
    infra_ref: str
    trigger: str
    nodes: tuple[Node, ...] = field(default_factory=tuple)

    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    def children_of(self, node_id: str | None) -> list[Node]:
        return [n for n in self.nodes if n.parent_id == node_id]


@dataclass(frozen=True)
class ExecutionSummary:
    """Lightweight projection of an Execution, for list_executions."""

    id: str
    pipeline_id: str
    status: ExecutionStatus
    started_at: datetime
    connector_ref: str
    template_ref: TemplateRef
    runner_pool: str


@dataclass(frozen=True)
class LogChunk:
    text: str
    truncated: bool
    char_budget: int
    source_node_id: str


@dataclass(frozen=True)
class BlastRadius:
    executions_affected: int
    shared_dimension: str | None


@dataclass(frozen=True)
class Evidence:
    source: str
    finding: str
    supports: str


@dataclass(frozen=True)
class Remediation:
    tier: Tier
    action: str
    rationale: str
    reversible: bool
    gate: Gate
    dry_run: dict = field(default_factory=dict)
    # Whatever identifiers the specific action needs to target the right thing -
    # e.g. {"execution_id": ...} for a retry, {"lock_id": ...} for a force-unlock.
    # Kept generic since it varies by tier/action rather than being fixed fields.
    context: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DryRunResult:
    remediation: Remediation
    would_succeed: bool
    preview: str
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    remediation: Remediation
    succeeded: bool
    detail: str
    executed_at: datetime


@dataclass(frozen=True)
class VerificationResult:
    remediation: Remediation
    fingerprint_recurred: bool
    resolved: bool
    detail: str


@dataclass(frozen=True)
class Report:
    """The output contract, PRD.md SS10."""

    execution_id: str
    fingerprint: str
    duplicate_of: str | None
    classification: Classification
    layer: Layer
    confidence: float
    blast_radius: BlastRadius
    evidence: tuple[Evidence, ...]
    suspected_change: dict | None
    remediation: Remediation | None
    abstained: bool
    escalation_reason: str | None
