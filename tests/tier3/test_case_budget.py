"""One budget and one revision for a whole Tier 3 case: investigator, revision, reviewers and re-investigation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from driftgate.adapters.synthetic import SyntheticSource
from driftgate.agents.investigator import SUBMIT_REPORT
from driftgate.agents.reviewer import SUBMIT_REVIEW
from driftgate.eval.ground_truth import FailureLabel
from driftgate.eval.scripted_agent import build_reinvestigation_script, scripted_model
from driftgate.eval.tier3_scripts import bad_diff, scripted_reviewer
from driftgate.llm.fake import FakeModel
from driftgate.llm.types import ModelResponse, ToolCall, Usage
from driftgate.orchestrator import ESCALATED, Orchestrator, Outcome, make_synthetic_target
from driftgate.tier3 import ReviewerHook


def report_model(label: FailureLabel, remediation: dict[str, Any]) -> FakeModel:
    """One tool call, then a report: a cheap investigator."""
    call = ToolCall("c0", "classify_signature", {"execution_id": label.execution_id})
    body = {
        "classification": "user",
        "layer": "L4",
        "confidence": 0.7,
        "hypothesis": "the lockfile is stale",
        "evidence": [{"source": "c0", "finding": "a finding", "supports": "hypothesis"}],
        "remediation": remediation,
    }
    return FakeModel(
        [
            ModelResponse(tool_calls=[call], usage=Usage(500, 50), stop_reason="tool_use"),
            ModelResponse(tool_calls=[ToolCall("cf", SUBMIT_REPORT, body)], usage=Usage(600, 60)),
        ]
    )


def tier3(label: FailureLabel, **extra: Any) -> dict[str, Any]:
    base = {
        "tier": 3,
        "action": label.correct_action,
        "rationale": "fix it",
        "reversible": True,
        "paths": list(label.fix_paths),
        "diff": label.fix_diff,
    }
    return {**base, **extra}


class Clock:
    """A fake clock the test can move forward (the rate limiter is the only reader that matters here)."""

    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


def verdict_model(verdict: str, comments: str = "x") -> FakeModel:
    call = ToolCall("rv", SUBMIT_REVIEW, {"verdict": verdict, "comments": comments})
    return FakeModel([ModelResponse(tool_calls=[call], usage=Usage(500, 50), stop_reason="tool_use")])


def assert_case_totals(out: Outcome, hook: ReviewerHook) -> None:
    assert out.investigation is not None
    shared = out.investigation.budget
    run = out.report.run
    fields = shared.run_fields()
    assert (run.tool_calls, run.input_tokens, run.output_tokens) == (
        shared.tool_calls,
        fields["input_tokens"],
        fields["output_tokens"],
    )
    assert shared.tool_calls <= 8 and shared.tokens_used <= 40_000
    for r in hook.results:  # every reviewer call is on the shared budget as well as on the reviewer's own view
        assert r.budget.calls and all(c in shared.calls for c in r.budget.calls)


def build(
    make_orch: Callable[..., Orchestrator],
    label: FailureLabel,
    investigators: list[Callable[[], Any]],
    reviewers: list[Callable[[], Any]],
    dataset_dir: Path,
    clock: Clock,
    advance_before_investigator: int | None = None,
    verified: bool = False,
) -> tuple[Orchestrator, ReviewerHook]:
    src = SyntheticSource(dataset_dir)
    inv_calls: list[int] = []
    rev_calls: list[int] = []

    def inv_factory(_e: str, _b: Any) -> Any:
        inv_calls.append(1)
        if advance_before_investigator == len(inv_calls):
            clock.t += 7200.0
        return investigators[len(inv_calls) - 1]()

    def rev_factory(_e: str, _b: Any) -> Any:
        rev_calls.append(1)
        return reviewers[len(rev_calls) - 1]()

    hook = ReviewerHook(src, rev_factory)
    orch = make_orch(
        label,
        model_factory=inv_factory,
        tier3_review=hook,
        verification=True,
        clock=clock,
        target=make_synthetic_target(dataset_dir, verified=verified),
    )
    return orch, hook


def test_revision_then_red_ci_cannot_exceed_one_budget_and_round_two_is_truncated(
    make_orch: Callable[..., Orchestrator], t3_labels: list[FailureLabel], dataset_dir: Path
) -> None:
    label = t3_labels[0]
    src = SyntheticSource(dataset_dir)
    round_two = FakeModel([])
    orch, hook = build(
        make_orch,
        label,
        [
            lambda: scripted_model(label, src, "diff:wrong_file"),
            lambda: scripted_model(label, src, "correct"),
            lambda: round_two,
        ],
        [lambda: scripted_reviewer(label, src, "revising"), lambda: scripted_reviewer(label, src, "revising")],
        dataset_dir,
        Clock(),
    )
    out = orch.handle(label.execution_id)
    assert round_two.requests == []  # the spent budget means round 2 never reached the model
    # 3 + 1 (review) + 3 (revision) + 1 (review) = 8 tool calls: the case's whole allowance is gone after the PR.
    assert [r.review.verdict for r in hook.results] == ["revise", "approve"] and out.revisions == 1
    assert out.rounds == 2 and out.first_attempt_failed and out.kind == ESCALATED
    assert out.report.budget_truncated and "budget exhausted" in out.reason
    assert out.report.prior_attempts and out.report.prior_attempts[0]["outcome"] == "failed"
    assert out.investigation is not None and out.investigation.budget.reinvestigations == 1
    assert out.report.run.tool_calls == 8
    assert_case_totals(out, hook)
    assert out.first_round is not None and out.first_round.revisions == 1


def test_a_revision_in_round_one_means_a_revise_verdict_in_round_two_escalates(
    make_orch: Callable[..., Orchestrator], t3_labels: list[FailureLabel], dataset_dir: Path
) -> None:
    label = t3_labels[0]
    src = SyntheticSource(dataset_dir)
    bad = bad_diff("wrong_file", label, src)
    assert bad is not None
    orch, hook = build(
        make_orch,
        label,
        [
            lambda: report_model(label, tier3(label, diff=bad.text, paths=list(bad.paths))),
            lambda: report_model(label, tier3(label)),
            lambda: FakeModel(build_reinvestigation_script(label, src, "right_paths")),
        ],
        [lambda: verdict_model("revise"), lambda: verdict_model("approve"), lambda: verdict_model("revise", "again")],
        dataset_dir,
        Clock(),
        advance_before_investigator=3,  # past the 2-per-hour limit so the second round reaches its reviewer
    )
    out = orch.handle(label.execution_id)
    assert [r.review.verdict for r in hook.results] == ["revise", "approve", "revise"]
    assert out.kind == ESCALATED and out.rounds == 2 and "revision limit reached" in out.reason
    assert out.revisions == 1
    stages = [e.payload.get("stage") for e in orch.audit.read()]
    assert stages.count("tier3_revision") == 1  # one revision for the whole case, not one per round
    assert_case_totals(out, hook)


def test_reviewer_tokens_and_calls_are_in_the_run_totals(
    make_orch: Callable[..., Orchestrator], t3_labels: list[FailureLabel], dataset_dir: Path
) -> None:
    label = t3_labels[0]
    src = SyntheticSource(dataset_dir)
    orch, hook = build(
        make_orch,
        label,
        [lambda: scripted_model(label, src, "correct")],
        [lambda: scripted_reviewer(label, src, "strict")],
        dataset_dir,
        Clock(),
        verified=True,
    )
    out = orch.handle(label.execution_id)
    (res,) = hook.results
    assert res.budget.tool_calls == 1 and res.budget.tokens_used > 0
    assert out.investigation is not None
    inv_only = sum(c.total_tokens for c in out.investigation.budget.calls if c not in res.budget.calls)
    assert out.report.run.input_tokens + out.report.run.output_tokens == inv_only + res.budget.tokens_used


def test_an_exhausted_case_budget_fails_the_review_closed(
    make_orch: Callable[..., Orchestrator], t3_labels: list[FailureLabel], dataset_dir: Path
) -> None:
    label = t3_labels[0]

    def hog() -> FakeModel:
        call = ToolCall("c0", "classify_signature", {"execution_id": label.execution_id})
        body = {
            "classification": "user",
            "layer": "L4",
            "confidence": 0.7,
            "hypothesis": "the lockfile is stale",
            "evidence": [{"source": "c0", "finding": "a finding", "supports": "hypothesis"}],
            "remediation": tier3(label),
        }
        return FakeModel(
            [
                ModelResponse(tool_calls=[call], usage=Usage(20_000, 100), stop_reason="tool_use"),
                ModelResponse(tool_calls=[ToolCall("cf", SUBMIT_REPORT, body)], usage=Usage(20_000, 100)),
            ]
        )

    reviewer = verdict_model("approve")
    orch, hook = build(make_orch, label, [hog], [lambda: reviewer], dataset_dir, Clock())
    out = orch.handle(label.execution_id)
    assert out.kind == ESCALATED and out.pull_request is None
    assert hook.results[0].stop_reason == "budget_tokens" and hook.results[0].review.verdict == "reject"
    assert reviewer.requests == []  # the reviewer model was never called
