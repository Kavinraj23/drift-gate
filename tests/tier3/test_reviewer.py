"""The independent PR reviewer: isolation, tools, verdict handling, budgets, fail-closed behaviour."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from driftgate.adapters.synthetic import SyntheticSource
from driftgate.agents import reviewer as reviewer_mod
from driftgate.agents.investigator import SYSTEM_PROMPT as INVESTIGATOR_PROMPT
from driftgate.agents.reviewer import (
    SUBMIT_REVIEW,
    SYSTEM_PROMPT,
    Reviewer,
    ReviewRequest,
    reviewer_tool_definitions,
)
from driftgate.eval.ground_truth import FailureLabel
from driftgate.llm.budget import InvestigationBudget
from driftgate.llm.config import InvestigationLimits
from driftgate.llm.errors import GatewayError
from driftgate.llm.fake import FakeModel
from driftgate.llm.types import ModelRequest, ModelResponse, ToolCall, Usage
from driftgate.tools import ToolContext

RunT3 = Callable[..., Any]
REQ = ReviewRequest(
    "billing-service", "main", "the lockfile is stale", "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n", {"x": "a\n"}
)


def respond(*calls: ToolCall, text: str = "", usage: Usage | None = None) -> ModelResponse:
    return ModelResponse(text=text, tool_calls=list(calls), usage=usage or Usage(500, 50), stop_reason="tool_use")


def verdict(v: str = "approve", comments: str = "fine", cid: str = "v1") -> ModelResponse:
    return respond(ToolCall(cid, SUBMIT_REVIEW, {"verdict": v, "comments": comments}))


def make(script: list[Any], source: SyntheticSource | None = None, limits: InvestigationLimits | None = None):  # type: ignore[no-untyped-def]
    model = FakeModel(script)
    budget = InvestigationBudget(limits or InvestigationLimits())
    ctx = ToolContext(source)  # type: ignore[arg-type]
    return Reviewer(model, ctx, budget), model, budget


# --- tools and prompt ----------------------------------------------------------------------------------------
def test_reviewer_is_offered_read_repo_file_and_its_verdict_tool_only() -> None:
    names = [t["name"] for t in reviewer_tool_definitions()]
    assert names == ["read_repo_file", SUBMIT_REVIEW]


def test_reviewer_has_its_own_static_system_prompt() -> None:
    assert SYSTEM_PROMPT != INVESTIGATOR_PROMPT and "reviewer" in SYSTEM_PROMPT.lower()
    r, model, _ = make([verdict()])
    r.review(REQ)
    r2, model2, _ = make([verdict()])
    r2.review(ReviewRequest("other", "main", "another hypothesis", "--- a/y\n+++ b/y\n@@ -1 +1 @@\n-c\n+d\n"))
    assert model.requests[0].system == model2.requests[0].system == SYSTEM_PROMPT
    assert model.requests[0].tools == model2.requests[0].tools


def test_verdict_enum_comes_from_the_report_contract() -> None:
    schema = json.loads((Path(__file__).resolve().parents[2] / "contracts" / "report.schema.json").read_text("utf-8"))
    contract = schema["properties"]["review"]["properties"]["verdict"]["enum"]
    offered = reviewer_tool_definitions()[-1]["input_schema"]["properties"]["verdict"]["enum"]
    assert offered == contract == ["approve", "revise", "reject"]


def test_other_tools_are_refused_and_never_dispatched(dataset_dir: Path) -> None:
    src = SyntheticSource(dataset_dir)
    sneaky = respond(ToolCall("t1", "get_execution", {"execution_id": "ex-000112"}))
    r, model, budget = make([sneaky, verdict()], src)
    res = r.review(REQ)
    assert res.review.verdict == "approve" and res.refused_tool_calls == 1
    assert res.tool_results == [] and budget.tool_calls == 0  # nothing ran, nothing counted
    result_block = model.requests[1].messages[-1]["content"][0]
    assert result_block["is_error"] and "unavailable tool" in result_block["content"]


def test_read_repo_file_works_and_counts_against_the_budget(dataset_dir: Path, t3_labels: list[FailureLabel]) -> None:
    src = SyntheticSource(dataset_dir)
    label = t3_labels[0]
    ex = src.get_execution(label.execution_id)
    call = ToolCall("r1", "read_repo_file", {"repo": ex.pipeline, "path": label.fix_paths[0], "ref": ex.refs["commit"]})
    r, model, budget = make([respond(call), verdict("revise", "tighten it")], src)
    res = r.review(REQ)
    assert (res.review.verdict, res.review.comments) == ("revise", "tighten it")
    assert budget.tool_calls == 1 and [t.tool for t in res.tool_results] == ["read_repo_file"] and res.valid
    assert label.fix_paths[0] in json.dumps(model.requests[1].messages[-1]["content"])


# --- verdict handling ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("v", ["approve", "revise", "reject"])
def test_each_verdict_is_returned_with_its_comments(v: str) -> None:
    r, _, _ = make([verdict(v, f"because {v}")])
    res = r.review(REQ)
    assert (res.review.verdict, res.review.comments, res.stop_reason) == (v, f"because {v}", "verdict")


@pytest.mark.parametrize(
    "bad", [{"verdict": "lgtm", "comments": "x"}, {"comments": "x"}, {"verdict": "approve", "comments": 3}]
)
def test_malformed_verdict_fails_closed(bad: dict[str, Any]) -> None:
    r, _, _ = make([respond(ToolCall("v", SUBMIT_REVIEW, bad))])
    res = r.review(REQ)
    assert res.review.verdict == "reject" and res.stop_reason == "malformed_verdict" and not res.valid


def test_no_verdict_after_one_nudge_fails_closed() -> None:
    r, model, _ = make([ModelResponse(text="I think it is fine."), ModelResponse(text="Really, it is fine.")])
    res = r.review(REQ)
    assert res.review.verdict == "reject" and res.stop_reason == "no_verdict" and len(model.requests) == 2
    assert "submit_review" in str(model.requests[1].messages[-1]["content"])


def test_tool_call_budget_is_enforced_for_the_reviewer(dataset_dir: Path) -> None:
    calls = [respond(ToolCall(f"c{i}", "read_repo_file", {"repo": "r", "path": "p", "ref": "main"})) for i in range(5)]
    src = SyntheticSource(dataset_dir)  # the files do not exist: errors come back as results, still counted
    r, _, budget = make(calls, src, InvestigationLimits(max_tool_calls=2))
    res = r.review(REQ)
    assert res.stop_reason == "budget_tool_calls" and res.review.verdict == "reject" and budget.tool_calls == 2


def test_token_budget_is_enforced_for_the_reviewer() -> None:
    hog = respond(ToolCall("c", "read_repo_file", {"repo": "r", "path": "p", "ref": "main"}), usage=Usage(50_000, 10))
    r, model, _ = make([hog, verdict()])
    res = r.review(REQ)
    assert res.stop_reason == "budget_tokens" and res.review.verdict == "reject" and len(model.requests) == 1


def test_gateway_failure_fails_closed() -> None:
    class Down:
        def complete(self, request: ModelRequest) -> ModelResponse:
            raise GatewayError("spend cap")

    r = Reviewer(Down(), ToolContext(None), InvestigationBudget())  # type: ignore[arg-type]
    res = r.review(REQ)
    assert res.review.verdict == "reject" and res.stop_reason == "model_unavailable"


def test_duplicate_tool_call_ids_are_rejected() -> None:
    dup = respond(ToolCall("same", "get_execution", {}), ToolCall("same", "get_execution", {}))
    r, model, _ = make([dup, verdict()])
    res = r.review(REQ)
    assert res.review.verdict == "approve"
    blocks = model.requests[1].messages[-1]["content"]
    assert blocks[1]["is_error"] and "unique" in blocks[1]["content"]


def test_reviewer_reuses_the_investigation_budget_class() -> None:
    assert reviewer_mod.InvestigationBudget is InvestigationBudget


# --- isolation from the investigator -------------------------------------------------------------------------
def test_reviewer_never_sees_the_investigators_trace(run_t3: RunT3, t3_labels: list[FailureLabel]) -> None:
    run = run_t3(t3_labels[0])
    assert run.outcome.kind == "pr_proposed" and len(run.reviewer_spies) == 1
    inv = run.outcome.investigation
    assert inv is not None and inv.tool_results and run.outcome.report.evidence
    seen = []
    for spy in run.reviewer_spies:
        for req in spy.requests:
            seen.append(req.system + json.dumps(req.messages) + json.dumps(req.tools))
    blob = "\n".join(seen)
    assert run.outcome.report.hypothesis in blob  # it gets the hypothesis ...
    assert (run.outcome.report.remediation.dry_run["diff_text"].splitlines()[0]) in blob  # type: ignore[union-attr]
    # ... and none of the investigator's trace: ids, findings, tool output, prompt, confidence, classification
    for e in run.outcome.report.evidence:
        assert e.finding not in blob and e.source not in blob
    for r in inv.tool_results:
        assert r.source not in blob
    assert INVESTIGATOR_PROMPT not in blob and "get_step_logs" not in blob and "classify_signature" not in blob
    assert (
        "confidence" not in blob.lower().replace("confidence", "", 0) or str(run.outcome.report.confidence) not in blob
    )


def test_reviewer_is_offered_only_read_repo_file_in_a_real_run(run_t3: RunT3, t3_labels: list[FailureLabel]) -> None:
    run = run_t3(t3_labels[0])
    for spy in run.reviewer_spies:
        for req in spy.requests:
            assert [t["name"] for t in req.tools] == ["read_repo_file", SUBMIT_REVIEW]
    assert all(r.offered_tools == ("read_repo_file", SUBMIT_REVIEW) for r in run.hook.results)


def test_reviewer_request_holds_diff_target_files_and_hypothesis(
    run_t3: RunT3, t3_labels: list[FailureLabel], dataset_dir: Path
) -> None:
    label = t3_labels[0]
    run = run_t3(label)
    first = str(run.reviewer_spies[0].requests[0].messages[0]["content"])
    base = SyntheticSource(dataset_dir)
    ex = base.get_execution(label.execution_id)
    assert label.fix_diff and label.fix_diff.splitlines()[0] in first
    assert base.read_file(ex.pipeline, label.fix_paths[0], ex.refs["commit"], 10**6).content[:40] in first
    assert f"Repository: {ex.pipeline} at ref {ex.refs['commit']}" in first
