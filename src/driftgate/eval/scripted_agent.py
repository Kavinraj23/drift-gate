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
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from driftgate.agents.investigator import SUBMIT_REPORT
from driftgate.domain import ExecutionSource
from driftgate.eval.ground_truth import FailureLabel
from driftgate.eval.tier3_scripts import BAD_KINDS, SeededDiff, bad_diff
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
    "wrong_paths",  # Tier 3 scenarios: proposes the right action on the wrong file (CI will be red)
)

#: Second-round behaviours of a re-investigation (see `build_reinvestigation_script`).
REINVESTIGATION_MODES = (
    "revise_escalate",  # reads the failed attempt, revises the hypothesis, then does what the label says is right
    "right_paths",  # reads the failed attempt, then proposes the label's Tier 3 fix
    "wrong_paths",  # reads the failed attempt, then proposes the wrong file again
    "repeat_tier0",  # reads the failed attempt, revises, but proposes another Tier 0 re-run
    "same_hypothesis",  # proposes without revising the hypothesis (code must refuse)
    "no_read",  # proposes without reading the failed attempt (code must refuse)
)

#: `diff:<kind>` makes a Tier 3 scenario propose a seeded bad diff (see `tier3_scripts.BAD_KINDS`).
DIFF_VARIANT_PREFIX = "diff:"

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
    if tool == "get_remediation_attempt":
        rows = d.get("attempts", [])
        last = rows[-1] if rows else {}
        v = last.get("verification", {})
        return (
            f"{len(rows)} prior attempt(s); last: Tier {last.get('tier')} {last.get('action')}, outcome "
            f"{last.get('outcome')}, verified={v.get('verified')}, recurred={v.get('recurred')}"
        )
    return f"{tool} result"


def _plan(
    label: FailureLabel, source: ExecutionSource, *, ignore_fleet: bool = False, wrong_paths: bool = False
) -> Plan:
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
    if tier == 3 and action and label.fix_paths and wrong_paths:
        wrong = _wrong_file_change(label, source)
        rem = {
            "tier": 3,
            "action": action,
            "rationale": f"a reviewed change to {wrong.paths[0]} fixes it",
            "reversible": True,
            "paths": list(wrong.paths),
            "diff": wrong.text,
        }
        return Plan([base, [logs]], cls, layer, 0.7, rem)
    if tier == 3 and action and label.fix_paths:
        path = label.fix_paths[0]
        read: Call = ("read_repo_file", {"repo": ex.pipeline, "path": path, "ref": ex.refs.get("commit", "main")})
        rem = {
            "tier": 3,
            "action": action,
            "rationale": f"the injected fault is in {path}; a reviewed change fixes it",
            "reversible": True,
            "paths": list(label.fix_paths),
            "diff": label.fix_diff or "",
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


def _wrong_file_change(label: FailureLabel, source: ExecutionSource) -> SeededDiff:
    """A well-formed diff to a file the action may change but that does not hold the fault (CI stays red)."""
    seeded = bad_diff("wrong_file", label, source)
    if seeded is None:
        raise ValueError(f"no wrong-file change is defined for {label.execution_id}")
    return seeded


def _with_bad_diff(plan: Plan, kind: str, label: FailureLabel, source: ExecutionSource) -> Plan:
    """Swap the Tier 3 proposal's diff and paths for a seeded bad one (unchanged when the kind does not apply)."""
    seeded = bad_diff(kind, label, source)
    if seeded is None or plan.remediation is None:
        raise ValueError(f"bad diff kind {kind!r} does not apply to {label.execution_id}")
    rem = {**plan.remediation, "diff": seeded.text, "paths": list(seeded.paths)}
    return Plan(plan.batches, plan.classification, plan.layer, plan.confidence, rem, plan.escalation_reason)


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
    bad_kind = variant.removeprefix(DIFF_VARIANT_PREFIX) if variant.startswith(DIFF_VARIANT_PREFIX) else None
    if variant not in VARIANTS and bad_kind not in BAD_KINDS:
        raise ValueError(f"unknown variant {variant!r}")
    if label.disposition == "close":
        return []  # governance: the pre-filter closes it, so any model call fails the script
    eid = label.execution_id
    if variant == "overconfident_tier0":
        plan = _overconfident(label)
    else:
        plan = _plan(label, source, ignore_fleet=variant == "ignore_fleet", wrong_paths=variant == "wrong_paths")
        if bad_kind is not None:
            plan = _with_bad_diff(plan, bad_kind, label, source)

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


# ---------------------------------------------------------------------------------------------
# Second round: the re-investigation script. The failed attempt is read through get_remediation_attempt, so the
# revised hypothesis is built from what the tool returned, never from the label's description of the failure.
# ---------------------------------------------------------------------------------------------

_FP_IN_CONTEXT = re.compile(r"Failure fingerprint: (\w+)")


def _fingerprint_from(request: ModelRequest) -> str:
    first = request.messages[0]["content"]
    m = _FP_IN_CONTEXT.search(first if isinstance(first, str) else "")
    return m.group(1) if m else "unknown"


def build_reinvestigation_script(label: FailureLabel, source: ExecutionSource, mode: str) -> list[Scripted]:
    if mode not in REINVESTIGATION_MODES:
        raise ValueError(f"unknown re-investigation mode {mode!r}")
    eid = label.execution_id
    ex = source.get_execution(eid)

    def cid(n: int) -> str:
        return f"toolu_{eid}_r2_{n}"

    base = [
        ToolCall(cid(0), "get_execution", {"execution_id": eid}),
        ToolCall(cid(1), "classify_signature", {"execution_id": eid}),
    ]
    script: list[Scripted] = [ModelResponse(tool_calls=base, usage=_usage(0), stop_reason="tool_use")]
    turn = 1
    if mode != "no_read":

        def read_attempt(request: ModelRequest) -> ModelResponse:
            call = ToolCall(cid(2), "get_remediation_attempt", {"fingerprint": _fingerprint_from(request)})
            return ModelResponse(tool_calls=[call], usage=_usage(1), stop_reason="tool_use")

        script.append(read_attempt)
        turn += 1
    if mode == "right_paths":
        ref = ex.refs.get("commit", "main")
        read = ToolCall(cid(3), "read_repo_file", {"repo": ex.pipeline, "path": label.fix_paths[0], "ref": ref})
        script.append(ModelResponse(tool_calls=[read], usage=_usage(turn), stop_reason="tool_use"))
        turn += 1

    remediation: dict[str, Any] | None
    reason = ""
    if mode == "right_paths":
        remediation = {
            "tier": 3,
            "action": label.correct_action,
            "rationale": f"the first change missed the fault in {label.fix_paths[0]}",
            "reversible": True,
            "paths": list(label.fix_paths),
            "diff": label.fix_diff or "",
        }
    elif mode == "wrong_paths":
        wrong = _wrong_file_change(label, source)
        remediation = {
            "tier": 3,
            "action": label.correct_action,
            "rationale": f"another change, to {wrong.paths[0]}",
            "reversible": True,
            "paths": list(wrong.paths),
            "diff": wrong.text,
        }
    elif mode in ("repeat_tier0", "same_hypothesis", "no_read"):
        remediation = {"tier": 0, "action": "rerun_failed_job", "rationale": "re-run once more", "reversible": True}
    elif label.correct_tier in (1, 2) and label.correct_action:
        remediation = {
            "tier": label.correct_tier,
            "action": label.correct_action,
            "rationale": f"{label.correct_action} addresses the platform cause the re-run exposed",
            "reversible": label.correct_tier == 1,
        }
    else:
        remediation = None
        reason = "the re-run did not clear the failure and no safe action is supported; a human should look"

    def final(request: ModelRequest) -> ModelResponse:
        results = _parse_results(request)
        sig = next((r["data"] for r in results.values() if r["tool"] == "classify_signature"), {})
        attempt = next((r["data"] for r in results.values() if r["tool"] == "get_remediation_attempt"), None)
        plan = Plan([], label.true_classification, label.true_layer, 0.7, remediation, reason)
        data = _report_input(plan, results, "correct")  # the first-round hypothesis formula, unrevised
        if mode != "same_hypothesis" and attempt is not None:
            last = (attempt.get("attempts") or [{}])[-1]
            v = last.get("verification", {})
            data["hypothesis"] = (
                f"Revised: the first hypothesis was wrong because the {last.get('action')} attempt did not hold "
                f"(verified={v.get('verified')}, recurred={v.get('recurred')}); the cause is "
                f"{sig.get('signature')} in step {sig.get('step')!r}"
            )
        return ModelResponse(
            text="Revised conclusion.",
            tool_calls=[ToolCall(cid(99), SUBMIT_REPORT, data)],
            usage=_usage(turn),
            stop_reason="tool_use",
        )

    script.append(final)
    return script


def two_round_factory(
    label: FailureLabel, source: ExecutionSource, first: str = "correct", second: str = "revise_escalate"
) -> Callable[[str, Any], FakeModel]:
    """A model factory for `Orchestrator`: the first call builds the first-round model, the second the
    re-investigation's. Stateful by design (it counts rounds); use a fresh factory per orchestrator."""
    rounds: list[int] = []

    def factory(_execution_id: str, _budget: Any) -> FakeModel:
        rounds.append(1)
        if len(rounds) == 1:
            return scripted_model(label, source, first)
        return FakeModel(build_reinvestigation_script(label, source, second))

    return factory
