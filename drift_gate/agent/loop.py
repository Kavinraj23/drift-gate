"""The LLM reasoning loop (PRD.md SS8 step 8, SS13): a direct Anthropic tool-use loop,
no framework. Runs only on the residual the deterministic classifier
(drift_gate.classifier) couldn't confidently close out on its own, with steps 1-4's
findings handed in as the start of its evidence bundle - it reasons on top of that,
not from scratch.
"""
from __future__ import annotations

import json

import anthropic

from drift_gate.agent.tools import TOOLS, dispatch
from drift_gate.classifier import DeterministicResult
from drift_gate.domain import Classification, Evidence, Gate, Layer, Remediation, Report, Tier
from drift_gate.protocols import ExecutionSource

MODEL = "claude-sonnet-5"
MAX_TURNS = 8

SYSTEM_PROMPT = """\
You are the investigation-and-remediation agent for drift-gate, a CI pipeline failure \
triage system. You are handed one execution that a cheap deterministic classifier \
could NOT confidently close out on its own - it is the residual, so treat the \
deterministic findings you're given as a starting evidence bundle, not the final \
answer.

Safety rules, non-negotiable:
- Every evidence item's "source" must be a tool you actually called this run. A claim \
that can't cite a real tool result gets rejected before it reaches a human.
- Tier 2+ remediation eligibility requires deterministic, verifiable evidence - your \
own confidence is never sufficient to qualify a Tier 2 action.
- If nothing you found supports a safe automated action, set abstained=true and give \
an escalation_reason rather than guessing. Declining to act is correct far more often \
than it feels like it should be - a wrong action is worse than an abstention.
- For a Tier 3 (pull_request gate) remediation, put the actual fix content in \
remediation.pr_diff - write the concrete file change, not just a description of it.
- Gather evidence with tools first, then call submit_report exactly once as your \
final action.
"""


def _evidence_to_domain(items: list[dict]) -> tuple[Evidence, ...]:
    return tuple(Evidence(e["source"], e["finding"], e["supports"]) for e in items)


def _remediation_to_domain(d: dict | None) -> Remediation | None:
    if d is None:
        return None
    return Remediation(
        tier=Tier(d["tier"]), action=d["action"], rationale=d["rationale"],
        reversible=d.get("reversible", True), gate=Gate(d["gate"]),
        dry_run={}, context={"pr_diff": d["pr_diff"]} if d.get("pr_diff") else {},
    )


def run_agent(
    source: ExecutionSource, execution_id: str, det: DeterministicResult,
    client: anthropic.Anthropic | None = None,
) -> tuple[Report, list[dict]]:
    """Returns (report, transcript). transcript is the raw message list - handy for
    printing/debugging the demo, not used by callers otherwise."""
    client = client or anthropic.Anthropic()

    det_evidence_json = [
        {"source": e.source, "finding": e.finding, "supports": e.supports}
        for e in det.evidence
    ]
    # Sources the agent is allowed to cite: what the deterministic layer already
    # established, plus whatever tools it calls itself during this run.
    allowed_sources = {e.source for e in det.evidence}

    opening = (
        f"Investigate execution `{execution_id}`.\n\n"
        f"Deterministic layer findings (fingerprint: {det.fingerprint}):\n"
        f"{json.dumps(det_evidence_json, indent=2)}\n\n"
        f"matched_fault_signature: {det.matched_fault_id}\n"
        f"blast_radius: {det.blast_radius.executions_affected} executions, "
        f"shared_dimension={det.blast_radius.shared_dimension}\n"
        f"deterministic layer's reason for escalating: {det.escalate_reason}\n\n"
        "Use the tools to gather whatever additional evidence you need, then call "
        "submit_report."
    )

    messages: list[dict] = [{"role": "user", "content": opening}]
    report: Report | None = None

    for _ in range(MAX_TURNS):
        response = client.messages.create(
            model=MODEL, max_tokens=4096, system=SYSTEM_PROMPT,
            tools=TOOLS, messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "submit_report":
                evidence_items = block.input.get("evidence", [])
                bad = [e for e in evidence_items if e["source"] not in allowed_sources]
                if bad:
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id, "is_error": True,
                        "content": (
                            f"Rejected: evidence source(s) "
                            f"{[e['source'] for e in bad]} don't match any tool "
                            f"actually called this run. Allowed sources so far: "
                            f"{sorted(allowed_sources)}. Call the relevant tool "
                            f"first, or fix the source field."
                        ),
                    })
                    continue
                remediation = block.input.get("remediation")
                report = Report(
                    execution_id=execution_id, fingerprint=det.fingerprint,
                    duplicate_of=None,
                    classification=Classification(block.input["classification"]),
                    layer=Layer(block.input["layer"]),
                    confidence=block.input["confidence"],
                    blast_radius=det.blast_radius,
                    evidence=_evidence_to_domain(evidence_items),
                    suspected_change=(
                        {"description": block.input["suspected_change"]}
                        if block.input.get("suspected_change") else None
                    ),
                    remediation=_remediation_to_domain(remediation),
                    abstained=block.input.get("abstained", False),
                    escalation_reason=block.input.get("escalation_reason"),
                )
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": "report accepted",
                })
                continue

            result = dispatch(source, block.name, block.input)
            allowed_sources.add(block.name)
            tool_results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        messages.append({"role": "user", "content": tool_results})
        if report is not None:
            break

    if report is None:
        raise RuntimeError(
            f"agent did not submit a report for {execution_id} within {MAX_TURNS} turns"
        )
    return report, messages
