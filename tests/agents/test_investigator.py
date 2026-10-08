"""Investigator loop: mechanics, per-investigation budget, unique ids, evidence integrity, report contract."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from driftgate.adapters.synthetic import SyntheticSource
from driftgate.agents.investigator import (
    SUBMIT_REPORT,
    SYSTEM_PROMPT,
    Investigation,
    Investigator,
    agent_tool_definitions,
)
from driftgate.eval.ground_truth import FailureLabel
from driftgate.eval.scripted_agent import scripted_model
from driftgate.llm.budget import InvestigationBudget
from driftgate.llm.config import InvestigationLimits
from driftgate.llm.errors import BudgetExceeded, FixtureMissing, InvestigationBudgetExhausted
from driftgate.llm.fake import FakeModel
from driftgate.llm.types import ModelRequest, ModelResponse, ToolCall, Usage
from driftgate.tools import TOOLS, ToolContext


@dataclass
class SpyBudget(InvestigationBudget):
    consumed: int = 0

    def consume_tool_call(self) -> bool:
        self.consumed += 1
        return super().consume_tool_call()


@pytest.fixture(scope="module")
def source(dataset_dir: Path) -> SyntheticSource:
    return SyntheticSource(dataset_dir)


@pytest.fixture
def run(source: SyntheticSource) -> Callable[..., Investigation]:

    def _run(label: FailureLabel, variant: str = "correct", model=None, budget=None) -> Investigation:  # type: ignore[no-untyped-def]
        budget = budget or SpyBudget()
        client = model or scripted_model(label, source, variant)
        inv = Investigator(client, ToolContext(source), budget).investigate(label.execution_id)
        inv.model = client  # type: ignore[attr-defined]
        return inv

    return _run


def _submit(**fields: object) -> ModelResponse:
    base = {
        "classification": "transient",
        "layer": "L4",
        "confidence": 0.8,
        "hypothesis": "h",
        "evidence": [],
    }
    return ModelResponse(
        tool_calls=[ToolCall("toolu_final", SUBMIT_REPORT, {**base, **fields})],
        usage=Usage(500, 50),
        stop_reason="tool_use",
    )


def _call(call_id: str, name: str, **args: object) -> ModelResponse:
    return ModelResponse(tool_calls=[ToolCall(call_id, name, dict(args))], usage=Usage(500, 50), stop_reason="tool_use")


# --- loop mechanics ------------------------------------------------------------------------------------------


def test_correct_run_reports_with_cited_evidence_and_a_proposal(run, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    inv = run(label)
    assert inv.stop_reason == "report"
    assert not inv.report.abstained and not inv.report.budget_truncated
    ids = set(inv.tool_call_ids)
    assert len(ids) == len(inv.tool_call_ids) == inv.budget.tool_calls == 3
    assert inv.report.evidence and {e.source for e in inv.report.evidence} <= ids
    assert inv.evidence_dropped == 0
    assert inv.proposal is not None and inv.proposal.remediation.tier == 0
    assert inv.proposal.remediation.gate_decision == "allowed"  # the pre-gate default; only SafetyGate changes it
    assert inv.report.fingerprint  # taken from classify_signature, which the model called
    assert inv.model_calls == 3


def test_requests_carry_a_static_cache_friendly_prefix_and_tool_results(run, pick) -> None:
    inv = run(pick("transient_throttling", fleet=False))
    requests = inv.model.requests
    assert {r.system for r in requests} == {SYSTEM_PROMPT}
    assert all(r.tools == requests[0].tools for r in requests)
    names = [t["name"] for t in requests[0].tools]
    assert names == [t.name for t in TOOLS] + [SUBMIT_REPORT]
    assert requests[0].tools == agent_tool_definitions()
    first = requests[0].messages[0]["content"]
    assert pick("transient_throttling", fleet=False).execution_id in first
    last = requests[-1].messages[-1]["content"]
    assert [b["type"] for b in last] == ["tool_result"]
    assert last[0]["tool_use_id"].startswith("toolu_")
    assert not last[0]["is_error"] and isinstance(json.loads(last[0]["content"]), dict)


def test_first_request_is_deterministic_across_runs(run, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    a, b = run(label), run(label)
    assert a.model.requests[0] == b.model.requests[0]


def test_system_prompt_states_the_rules_the_code_enforces() -> None:
    for needle in ("cheapest", "cite", "never sufficient", "Tier 3", "submit_report", "8 tool calls"):
        assert needle.lower() in SYSTEM_PROMPT.lower()


def test_unknown_tool_is_an_error_result_not_an_exception(run, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    model = FakeModel([_call("t1", "delete_everything"), _submit()])
    inv = run(label, model=model)
    assert [(r.source, r.ok) for r in inv.tool_results] == [("t1", False)]
    assert inv.budget.consumed == 1  # an unknown call still costs budget
    assert model.requests[1].messages[-1]["content"][0]["is_error"] is True


def test_text_only_json_report_is_accepted(run, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    body = json.dumps(
        {
            "classification": "transient",
            "layer": "L4",
            "confidence": 0.5,
            "hypothesis": "h",
            "evidence": [{"source": "t1", "finding": "f", "supports": "s"}],
        }
    )
    model = FakeModel([_call("t1", "get_execution", execution_id=label.execution_id), ModelResponse(text=body)])
    inv = run(label, model=model)
    assert inv.stop_reason == "report" and len(inv.report.evidence) == 1


def test_model_that_never_reports_gets_one_nudge_then_abstains(run, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    model = FakeModel([ModelResponse(text="thinking"), ModelResponse(text="still thinking")])
    inv = run(label, model=model)
    assert inv.stop_reason == "no_report" and inv.report.abstained and inv.proposal is None
    assert len(model.requests) == 2 and "submit_report" in model.requests[1].messages[-1]["content"]


def test_malformed_report_abstains(run, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    inv = run(
        label, model=FakeModel([_call("t1", "get_execution", execution_id=label.execution_id), _submit(layer="L9")])
    )
    assert inv.stop_reason == "malformed_report" and inv.report.abstained and inv.proposal is None
    bad_tier = {"tier": 7, "action": "x", "rationale": "r", "reversible": True}
    inv = run(
        label,
        model=FakeModel([_call("t1", "get_execution", execution_id=label.execution_id), _submit(remediation=bad_tier)]),
    )
    assert inv.stop_reason == "malformed_report" and inv.proposal is None


def test_gateway_failures_abstain_but_missing_fixtures_propagate(run, pick) -> None:
    label = pick("transient_throttling", fleet=False)

    class Boom:
        def __init__(self, exc: Exception) -> None:
            self.exc = exc

        def complete(self, request: ModelRequest) -> ModelResponse:
            raise self.exc

    inv = run(label, model=Boom(BudgetExceeded("daily", 1.0, 0.99, 0.05)))
    assert inv.stop_reason == "model_unavailable" and inv.report.abstained and not inv.report.budget_truncated
    with pytest.raises(FixtureMissing):
        run(label, model=Boom(FixtureMissing("k", "p")))


# --- budget ---------------------------------------------------------------------------------------------------


def test_consume_tool_call_is_called_once_per_tool_call(run, pick) -> None:
    label = pick("user_lockfile_mismatch")
    inv = run(label)
    assert inv.budget.consumed == inv.budget.tool_calls == len(inv.tool_results) == 3


def test_the_ninth_tool_call_is_refused_and_the_run_abstains_with_partial_evidence(run, pick, source) -> None:
    label = pick("transient_throttling", fleet=False)
    model = scripted_model(label, source, "budget_calls")
    inv = run(label, model=model)
    assert inv.stop_reason == "budget_tool_calls"
    assert inv.budget.consumed == 9 and inv.budget.tool_calls == 8 and len(inv.tool_results) == 8
    r = inv.report
    assert r.abstained and r.budget_truncated and r.escalation_reason and inv.proposal is None
    assert r.remediation is None and r.classification == "unknown"
    assert {e.source for e in r.evidence} == set(inv.tool_call_ids) and len(r.evidence) == 8
    assert r.run.tool_calls == 8


def test_a_smaller_tool_call_limit_is_honoured(source, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    budget = SpyBudget(InvestigationLimits(max_tool_calls=2))
    inv = Investigator(scripted_model(label, source), ToolContext(source), budget).investigate(label.execution_id)
    assert inv.stop_reason == "budget_tool_calls" and budget.tool_calls == 2 and budget.consumed == 3


def test_token_exhaustion_truncates_before_running_the_next_tool(run, pick, source) -> None:
    label = pick("transient_throttling", fleet=False)
    inv = run(label, model=scripted_model(label, source, "budget_tokens"))
    assert inv.stop_reason == "budget_tokens"
    assert inv.budget.tool_calls == 1  # the call in the over-budget response was not run
    assert inv.report.budget_truncated and inv.report.abstained and inv.proposal is None
    assert inv.report.run.input_tokens + inv.report.run.output_tokens > 40_000
    assert len(inv.report.evidence) == 1


def test_gateway_style_token_exhaustion_is_handled(run, pick) -> None:
    label = pick("transient_throttling", fleet=False)

    class Exhausted:
        def complete(self, request: ModelRequest) -> ModelResponse:
            raise InvestigationBudgetExhausted("token budget 40000 used (41000)")

    inv = run(label, model=Exhausted())
    assert inv.stop_reason == "budget_tokens" and inv.report.budget_truncated and inv.report.abstained


# --- unique ids -----------------------------------------------------------------------------------------------


def test_a_repeated_tool_call_id_is_rejected_and_never_dispatched(run, pick, source) -> None:
    label = pick("transient_throttling", fleet=False)
    inv = run(label, model=scripted_model(label, source, "duplicate_ids"))
    ids = inv.tool_call_ids
    assert len(ids) == len(set(ids))
    assert inv.duplicate_ids_rejected == 1
    assert [r.tool for r in inv.tool_results[:1]] == ["get_execution"]  # the second call with that id never ran
    assert inv.budget.tool_calls == len(inv.tool_results) and inv.budget.consumed == len(inv.tool_results)
    second = inv.model.requests[1].messages[-1]["content"]
    assert [b["is_error"] for b in second][:2] == [False, True]


def test_a_repeated_id_across_turns_is_rejected(run, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    eid = label.execution_id
    model = FakeModel(
        [
            _call("same", "get_execution", execution_id=eid),
            _call("same", "classify_signature", execution_id=eid),
            _submit(),
        ]
    )
    inv = run(label, model=model)
    assert inv.tool_call_ids == ["same"] and inv.duplicate_ids_rejected == 1


# --- evidence integrity (invariant 7) -------------------------------------------------------------------------


def test_evidence_citing_an_unissued_id_is_dropped_in_code(run, pick, source) -> None:
    label = pick("transient_throttling", fleet=False)
    inv = run(label, model=scripted_model(label, source, "bogus_evidence"))
    assert inv.evidence_submitted == 4 and inv.evidence_dropped == 1
    assert "toolu_never_issued" not in {e.source for e in inv.report.evidence}
    assert {e.source for e in inv.report.evidence} <= set(inv.tool_call_ids)
    assert not inv.report.abstained and inv.proposal is not None


def test_a_report_whose_evidence_is_all_dropped_abstains(run, pick, source) -> None:
    label = pick("transient_throttling", fleet=False)
    inv = run(label, model=scripted_model(label, source, "all_bogus_evidence"))
    assert inv.evidence_dropped == inv.evidence_submitted == 1
    assert inv.report.abstained and inv.report.evidence == []
    assert inv.proposal is None and inv.report.remediation is None  # the uncited proposal is discarded too
    assert inv.report.classification == "unknown" and inv.report.confidence == 0.0
    assert not inv.report.budget_truncated and "no cited evidence" in inv.report.escalation_reason


def test_a_report_with_no_evidence_at_all_abstains(run, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    inv = run(
        label,
        model=FakeModel(
            [_submit(remediation={"tier": 0, "action": "rerun_failed_job", "rationale": "r", "reversible": True})]
        ),
    )
    assert inv.report.abstained and inv.proposal is None


def test_malformed_evidence_items_are_dropped(run, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    eid = label.execution_id
    items = [
        {"source": "t1", "finding": "ok", "supports": "s"},
        {"finding": "no source", "supports": "s"},
        {"source": "t1", "finding": 5, "supports": "s"},
        "not an object",
    ]
    inv = run(label, model=FakeModel([_call("t1", "get_execution", execution_id=eid), _submit(evidence=items)]))
    assert len(inv.report.evidence) == 1 and inv.evidence_dropped == 3


def test_an_evidence_item_may_cite_a_failed_tool_call_of_this_run(run, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    items = [{"source": "t1", "finding": "the call failed", "supports": "s"}]
    inv = run(label, model=FakeModel([_call("t1", "get_execution", execution_id="nope"), _submit(evidence=items)]))
    assert inv.tool_results[0].ok is False and len(inv.report.evidence) == 1


# --- report contract ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant", ["correct", "bogus_evidence", "all_bogus_evidence", "duplicate_ids", "budget_calls", "budget_tokens"]
)
def test_every_investigation_report_validates_against_the_schema(
    run, pick, source, report_validator: Draft202012Validator, variant: str
) -> None:
    label = pick("user_lockfile_mismatch")
    inv = run(label, model=scripted_model(label, source, variant))
    report_validator.validate(json.loads(json.dumps(inv.report.to_dict())))
    assert inv.report.execution_id == label.execution_id


def test_confidence_is_recorded_but_changes_nothing_in_the_loop(run, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    eid = label.execution_id
    rem = {"tier": 0, "action": "rerun_failed_job", "rationale": "r", "reversible": True}
    ev = [{"source": "t1", "finding": "f", "supports": "s"}]
    low = run(
        label,
        model=FakeModel(
            [_call("t1", "get_execution", execution_id=eid), _submit(confidence=0.1, evidence=ev, remediation=rem)]
        ),
    )
    high = run(
        label,
        model=FakeModel(
            [_call("t1", "get_execution", execution_id=eid), _submit(confidence=1.0, evidence=ev, remediation=rem)]
        ),
    )
    assert (low.report.confidence, high.report.confidence) == (0.1, 1.0)
    assert low.proposal.remediation == high.proposal.remediation  # type: ignore[union-attr]
