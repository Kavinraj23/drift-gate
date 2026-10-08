"""Tier 3 end to end through the orchestrator: diff checks, reviewer verdicts, one revision, PR record, audit."""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from driftgate.adapters.synthetic import SyntheticSource, SyntheticTarget
from driftgate.agents.investigator import SUBMIT_REPORT
from driftgate.domain import Review
from driftgate.eval.e2e import _scripted_factory
from driftgate.eval.ground_truth import FailureLabel
from driftgate.eval.scripted_agent import scripted_model
from driftgate.eval.tier3_scripts import BAD_KINDS, bad_diff
from driftgate.llm.fake import FakeModel
from driftgate.llm.types import ModelResponse, ToolCall, Usage
from driftgate.orchestrator import BRANCH_PREFIX, ESCALATED, PR_PROPOSED
from driftgate.tier3 import BRANCH_PREFIX as T3_PREFIX
from driftgate.tier3 import PullRequest

SRC = Path(__file__).resolve().parents[2] / "src" / "driftgate"
RunT3 = Callable[..., Any]

#: Where each seeded bad diff must be stopped by a strict reviewer: the deterministic checks or the reviewer.
CAUGHT_BY = {
    "wrong_file": "review",
    "delete_fix_file": "diff_check",
    "unrelated_workflow": "diff_check",  # except fix_undefined_variable, where workflows are in scope (see below)
    "secret_literal": "diff_check",
    "over_broad": "review",
    "huge": "diff_check",
    "subtractive_first": "diff_check",
}


def rejected_stage(run: Any) -> str:
    return str(run.audit[-1].payload.get("stage"))


# --- the happy path ------------------------------------------------------------------------------------------
def test_correct_diffs_are_approved_and_open_a_pr_for_every_scenario(
    run_t3: RunT3, t3_labels: list[FailureLabel]
) -> None:
    for label in t3_labels:
        run = run_t3(label)
        out = run.outcome
        assert out.kind == PR_PROPOSED, (label.fault_id, out.reason)
        assert out.report.review == Review("approve", out.report.review.comments)  # type: ignore[union-attr]
        assert out.pull_request is not None and out.pull_request.diff_text == label.fix_diff
        assert run.target.pull_requests == [out.pull_request]


def test_pr_is_opened_on_an_agent_branch_and_never_merged(run_t3: RunT3, t3_labels: list[FailureLabel]) -> None:
    run = run_t3(t3_labels[0])
    out = run.outcome
    assert BRANCH_PREFIX == T3_PREFIX == "driftgate/"
    assert out.pull_request is not None and out.pull_request.branch.startswith("driftgate/")
    assert out.pr_record is not None and out.pr_record.merged is False and out.pr_record.number == 1
    assert out.pull_request.base == "main"
    assert not [n for n in dir(run.target) if "merge" in n.lower()]  # there is no merge operation to call
    final = run.stage("pr_proposed")[-1]
    assert final.payload["merged"] is False and final.payload["branch"] == out.pull_request.branch
    assert out.report.remediation.dry_run["pull_request"]["merged"] is False  # type: ignore[union-attr]
    assert run.target.calls == []  # Tier 3 never runs dry_run/execute against the target


def test_target_refuses_branches_outside_the_agent_namespace() -> None:
    target = SyntheticTarget()
    for branch in ("main", "feature/fix", "driftgate", "driftgate/"):
        with pytest.raises(ValueError, match="driftgate/"):
            target.open_pull_request(PullRequest(branch, "main", "t", "b", "d", ("p",)))
    assert target.pull_requests == []


def test_pr_description_carries_the_evidence_bundle_and_the_reviewers_verdict(
    run_t3: RunT3, t3_labels: list[FailureLabel]
) -> None:
    run = run_t3(t3_labels[0])
    out = run.outcome
    body = out.pull_request.body  # type: ignore[union-attr]
    assert out.report.evidence
    for e in out.report.evidence:
        assert e.source in body and e.finding in body
    review = out.report.review
    assert review is not None and "Verdict: **approve**" in body and review.comments in body
    assert out.report.hypothesis in body and out.report.fingerprint in body
    assert "not been merged" in body
    assert out.report.remediation.dry_run["pull_request"]["description"] == body  # type: ignore[union-attr]


def test_report_and_audit_record_the_review(
    run_t3: RunT3, t3_labels: list[FailureLabel], report_validator: Draft202012Validator
) -> None:
    run = run_t3(t3_labels[0])
    out = run.outcome
    report_validator.validate(json.loads(json.dumps(out.report.to_dict())))
    assert out.report.review is not None and out.report.review.verdict == "approve"
    entries = run.stage("tier3_review")
    assert len(entries) == 1
    assert (entries[0].payload["verdict"], entries[0].payload["comments"]) == (
        out.report.review.verdict,
        out.report.review.comments,
    )
    diff_entry = run.stage("tier3_diff")[0]
    assert diff_entry.payload["ok"] is True and diff_entry.payload["files"] == list(t3_labels[0].fix_paths)
    kinds = [(e.kind, e.payload.get("stage")) for e in run.audit]
    assert (
        kinds.index(("gate_decision", None))
        < kinds.index(("proposal", "tier3_diff"))
        < kinds.index(("proposal", "tier3_review"))
        < kinds.index(("execution", "pr_proposed"))
    )


def test_structured_diff_is_stored_on_the_remediation(run_t3: RunT3, t3_labels: list[FailureLabel]) -> None:
    label = t3_labels[0]
    run = run_t3(label)
    dry = run.outcome.report.remediation.dry_run  # type: ignore[union-attr]
    assert dry["diff_text"] == label.fix_diff
    files = dry["diff"]["files"]
    assert [f["path"] for f in files] == list(label.fix_paths) and files[0]["hunks"]
    assert dry["diff"]["added"] > 0


def test_reviewer_usage_is_rolled_into_the_run_stats(run_t3: RunT3, t3_labels: list[FailureLabel]) -> None:
    run = run_t3(t3_labels[0])
    out = run.outcome
    inv = out.investigation
    assert inv is not None
    reviewer_calls = sum(r.budget.tool_calls for r in run.hook.results)
    assert reviewer_calls == 1 and out.report.run.tool_calls == inv.budget.tool_calls + reviewer_calls
    inv_tokens = inv.budget.run_fields()
    rev_tokens = sum(r.budget.tokens_used for r in run.hook.results)
    assert (
        out.report.run.input_tokens + out.report.run.output_tokens
        == inv_tokens["input_tokens"] + inv_tokens["output_tokens"] + rev_tokens
    )


def test_without_a_reviewer_hook_the_m5_behaviour_is_unchanged(
    make_orch: Callable[..., Any], t3_labels: list[FailureLabel]
) -> None:
    out = make_orch(t3_labels[0]).handle(t3_labels[0].execution_id)
    assert out.kind == PR_PROPOSED and out.pull_request is None and out.report.review is None


# --- seeded bad diffs ----------------------------------------------------------------------------------------
def _cases(labels: list[FailureLabel], dataset_dir: Path) -> list[tuple[str, FailureLabel]]:
    src = SyntheticSource(dataset_dir)
    return [(k, lb) for k in BAD_KINDS for lb in labels if bad_diff(k, lb, src) is not None]


def test_every_seeded_bad_diff_is_stopped_and_never_becomes_a_pr(
    run_t3: RunT3, t3_labels: list[FailureLabel], dataset_dir: Path
) -> None:
    cases = _cases(t3_labels, dataset_dir)
    assert len(cases) >= 50 and {k for k, _ in cases} == set(BAD_KINDS)
    for kind, label in cases:
        run = run_t3(label, f"diff:{kind}")
        out = run.outcome
        assert out.kind == ESCALATED and run.target.pull_requests == [] and out.pull_request is None, (
            kind,
            label.fault_id,
        )
        expected = CAUGHT_BY[kind]
        if kind == "unrelated_workflow" and label.correct_action == "fix_undefined_variable":
            expected = "review"
        assert rejected_stage(run) == expected, (kind, label.fault_id, out.reason)
        assert out.report.escalation_reason == out.reason and out.reason


def test_reviewer_catches_what_the_validator_cannot_see(run_t3: RunT3, t3_labels: list[FailureLabel]) -> None:
    for kind in ("wrong_file", "over_broad"):
        for label in t3_labels:
            run = run_t3(label, f"diff:{kind}")
            assert run.stage("tier3_diff")[0].payload["ok"] is True  # the deterministic checks pass it
            review = run.outcome.report.review
            assert review is not None and review.verdict == "reject" and review.comments
            assert run.outcome.reason.startswith("reviewer verdict reject:")


def test_validator_stops_bad_diffs_even_when_the_reviewer_would_approve(
    run_t3: RunT3, t3_labels: list[FailureLabel], dataset_dir: Path
) -> None:
    for kind in ("delete_fix_file", "secret_literal", "huge", "subtractive_first"):
        for label in t3_labels:
            if bad_diff(kind, label, SyntheticSource(dataset_dir)) is None:
                continue
            run = run_t3(label, f"diff:{kind}", "lenient")
            assert run.outcome.kind == ESCALATED and rejected_stage(run) == "diff_check", (kind, label.fault_id)
            assert run.reviewer_spies == []  # the reviewer is not even asked: checks run first
            assert run.outcome.report.review is None
            assert "diff check failed" in run.outcome.reason


def test_a_lenient_reviewer_lets_semantic_mistakes_through_so_the_reviewer_is_what_stops_them(
    run_t3: RunT3, t3_labels: list[FailureLabel]
) -> None:
    run = run_t3(t3_labels[0], "diff:wrong_file", "lenient")
    assert run.outcome.kind == PR_PROPOSED  # documents the residual risk: a weak reviewer is the only control here
    assert run_t3(t3_labels[0], "diff:wrong_file", "strict").outcome.kind == ESCALATED


def test_specific_violation_codes_are_reported(run_t3: RunT3, t3_labels: list[FailureLabel]) -> None:
    lock = next(lb for lb in t3_labels if lb.correct_action == "regenerate_lockfile")
    run = run_t3(lock, "diff:delete_fix_file")
    codes = [v["code"] for v in run.stage("tier3_diff")[0].payload["violations"]]
    assert "deletes_file" in codes and "diff check failed" in run.outcome.reason
    run = run_t3(lock, "diff:subtractive_first")
    assert [v["code"] for v in run.stage("tier3_diff")[0].payload["violations"]] == ["subtractive_before_additive"]
    run = run_t3(lock, "diff:secret_literal")
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps([e.payload for e in run.audit])  # codes, not literals
    assert [v["code"] for v in run.stage("tier3_diff")[0].payload["violations"]] == ["secret_literal"]


# --- hand-built investigator reports -------------------------------------------------------------------------
def report_model(label: FailureLabel, remediation: dict[str, Any] | None) -> FakeModel:
    eid = label.execution_id
    call = ToolCall("c0", "classify_signature", {"execution_id": eid})
    body: dict[str, Any] = {
        "classification": "user",
        "layer": "L4",
        "confidence": 0.7,
        "hypothesis": "the lockfile is stale",
        "evidence": [{"source": "c0", "finding": "a finding", "supports": "hypothesis"}],
    }
    if remediation is not None:
        body["remediation"] = remediation
    else:
        body.update(abstain=True, escalation_reason="no safe change")
    return FakeModel(
        [
            ModelResponse(tool_calls=[call], usage=Usage(500, 50), stop_reason="tool_use"),
            ModelResponse(
                tool_calls=[ToolCall("cf", SUBMIT_REPORT, body)], usage=Usage(600, 60), stop_reason="tool_use"
            ),
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


def test_tier3_without_a_diff_cannot_open_a_pr(
    make_orch: Callable[..., Any], t3_labels: list[FailureLabel], dataset_dir: Path
) -> None:
    from driftgate.tier3 import ReviewerHook

    label = t3_labels[0]
    src = SyntheticSource(dataset_dir)
    called: list[int] = []

    def reviewers(*_: Any) -> Any:
        called.append(1)
        raise AssertionError("the reviewer must not run without a valid diff")

    orch = make_orch(
        label,
        model_factory=lambda e, b: report_model(label, tier3(label, diff="")),
        tier3_review=ReviewerHook(src, reviewers),
    )
    out = orch.handle(label.execution_id)
    assert out.kind == ESCALATED and "no_diff" in out.reason and called == []


def test_diff_touching_an_undeclared_file_is_escalated(
    make_orch: Callable[..., Any], t3_labels: list[FailureLabel], dataset_dir: Path
) -> None:
    from driftgate.tier3 import ReviewerHook

    label = t3_labels[0]
    lying = tier3(label, paths=["package.json"])  # declares one file, the diff changes another
    orch = make_orch(
        label,
        model_factory=lambda e, b: report_model(label, lying),
        tier3_review=ReviewerHook(SyntheticSource(dataset_dir), lambda *_: None),  # type: ignore[arg-type, return-value]
    )
    out = orch.handle(label.execution_id)
    assert out.kind == ESCALATED and "undeclared_path" in out.reason


def test_gate_still_runs_first_and_a_killed_tier_never_reaches_the_reviewer(
    run_t3: RunT3, t3_labels: list[FailureLabel], tmp_path: Path
) -> None:
    kill = tmp_path / "kill.json"
    kill.write_text(json.dumps({"global": True, "tiers": {"0": True, "1": True, "2": True, "3": False}}), "utf-8")
    run = run_t3(t3_labels[0], kill_switch_path=kill)
    assert run.outcome.kind == ESCALATED and run.reviewer_spies == [] and run.target.pull_requests == []
    assert run.outcome.gate is not None and run.outcome.gate.decision != "allowed"


# --- verdicts: reject, revise, the one-revision cap ----------------------------------------------------------
def test_reject_escalates_and_records_the_comments(run_t3: RunT3, t3_labels: list[FailureLabel]) -> None:
    run = run_t3(t3_labels[0], "diff:wrong_file")
    out = run.outcome
    assert out.kind == ESCALATED and out.revisions == 0 and run.investigator_calls == 1
    assert out.report.review is not None and out.report.review.verdict == "reject"
    assert out.report.review.comments in out.reason
    assert run.audit[-1].kind == "rejected" and run.audit[-1].payload["stage"] == "review"
    assert run.target.pull_requests == []


def test_revise_sends_feedback_to_the_investigator_once_then_approves(
    run_t3: RunT3, t3_labels: list[FailureLabel]
) -> None:
    label = t3_labels[0]
    run = run_t3(label, "revise:wrong_file", "revising")
    out = run.outcome
    assert out.kind == PR_PROPOSED and out.revisions == 1
    assert run.investigator_calls == 2 and len(run.reviewer_spies) == 2
    assert [r.review.verdict for r in run.hook.results] == ["revise", "approve"]
    assert out.pull_request is not None and out.pull_request.diff_text == label.fix_diff  # the revised, correct diff
    assert "after 1 revision round" in out.pull_request.body
    revision = run.stage("tier3_revision")[0]
    assert run.hook.results[0].review.comments == revision.payload["feedback"]
    assert [e.payload["verdict"] for e in run.stage("tier3_review")] == ["revise", "approve"]
    assert [e.payload["round"] for e in run.stage("tier3_review")] == [0, 1]
    assert [e.payload["ok"] for e in run.stage("tier3_diff")] == [True, True]
    assert out.report.review is not None and out.report.review.verdict == "approve"
    assert out.report.run.tool_calls >= out.investigation.budget.tool_calls  # type: ignore[union-attr]
    assert out.report.evidence and {e.source for e in out.report.evidence} <= set(out.investigation.tool_call_ids)  # type: ignore[union-attr]


def test_the_investigator_sees_the_feedback_and_the_reviewer_sees_round_two(
    make_orch: Callable[..., Any], run_t3: RunT3, t3_labels: list[FailureLabel]
) -> None:
    run = run_t3(t3_labels[0], "revise:wrong_file", "revising")
    second = str(run.reviewer_spies[1].requests[0].messages[0]["content"])
    assert "review round 2" in second
    feedback = run.stage("tier3_revision")[0].payload["feedback"]
    assert feedback and "Please revise" in feedback


def test_revision_is_gated_and_counted_by_the_rate_limit(run_t3: RunT3, t3_labels: list[FailureLabel]) -> None:
    label = t3_labels[0]
    run = run_t3(label, "revise:wrong_file", "revising")
    assert (
        len(run.orch.audit.read("gate_decision")) == 2
    )  # the original and the revised proposal each went through the gate
    again = run.orch.handle(label.execution_id)  # a third attempt on the same fingerprint within the hour
    assert again.kind == ESCALATED and again.gate is not None and "rate limit" in again.gate.reason


def test_second_revise_escalates_instead_of_looping(run_t3: RunT3, t3_labels: list[FailureLabel]) -> None:
    run = run_t3(t3_labels[0], "diff:wrong_file", "revising")  # the investigator never gets it right
    out = run.outcome
    assert out.kind == ESCALATED and out.revisions == 1
    assert run.investigator_calls == 2 and len(run.reviewer_spies) == 2  # exactly one revision round, two reviews
    assert [r.review.verdict for r in run.hook.results] == ["revise", "revise"]
    assert "revision limit reached" in out.reason and run.target.pull_requests == []
    assert len(run.stage("tier3_revision")) == 1


def test_a_revision_that_breaks_the_diff_checks_is_escalated(
    make_orch: Callable[..., Any], t3_labels: list[FailureLabel], dataset_dir: Path
) -> None:
    from driftgate.eval.tier3_scripts import scripted_reviewer
    from driftgate.tier3 import ReviewerHook

    label = t3_labels[0]
    src = SyntheticSource(dataset_dir)
    calls: list[int] = []

    def investigators(e: str, b: Any) -> Any:
        calls.append(1)
        return scripted_model(label, src, "diff:wrong_file" if len(calls) == 1 else "diff:secret_literal")

    hook = ReviewerHook(src, lambda e, b: scripted_reviewer(label, src, "revising"))
    out = make_orch(label, model_factory=investigators, tier3_review=hook).handle(label.execution_id)
    assert out.kind == ESCALATED and "secret_literal" in out.reason and len(hook.results) == 1


def test_a_revision_that_abstains_is_escalated(
    make_orch: Callable[..., Any], t3_labels: list[FailureLabel], dataset_dir: Path
) -> None:
    from driftgate.eval.tier3_scripts import scripted_reviewer
    from driftgate.tier3 import ReviewerHook

    label = t3_labels[0]
    src = SyntheticSource(dataset_dir)
    calls: list[int] = []

    def investigators(e: str, b: Any) -> Any:
        calls.append(1)
        return scripted_model(label, src, "diff:wrong_file") if len(calls) == 1 else report_model(label, None)

    hook = ReviewerHook(src, lambda e, b: scripted_reviewer(label, src, "revising"))
    out = make_orch(label, model_factory=investigators, tier3_review=hook).handle(label.execution_id)
    assert out.kind == ESCALATED and out.reason == "no safe change" and out.pull_request is None


def test_a_garbled_reviewer_fails_closed(run_t3: RunT3, t3_labels: list[FailureLabel]) -> None:
    run = run_t3(t3_labels[0], reviewer="garbled")
    assert run.outcome.kind == ESCALATED and run.target.pull_requests == []
    assert run.outcome.report.review is not None and run.outcome.report.review.verdict == "reject"
    assert "failing closed" in run.outcome.report.review.comments


def test_audit_log_is_sequenced_and_complete_for_a_revised_case(run_t3: RunT3, t3_labels: list[FailureLabel]) -> None:
    run = run_t3(t3_labels[0], "revise:wrong_file", "revising")
    assert [e.seq for e in run.audit] == list(range(len(run.audit)))
    stages = [(e.kind, e.payload.get("stage")) for e in run.audit]
    assert stages[0] == ("proposal", "agent") and stages[-1] == ("execution", "pr_proposed")
    assert stages.count(("proposal", "tier3_diff")) == 2 and stages.count(("proposal", "gate_facts")) == 2


# --- architecture --------------------------------------------------------------------------------------------
def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names += [mod, *[f"{mod}.{a.name}" for a in node.names]]
    return names


def test_reviewer_imports_no_adapters_targets_gates_or_orchestration() -> None:
    forbidden = ("adapters", "orchestrator", "tier3", "gates", "SyntheticTarget", "eval", "generator", "audit")
    for name in _imports(SRC / "agents" / "reviewer.py"):
        parts = name.split(".")
        assert not (parts[0] == "driftgate" and len(parts) > 1 and parts[1] in forbidden), name
        assert "SyntheticTarget" not in name and "GitHubActionsTarget" not in name


def test_only_the_orchestrator_opens_pull_requests_on_a_target() -> None:
    offenders = [
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if ".open_pull_request(" in p.read_text(encoding="utf-8")
        and p.relative_to(SRC).as_posix() not in ("orchestrator.py", "adapters/synthetic.py", "tier3.py")
    ]
    assert offenders == []
    for sub in ("agents", "tools"):
        assert not any("open_pull_request" in p.read_text(encoding="utf-8") for p in (SRC / sub).rglob("*.py"))


def test_tier3_module_does_not_import_adapters() -> None:
    assert not [n for n in _imports(SRC / "tier3.py") + _imports(SRC / "diffs.py") if "adapters" in n]


def test_scripted_factory_is_the_one_the_e2e_uses(dataset_dir: Path, t3_labels: list[FailureLabel]) -> None:
    # guard for the helper used throughout this package: revise: variants start bad, then turn correct
    factory = _scripted_factory(t3_labels[0], SyntheticSource(dataset_dir), "revise:wrong_file")
    first, second = factory("e", None), factory("e", None)  # type: ignore[arg-type]
    assert first is not second
