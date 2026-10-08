"""TEST DOUBLE: builds a scripted `FakeModel` that plays a competent investigator for a labelled scenario.

It lives in `eval/` because it reads the scenario's ground-truth label to decide what the *correct* diagnosis
is (invariant 8: only `eval/` may). The script it returns is plain model output, a sequence of tool calls (ids,
cheap tools first) and a final `submit_report`; the evidence text is built from the real tool results found in
the conversation, so findings always describe what the tools returned. Scripted responses are test doubles, not
error text (invariant 9).

Variants build the misbehaving models the tests need: bogus or duplicated tool call ids, a runaway tool-call
loop, token exhaustion, and over-confident proposals that deterministic code must refuse.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from driftgate.agents.investigator import SUBMIT_REPORT
from driftgate.domain import ExecutionSource
from driftgate.eval.ground_truth import FailureLabel
from driftgate.llm.fake import FakeModel, Scripted
from driftgate.llm.types import ModelRequest, ModelResponse, ToolCall, Usage

VARIANTS = (
    "correct",
    "bogus_evidence",  # a valid report plus one evidence item citing an id that was never issued
    "all_bogus_evidence",  # every evidence item cites an id that was never issued
    "duplicate_ids",  # the model reuses one tool call id for two different calls
    "budget_calls",  # keeps calling tools past the limit of 8
    "budget_tokens",  # a response whose usage exhausts the token budget
    "overconfident_tier0",  # proposes Tier 0 at confidence 0.99 whatever the facts are
    "ignore_fleet",  # proposes the scenario's fix even though the failure is fleet-wide
)

Call = tuple[str, dict[str, Any]]


@dataclass(frozen=True)
class Plan:
    """What the scripted investigator does: tool calls in order, then a verdict."""

    batches: list[list[Call]]  # calls grouped by model turn
    classification: str
    layer: str
    confidence: float
    remediation: dict[str, Any] | None
    escalation_reason: str = ""


def _usage(turn: int) -> Usage:
    return Usage(input_tokens=900 + 700 * turn, output_tokens=140)


def _parse_results(request: ModelRequest) -> dict[str, dict[str, Any]]:
    """tool_use id -> {"tool": name, "data": parsed JSON result} for every call answered so far."""
    names: dict[str, str] = {}
    out: dict[str, dict[str, Any]] = {}
    for m in request.messages:
        if not isinstance(m["content"], list):
            continue
        for block in m["content"]:
            if block.get("type") == "tool_use":
                names[block["id"]] = block["name"]
            elif block.get("type") == "tool_result" and not block.get("is_error"):
                out[block["tool_use_id"]] = {
                    "tool": names.get(block["tool_use_id"], "?"),
                    "data": json.loads(block["content"]),
                }
    return out


def _finding(tool: str, d: dict[str, Any]) -> str:
    if tool == "get_execution":
        steps = ", ".join(leaf["name"] for leaf in d.get("failed_leaves", [])) or "none"
        return f"{d.get('pipeline')} execution is {d.get('status')}; failed step: {steps}"
    if tool == "classify_signature":
        return (
            f"signature {d.get('signature')} at step {d.get('step')!r} (layer {d.get('layer')}); "
            f"deterministic match: {d.get('deterministic_match')}; Tier 0 rule: {d.get('tier0_rule_exists')}"
        )
    if tool == "fleet_correlate":
        dim = d.get("shared_dimension") or "no shared dimension"
        return f"fleet_wide={d.get('fleet_wide')}; {d.get('executions_affected')} executions on {dim}"
    if tool == "flake_history":
        return f"{len(d.get('fail_then_pass_episodes', []))} fail-then-pass episodes for this fingerprint"
    if tool == "get_step_logs":
        return f"{d.get('error_block_count')} error blocks in step {d.get('step')!r}"
    if tool == "read_repo_file":
        return f"read {d.get('path')} at {d.get('ref')}"
    return f"{tool} result"


def _plan(label: FailureLabel, source: ExecutionSource, *, ignore_fleet: bool = False) -> Plan:
    ex = source.get_execution(label.execution_id)
    eid = label.execution_id
    base: list[Call] = [("get_execution", {"execution_id": eid}), ("classify_signature", {"execution_id": eid})]
    logs: Call = ("get_step_logs", {"execution_id": eid})
    cls, layer = label.true_classification, label.true_layer
    action, tier = label.correct_action, label.correct_tier

    if label.fleet_wide and not ignore_fleet:
        return Plan(
            [base, [("fleet_correlate", {"execution_id": eid})]],
            "platform",
            layer,
            0.85,
            None,
            "fleet-wide failure on a shared dimension: not for an agent to remediate",
        )
    if tier == 0 and action:
        calls = (
            [base, [("flake_history", {"execution_id": eid})], [logs]]
            if label.tier0_path == "flake_precedent"
            else [base, [logs]]
        )
        why = (
            "fail-then-pass history for this fingerprint"
            if label.tier0_path == "flake_precedent"
            else "Tier 0 known-transient rule"
        )
        rem = {"tier": 0, "action": action, "rationale": f"re-run the failed job: {why}", "reversible": True}
        return Plan(calls, cls, layer, 0.9, rem)
    if tier == 3 and action and label.fix_paths:
        path = label.fix_paths[0]
        read: Call = ("read_repo_file", {"repo": ex.pipeline, "path": path, "ref": ex.refs.get("commit", "main")})
        rem = {
            "tier": 3,
            "action": action,
            "rationale": f"the injected fault is in {path}; a reviewed change fixes it",
            "reversible": True,
            "paths": list(label.fix_paths),
        }
        return Plan([base, [logs], [read]], cls, layer, 0.8, rem)
    if tier in (1, 2) and action:
        rem = {
            "tier": tier,
            "action": action,
            "rationale": f"{action} addresses the platform cause",
            "reversible": tier == 1,
        }
        return Plan([base, [logs]], cls, layer, 0.7, rem)
    reason = "no deterministic cause and no safe action; a human should look"
    return Plan([base, [("flake_history", {"execution_id": eid})], [logs]], cls, layer, 0.6, None, reason)


def _overconfident(label: FailureLabel) -> Plan:
    eid = label.execution_id
    rem = {"tier": 0, "action": "rerun_failed_job", "rationale": "looks transient; re-run", "reversible": True}
    return Plan(
        [[("get_execution", {"execution_id": eid}), ("classify_signature", {"execution_id": eid})]],
        "transient",
        label.true_layer,
        0.99,
        rem,
    )


def _report_input(plan: Plan, results: dict[str, dict[str, Any]], variant: str) -> dict[str, Any]:
    evidence = [
        {"source": cid, "finding": _finding(r["tool"], r["data"]), "supports": "hypothesis"}
        for cid, r in results.items()
    ]
    sig = next((r["data"] for r in results.values() if r["tool"] == "classify_signature"), {})
    hypothesis = f"{sig.get('signature', 'unknown signature')} in step {sig.get('step', '?')!r}"
    bogus = {"source": "toolu_never_issued", "finding": "an uncited claim", "supports": "hypothesis"}
    if variant == "bogus_evidence":
        evidence.append(bogus)
    elif variant == "all_bogus_evidence":
        evidence = [bogus]
    out: dict[str, Any] = {
        "classification": plan.classification,
        "layer": plan.layer,
        "confidence": plan.confidence,
        "hypothesis": hypothesis,
        "evidence": evidence,
    }
    if plan.remediation:
        out["remediation"] = plan.remediation
    else:
        out["escalation_reason"] = plan.escalation_reason
    return out


def _call_id(execution_id: str, n: int) -> str:
    return f"toolu_{execution_id}_{n}"


def build_script(label: FailureLabel, source: ExecutionSource, variant: str = "correct") -> list[Scripted]:
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    if label.disposition == "close":
        return []  # governance: the pre-filter closes it, so any model call fails the script
    eid = label.execution_id
    if variant == "overconfident_tier0":
        plan = _overconfident(label)
    else:
        plan = _plan(label, source, ignore_fleet=variant == "ignore_fleet")

    if variant == "budget_calls":
        calls: list[Call] = [("get_execution", {"execution_id": eid})] * 10
        script: list[Scripted] = [
            ModelResponse(tool_calls=[ToolCall(_call_id(eid, i), n, a)], usage=_usage(i), stop_reason="tool_use")
            for i, (n, a) in enumerate(calls)
        ]
        return script
    if variant == "budget_tokens":
        first = ModelResponse(
            tool_calls=[ToolCall(_call_id(eid, 0), "get_execution", {"execution_id": eid})],
            usage=_usage(0),
            stop_reason="tool_use",
        )
        runaway = ModelResponse(
            tool_calls=[ToolCall(_call_id(eid, 1), "classify_signature", {"execution_id": eid})],
            usage=Usage(input_tokens=45_000, output_tokens=500),
            stop_reason="tool_use",
        )
        return [first, runaway]

    script = []
    n = 0
    for turn, batch in enumerate(plan.batches):
        tool_calls = []
        for name, args in batch:
            tool_calls.append(ToolCall(_call_id(eid, n), name, args))
            n += 1
        if variant == "duplicate_ids" and turn == 0 and len(tool_calls) > 1:
            tool_calls[1] = ToolCall(tool_calls[0].id, tool_calls[1].name, tool_calls[1].input)
        script.append(ModelResponse(tool_calls=tool_calls, usage=_usage(turn), stop_reason="tool_use"))

    turn_count = len(plan.batches)

    def final(request: ModelRequest) -> ModelResponse:
        results = _parse_results(request)
        return ModelResponse(
            text="I have enough evidence.",
            tool_calls=[ToolCall(_call_id(eid, 99), SUBMIT_REPORT, _report_input(plan, results, variant))],
            usage=_usage(turn_count),
            stop_reason="tool_use",
        )

    script.append(final)
    return script


def scripted_model(label: FailureLabel, source: ExecutionSource, variant: str = "correct") -> FakeModel:
    return FakeModel(build_script(label, source, variant))


ScriptFactory = Callable[[FailureLabel, ExecutionSource, str], FakeModel]
