"""Deterministic cheapest-first classifier used as the evaluation baseline, with no model calls.

Order: status gate (pre-filter) -> signature -> fleet correlation -> flake check. It can only ever
propose Tier 0 (a re-run); it cannot write a diff, so it escalates every Tier 1-3 case. It reads the
same tools as the agent, through the same dispatcher.
"""

from __future__ import annotations

from dataclasses import dataclass

from driftgate.domain import BlastRadius, Evidence, Remediation, Report, RunStats
from driftgate.prefilter import prefilter
from driftgate.tools import ToolContext, ToolResult, dispatch

TIER0_ACTION = "rerun_failed_job"
GENERIC_SIGNATURE = "exit_code_nonzero"


@dataclass(frozen=True)
class BaselineResult:
    report: Report
    closed_by_prefilter: bool
    tool_calls: int

    @property
    def would_remediate(self) -> bool:
        return self.report.remediation is not None

    @property
    def tier(self) -> int | None:
        """Effective tier the baseline would act at; None means escalate or close."""
        return self.report.remediation.tier if self.report.remediation else None


class _Calls:
    def __init__(self, ctx: ToolContext, execution_id: str) -> None:
        self._ctx = ctx
        self._execution_id = execution_id
        self.results: list[ToolResult] = []

    def call(self, name: str) -> ToolResult:
        call_id = f"{self._execution_id}:baseline-{len(self.results) + 1}"
        r = dispatch(self._ctx, name, {"execution_id": self._execution_id}, call_id)
        self.results.append(r)
        return r


def _tier0(rationale: str) -> Remediation:
    return Remediation(
        tier=0, action=TIER0_ACTION, rationale=rationale, reversible=True, gate="auto", gate_decision="allowed"
    )


def run_baseline(ctx: ToolContext, execution_id: str) -> BaselineResult:
    ex = ctx.source.get_execution(execution_id)
    pre = prefilter(ex)
    if pre.closed and pre.report is not None:
        return BaselineResult(pre.report, True, 0)
    if ex.status != "failed":
        raise ValueError(f"{execution_id} has status {ex.status!r}; the baseline classifies failures")

    calls = _Calls(ctx, execution_id)
    sig = calls.call("classify_signature")
    if not sig.ok or not sig.data.get("signature"):
        raise ValueError(f"cannot classify {execution_id}: {sig.error or 'no failed step'}")
    d = sig.data
    evidence = [Evidence(sig.source, f"signature {d['signature']} at step {d['step']!r}", d["classification"])]
    classification, layer = d["classification"], d["layer"]
    blast = BlastRadius()
    remediation: Remediation | None = None
    reason = ""

    fleet = calls.call("fleet_correlate")
    if fleet.ok and fleet.data["fleet_wide"]:
        blast = BlastRadius(fleet.data["executions_affected"], fleet.data["shared_dimension"])
        evidence.append(
            Evidence(fleet.source, f"{blast.executions_affected} executions share {blast.shared_dimension}", "platform")
        )
        # A user-class signature repeated across pipelines on one shared dimension is the platform's fault.
        if classification not in ("platform", "transient"):
            classification = "platform"
        hypothesis = f"{blast.executions_affected} executions with {d['signature']} share {blast.shared_dimension}"
        reason = f"fleet-wide blast radius ({blast.shared_dimension}); escalate"
        confidence = 0.85
    elif d["tier0_rule_exists"]:
        remediation = _tier0(f"known-transient rule for {d['signature']}")
        classification = "transient"
        hypothesis = f"{d['signature']} matches a Tier 0 known-transient rule"
        confidence = 0.9
    elif classification in ("transient", "unknown"):
        flake = calls.call("flake_history")
        precedent = bool(flake.ok and flake.data["precedent"])
        evidence.append(Evidence(flake.source, f"fail-then-pass precedent for fingerprint: {precedent}", "transient"))
        if precedent:
            remediation = _tier0("flake precedent for this fingerprint")
            classification = "transient"
            hypothesis = f"{d['signature']} with fail-then-pass history for this fingerprint"
            confidence = 0.8 if d["deterministic_match"] else 0.6
        else:
            if d["signature"] == GENERIC_SIGNATURE:
                # A bare exit code with no precedent: the default reading is a defect in the pipeline's own work.
                classification = "user"
            hypothesis = f"{d['signature']} without flake precedent"
            confidence = 0.5
            reason = "no Tier 0 rule and no flake precedent"
    else:
        hypothesis = f"{d['signature']} ({classification}); the baseline cannot author a fix"
        confidence = 0.8
        reason = f"{classification} failure needs a human or a Tier 1-3 change; the baseline only re-runs"

    report = Report(
        execution_id=execution_id,
        fingerprint=d["fingerprint"],
        classification=classification,
        layer=layer,
        confidence=confidence,
        hypothesis=hypothesis,
        blast_radius=blast,
        evidence=evidence,
        remediation=remediation,
        escalation_reason="" if remediation else reason,
        run=RunStats(tool_calls=len(calls.results)),
    )
    return BaselineResult(report, False, len(calls.results))
