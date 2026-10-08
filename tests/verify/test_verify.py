"""M7: verification, rollback, and the single re-investigation round. Offline, fake model, fake clock."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from driftgate.adapters.synthetic import SimulatedOutcome, SyntheticSource, SyntheticTarget
from driftgate.agents.investigator import SYSTEM_PROMPT
from driftgate.audit import AuditLog
from driftgate.domain import Remediation, Report
from driftgate.eval.e2e import DRILLS, run_all, run_drill
from driftgate.eval.ground_truth import FailureLabel, GroundTruth
from driftgate.eval.metrics import (
    Rate,
    recovery_breakdown,
    recovery_or_correct_escalation_rate,
    recovery_rate,
    remediation_success_rate,
)
from driftgate.eval.scripted_agent import (
    Plan,
    _parse_results,
    _report_input,
    build_reinvestigation_script,
    two_round_factory,
)
from driftgate.llm.budget import InvestigationBudget
from driftgate.llm.fake import FakeModel
from driftgate.llm.types import ModelRequest, ModelResponse, ToolCall, Usage
from driftgate.orchestrator import (
    ESCALATED,
    EXECUTED,
    PR_PROPOSED,
    Orchestrator,
    Outcome,
    make_synthetic_target,
)
from driftgate.tools import ToolContext, dispatch
from driftgate.tools.attempts import AttemptStore, RemediationAttempt
from driftgate.tools.base import FailureAnalyzer
from driftgate.verify import VERIFIED_BY_CI, Verifier

MakeOrch = Callable[..., Orchestrator]


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


@pytest.fixture
def by_sid(truth: GroundTruth) -> Callable[[str], FailureLabel]:
    return lambda sid: next(s for s in truth.scenarios() if s.scenario_id == sid)


class Rig:
    """One two-round run: records every model and budget the orchestrator asks for."""

    def __init__(self, dataset: Path, label: FailureLabel, first: str, second: str) -> None:
        self.inner = two_round_factory(label, SyntheticSource(dataset), first, second)
        self.models: list[FakeModel] = []
        self.budgets: list[InvestigationBudget] = []
        self.before_round: dict[int, Callable[[], None]] = {}

    def __call__(self, eid: str, budget: InvestigationBudget) -> FakeModel:
        n = len(self.models) + 1
        if n in self.before_round:
            self.before_round[n]()
        model = self.inner(eid, budget)
        self.models.append(model)
        self.budgets.append(budget)
        return model


@pytest.fixture
def run(
    make_orch: MakeOrch, dataset_dir: Path, by_sid: Callable[[str], FailureLabel]
) -> Callable[..., tuple[Outcome, Orchestrator, Rig]]:
    def _run(
        sid: str,
        first: str,
        second: str,
        *,
        verified: bool = True,
        rig_hook: Callable[[Rig], None] | None = None,
        **kw: Any,
    ) -> tuple[Outcome, Orchestrator, Rig]:
        label = by_sid(sid)
        rig = Rig(dataset_dir, label, first, second)
        if rig_hook:
            rig_hook(rig)
        kw.setdefault("target", make_synthetic_target(dataset_dir, verified=verified))
        orch = make_orch(label, model_factory=rig, verification=True, **kw)
        return orch.handle(label.execution_id), orch, rig

    return _run


def kinds(orch: Orchestrator) -> list[tuple[str, Any]]:
    return [(e.kind, e.payload.get("stage")) for e in orch.audit.read()]


# --- verified -> closed -------------------------------------------------------------------------------------------
def test_verified_tier0_marks_attempt_successful_and_closes(run) -> None:  # type: ignore[no-untyped-def]
    out, orch, rig = run("sc-06", "correct", "revise_escalate")
    assert out.kind == EXECUTED and out.verified is True and out.rounds == 1 and not out.first_attempt_failed
    (attempt,) = orch.attempts.all()
    assert attempt.outcome == "verified" and attempt.verification["verified"] is True
    assert attempt.verification["recurred"] is False
    assert len(rig.models) == 1  # no re-investigation
    assert ("verification", "result") in kinds(orch) and ("verification", "closed") in kinds(orch)
    assert ("verification", "rollback") not in kinds(orch)


def test_tier3_green_ci_is_noted_in_the_pr_record(run) -> None:  # type: ignore[no-untyped-def]
    out, orch, _ = run("sc-17", "correct", "revise_escalate")
    assert out.kind == PR_PROPOSED and out.verified is True
    assert out.report.remediation.dry_run["ci"] == {"status": "green", "note": VERIFIED_BY_CI}  # type: ignore[union-attr]
    assert orch.attempts.all()[0].outcome == "verified"


def test_tier3_red_ci_is_an_unverified_remediation_and_triggers_reinvestigation(run) -> None:  # type: ignore[no-untyped-def]
    out, orch, rig = run("sc-17", "wrong_paths", "revise_escalate")
    assert out.first_attempt_failed and out.rounds == 2 and len(rig.models) == 2
    first = orch.attempts.all()[0]
    assert first.outcome == "failed" and first.verification["details"]["ci"] == "red"
    assert first.verification["rolled_back"] is False  # a PR that was never merged has nothing to undo


# --- not verified: failed, rollback, visible to the agent -------------------------------------------------------
def test_unverified_reversible_action_is_rolled_back_and_marked_failed(run, dataset_dir: Path) -> None:  # type: ignore[no-untyped-def]
    target = SyntheticTarget(default=SimulatedOutcome(verified=False), data_dir=dataset_dir)
    out, orch, _ = run("sc-06", "correct", "revise_escalate", target=target)
    assert [c[0] for c in target.calls] == ["dry_run", "execute", "verify", "rollback"]
    assert len(target.rolled_back) == 1
    (attempt,) = orch.attempts.all()
    assert attempt.outcome == "failed" and attempt.verification["verified"] is False
    assert attempt.verification["rolled_back"] is True
    assert ("verification", "rollback") in kinds(orch)
    assert out.first_attempt_failed


def test_irreversible_action_is_not_rolled_back(dataset_dir: Path, tmp_path: Path) -> None:
    source = SyntheticSource(dataset_dir)
    eid = next(e.execution_id for e in source.list_executions(None, {"status": "failed"}))
    analysis = FailureAnalyzer(source).analyze(eid)
    assert analysis is not None
    target = SyntheticTarget(default=SimulatedOutcome(verified=False))
    attempts = AttemptStore()
    audit = AuditLog(tmp_path / "a.jsonl", FakeClock())
    rem = Remediation(0, "rerun_failed_job", "x", reversible=False, gate="auto")
    report = Report(eid, analysis.fingerprint, "transient", "L4", 0.5, "h", remediation=rem)
    out = Outcome(eid, EXECUTED, report, attempt_id="a1")
    attempts.add(RemediationAttempt("a1", analysis.fingerprint, eid, 0, rem.action, "executed"))
    v = Verifier(target, attempts, audit, FailureAnalyzer(source)).verify(out)
    assert not v.verified and target.rolled_back == []
    assert attempts.get("a1").verification["rolled_back"] is False  # type: ignore[union-attr]
    (entry,) = [e for e in audit.read() if e.payload.get("stage") == "rollback"]
    assert "irreversible" in entry.payload["description"]


def test_failed_attempt_is_readable_through_get_remediation_attempt(run, dataset_dir: Path) -> None:  # type: ignore[no-untyped-def]
    target = SyntheticTarget(default=SimulatedOutcome(verified=False), data_dir=dataset_dir)
    out, orch, _ = run("sc-06", "correct", "revise_escalate", target=target)
    ctx = ToolContext(SyntheticSource(dataset_dir), orch.attempts)
    res = dispatch(ctx, "get_remediation_attempt", {"fingerprint": out.report.fingerprint}, "t1")
    assert res.ok
    (row,) = res.data["attempts"]
    assert row["action"] == "rerun_failed_job" and row["outcome"] == "failed"
    assert row["verification"]["verified"] is False
    # the second round did read it (the scripted agent cites the call)
    assert any(e.finding.startswith("1 prior attempt(s)") for e in out.report.evidence)


def test_next_execution_log_is_new_evidence_and_the_fingerprint_recurred(dataset_dir: Path) -> None:
    res = run_drill(dataset_dir, DRILLS[0])  # throttling re-run that fails again with the masked state lock
    out = res.outcome
    assert out is not None and out.first_attempt_failed
    first = out.report.prior_attempts[0]
    assert first["verification"]["recurred"] is True
    assert "Error acquiring the state lock" in first["verification"]["details"]["next_log"]
    assert first["verification"]["rolled_back"] is True


# --- re-investigation: once, with the failed attempt as evidence ---------------------------------------------------
def test_reinvestigation_context_goes_in_the_user_message_only(run) -> None:  # type: ignore[no-untyped-def]
    out, _, rig = run("sc-17", "wrong_paths", "right_paths")
    first_req, second_req = rig.models[0].requests[0], rig.models[1].requests[0]
    assert "RE-INVESTIGATION" not in first_req.messages[0]["content"]
    assert "RE-INVESTIGATION" in second_req.messages[0]["content"]
    assert first_req.system == second_req.system == SYSTEM_PROMPT
    assert first_req.tools == second_req.tools


def test_wrong_file_then_right_file_recovers_and_is_verified(run) -> None:  # type: ignore[no-untyped-def]
    out, orch, rig = run("sc-17", "wrong_paths", "right_paths")
    assert out.kind == PR_PROPOSED and out.verified is True and out.rounds == 2 and out.first_attempt_failed
    assert [a.outcome for a in orch.attempts.all()] == ["failed", "verified"]
    assert out.report.hypothesis.startswith("Revised:")
    assert [r["round"] for r in out.report.prior_attempts] == [1, 2]


def test_only_one_reinvestigation_round(run) -> None:  # type: ignore[no-untyped-def]
    out, orch, rig = run("sc-17", "wrong_paths", "wrong_paths")
    assert len(rig.models) == 2  # first round + exactly one re-investigation
    assert rig.budgets[0].reinvestigations == 1 and rig.budgets[1].reinvestigations == 1
    assert not rig.budgets[1].begin_reinvestigation()
    assert len(orch.attempts.all()) == 2


def test_second_failure_escalates_with_both_attempts_and_the_revised_reasoning(  # type: ignore[no-untyped-def]
    run, report_validator: Draft202012Validator
) -> None:
    out, _, _ = run("sc-17", "wrong_paths", "wrong_paths")
    assert out.kind == ESCALATED and "second failure" in out.reason
    pa = out.report.prior_attempts
    assert len(pa) == 2 and all(a["outcome"] == "failed" for a in pa)
    assert pa[0]["hypothesis"] and out.report.hypothesis.startswith("Revised:")
    assert out.report.escalation_reason == out.reason
    report_validator.validate(json.loads(json.dumps(out.report.to_dict())))


def test_original_evidence_is_kept_and_every_citation_is_a_real_tool_call(run) -> None:  # type: ignore[no-untyped-def]
    out, _, _ = run("sc-17", "wrong_paths", "wrong_paths")
    assert out.investigation is not None
    own = set(out.earlier_tool_call_ids) | set(out.investigation.tool_call_ids)
    assert {e.source for e in out.report.evidence} <= own
    assert any(e.source in out.earlier_tool_call_ids for e in out.report.evidence)  # original evidence present
    assert any(e.source in out.investigation.tool_call_ids for e in out.report.evidence)


def test_tier0_retry_failure_hard_escalates_when_the_agent_retries_again(run, dataset_dir: Path) -> None:  # type: ignore[no-untyped-def]
    target = SyntheticTarget(default=SimulatedOutcome(verified=False), data_dir=dataset_dir)
    out, orch, _ = run("sc-06", "correct", "repeat_tier0", target=target)
    assert out.kind == ESCALATED and out.gate is not None and out.gate.decision == "refused"
    assert "Tier 0 retry" in out.reason
    assert len(out.report.prior_attempts) == 1
    assert len([c for c in target.calls if c[0] == "execute"]) == 1  # the second re-run never reached the target


def test_failure_after_an_earlier_failed_attempt_on_the_fingerprint_hard_escalates(  # type: ignore[no-untyped-def]
    make_orch: MakeOrch, dataset_dir: Path, by_sid
) -> None:
    label = by_sid("sc-17")
    rig = Rig(dataset_dir, label, "wrong_paths", "right_paths")
    orch = make_orch(label, model_factory=rig, verification=True, target=make_synthetic_target(dataset_dir))
    fp = "unknown"
    # learn the fingerprint, then seed a failed Tier 0 attempt for it
    fp = next(r for r in [dispatch(orch._ctx, "classify_signature", {"execution_id": label.execution_id}, "x")]).data[
        "fingerprint"
    ]
    orch.attempts.add(RemediationAttempt("old:1", fp, "ex-old", 0, "rerun_failed_job", "failed", {"verified": False}))
    out = orch.handle(label.execution_id)
    assert out.kind == ESCALATED and "earlier failed attempt" in out.reason
    assert len(rig.models) == 1  # no re-investigation
    assert len(out.report.prior_attempts) == 2


def test_reinvestigation_must_read_the_attempt_and_revise_the_hypothesis(run) -> None:  # type: ignore[no-untyped-def]
    out, orch, _ = run("sc-17", "wrong_paths", "no_read")
    assert out.kind == ESCALATED and "without reading the failed attempt" in out.reason
    assert out.gate is None and len(orch.attempts.all()) == 1  # nothing executed or proposed in round 2
    out2, _, _ = run("sc-17", "wrong_paths", "same_hypothesis")
    assert out2.kind == ESCALATED and "did not revise the first hypothesis" in out2.reason


# --- gate, rate limit, kill switch and ceiling still bind re-proposals --------------------------------------------
def test_exhausted_rate_limit_hard_escalates_the_second_proposal(  # type: ignore[no-untyped-def]
    make_orch: MakeOrch, dataset_dir: Path, by_sid
) -> None:
    label = by_sid("sc-17")
    clock = FakeClock()
    rig = Rig(dataset_dir, label, "wrong_paths", "right_paths")
    orch = make_orch(
        label, model_factory=rig, clock=clock, verification=True, target=make_synthetic_target(dataset_dir)
    )
    fp = dispatch(orch._ctx, "classify_signature", {"execution_id": label.execution_id}, "x").data["fingerprint"]
    orch._gate._rate.record(fp)  # one allowance already spent on this fingerprint within the hour
    out = orch.handle(label.execution_id)
    assert out.kind == ESCALATED and out.gate is not None and out.gate.decision == "refused"
    assert "rate limit" in out.reason
    assert len(out.report.prior_attempts) == 1 and out.first_attempt_failed


def test_rate_limit_window_expires_on_the_fake_clock(make_orch: MakeOrch, dataset_dir: Path, by_sid) -> None:  # type: ignore[no-untyped-def]
    label = by_sid("sc-17")
    clock = FakeClock()
    rig = Rig(dataset_dir, label, "wrong_paths", "right_paths")
    orch = make_orch(
        label, model_factory=rig, clock=clock, verification=True, target=make_synthetic_target(dataset_dir)
    )
    fp = dispatch(orch._ctx, "classify_signature", {"execution_id": label.execution_id}, "x").data["fingerprint"]
    orch._gate._rate.record(fp)
    clock.t += 3601  # an hour later the earlier attempt no longer counts
    out = orch.handle(label.execution_id)
    assert out.kind == PR_PROPOSED and out.verified is True


def test_kill_switch_flipped_between_rounds_blocks_the_reproposal(  # type: ignore[no-untyped-def]
    make_orch: MakeOrch, dataset_dir: Path, by_sid, kill_file: Path
) -> None:
    label = by_sid("sc-17")
    rig = Rig(dataset_dir, label, "wrong_paths", "right_paths")
    rig.before_round[2] = lambda: kill_file.write_text(
        json.dumps({"global": True, "tiers": {"0": True, "1": True, "2": True, "3": False}}), encoding="utf-8"
    )
    orch = make_orch(label, model_factory=rig, verification=True, target=make_synthetic_target(dataset_dir))
    out = orch.handle(label.execution_id)
    assert out.kind == ESCALATED and out.gate is not None and out.gate.decision == "refused"
    assert "kill switch" in out.reason


def test_abort_ceiling_binds_the_reproposal(make_orch: MakeOrch, dataset_dir: Path, by_sid) -> None:  # type: ignore[no-untyped-def]
    label = by_sid("sc-17")
    rig = Rig(dataset_dir, label, "wrong_paths", "right_paths")
    orch = make_orch(label, model_factory=rig, verification=True, target=make_synthetic_target(dataset_dir))
    rig.before_round[2] = lambda: setattr(orch._gate, "_ceiling", -1)  # blast radius now exceeds the ceiling
    out = orch.handle(label.execution_id)
    assert out.kind == ESCALATED and out.gate is not None and out.gate.decision == "downgraded"
    assert "ceiling" in out.reason


def test_gate_never_raises_a_tier_on_reproposal(run) -> None:  # type: ignore[no-untyped-def]
    out, _, _ = run("sc-06", "correct", "repeat_tier0", verified=False)
    assert out.gate is not None and out.gate.effective_tier is None  # type: ignore[union-attr]


# --- budget rollup, audit, schema -------------------------------------------------------------------------------
def test_both_rounds_share_one_budget_and_the_run_rollup_matches_it(run) -> None:  # type: ignore[no-untyped-def]
    out, _, rig = run("sc-17", "wrong_paths", "right_paths")
    assert len(rig.budgets) == 2 and rig.budgets[0] is rig.budgets[1]  # one budget for the whole case
    shared = rig.budgets[0]
    assert shared.reinvestigations == 1
    run_ = out.report.run
    assert (run_.tool_calls, run_.input_tokens, run_.output_tokens) == (
        shared.tool_calls,
        shared.run_fields()["input_tokens"],
        shared.run_fields()["output_tokens"],
    )
    assert run_.tool_calls == len(out.earlier_tool_call_ids) + len(out.investigation.tool_call_ids)  # type: ignore[union-attr]
    assert out.model_calls == len(shared.calls)
    assert out.tool_calls == run_.tool_calls and out.tokens == run_.input_tokens + run_.output_tokens


def test_a_two_round_case_never_exceeds_the_per_investigation_limits(table) -> None:  # type: ignore[no-untyped-def]
    two_round = [r for r in table if r.outcome is not None and r.outcome.rounds == 2]
    assert len(two_round) >= len(DRILLS)
    for r in two_round:
        o = r.outcome
        budget = o.investigation.budget  # type: ignore[union-attr]
        assert o.tool_calls <= budget.limits.max_tool_calls == 8, r.row.scenario_id
        assert budget.tokens_used <= budget.limits.max_total_tokens == 40_000, r.row.scenario_id
        assert budget.reinvestigations == 1


def test_round_one_spending_all_tool_calls_truncates_round_two_and_escalates(  # type: ignore[no-untyped-def]
    make_orch: MakeOrch, dataset_dir: Path, by_sid
) -> None:
    label, source = by_sid("sc-17"), SyntheticSource(dataset_dir)
    eid = label.execution_id
    wrong = {"tier": 3, "action": label.correct_action, "rationale": "docs", "reversible": True, "paths": ["README.md"]}
    plan = Plan([], label.true_classification, label.true_layer, 0.7, wrong)
    turns: list = [  # eight tool calls, one per turn, then the report
        ModelResponse(
            tool_calls=[
                ToolCall(f"toolu_{eid}_{i}", "get_execution" if i else "classify_signature", {"execution_id": eid})
            ],
            usage=Usage(input_tokens=900, output_tokens=100),
            stop_reason="tool_use",
        )
        for i in range(8)
    ]

    def final(request: ModelRequest) -> ModelResponse:
        data = _report_input(plan, _parse_results(request), "correct")
        return ModelResponse(tool_calls=[ToolCall("toolu_final", "submit_report", data)], usage=Usage(10, 10))

    first = FakeModel([*turns, final])
    models = iter([first, FakeModel(build_reinvestigation_script(label, source, "right_paths"))])
    budgets: list[InvestigationBudget] = []

    def factory(_eid: str, budget: InvestigationBudget) -> FakeModel:
        budgets.append(budget)
        return next(models)

    orch = make_orch(label, model_factory=factory, verification=True, target=make_synthetic_target(dataset_dir))
    out = orch.handle(eid)
    assert budgets[0] is budgets[1] and budgets[0].tool_calls == 8
    assert out.rounds == 2 and out.kind == ESCALATED
    assert out.report.budget_truncated is True and out.report.abstained is True
    assert out.report.run.tool_calls == 8  # round 2 got nothing: it never ran a tool
    assert out.tool_calls <= 8
    assert len(out.report.prior_attempts) >= 1  # the failed attempt travels with the escalation
    assert out.first_round is not None and out.first_round.report.remediation is not None


def test_audit_covers_verification_rollback_reinvestigation_and_escalation(run) -> None:  # type: ignore[no-untyped-def]
    out, orch, _ = run("sc-06", "correct", "revise_escalate", verified=False)
    ks = kinds(orch)
    for expected in (
        ("verification", "result"),
        ("verification", "rollback"),
        ("proposal", "reinvestigation_start"),
    ):
        assert expected in ks
    assert ks.index(("verification", "result")) < ks.index(("verification", "rollback"))
    assert ks.index(("verification", "rollback")) < ks.index(("proposal", "reinvestigation_start"))
    assert ks[-1][0] == "rejected"  # the round-2 escalation is the last thing audited
    seqs = [e.seq for e in orch.audit.read()]
    assert seqs == list(range(len(seqs)))


def test_audit_of_a_second_failure(run) -> None:  # type: ignore[no-untyped-def]
    out2, orch2, _ = run("sc-17", "wrong_paths", "wrong_paths")
    assert ("rejected", "verification_escalation") in kinds(orch2)
    results = [e for e in orch2.audit.read() if e.payload.get("stage") == "result"]
    assert [e.payload["verified"] for e in results] == [False, False]
    assert out2.kind == ESCALATED


def test_every_two_round_report_validates_against_the_contract(  # type: ignore[no-untyped-def]
    run, report_validator: Draft202012Validator
) -> None:
    for sid, first, second, verified in (
        ("sc-17", "wrong_paths", "right_paths", True),
        ("sc-17", "wrong_paths", "wrong_paths", True),
        ("sc-06", "correct", "repeat_tier0", False),
        ("sc-06", "correct", "revise_escalate", False),
        ("sc-17", "wrong_paths", "no_read", True),
    ):
        out, _, _ = run(sid, first, second, verified=verified)
        report_validator.validate(json.loads(json.dumps(out.report.to_dict())))


def test_tier1_awaiting_approval_is_not_verified(run) -> None:  # type: ignore[no-untyped-def]
    out, orch, _ = run("sc-10", "correct", "revise_escalate")
    assert out.kind == "awaiting_approval" and out.verified is None and orch.attempts.all() == []


def test_verification_is_off_unless_requested(make_orch: MakeOrch, by_sid) -> None:  # type: ignore[no-untyped-def]
    label = by_sid("sc-06")
    orch = make_orch(label)
    out = orch.handle(label.execution_id)
    assert out.kind == EXECUTED and out.verified is None
    assert [c[0] for c in orch.target.calls] == ["dry_run", "execute"]  # type: ignore[attr-defined]


# --- end to end: wrong first fix recovers or escalates correctly ---------------------------------------------------
@pytest.fixture(scope="module")
def table(dataset_dir: Path):  # type: ignore[no-untyped-def]
    return run_all(dataset_dir, mode="fake", drills=True)


def test_wrong_first_fix_scenarios_escalate_correctly(table) -> None:  # type: ignore[no-untyped-def]
    rows = {r.row.scenario_id: r for r in table}
    for sid in ("sc-11", "sc-29"):  # the real gate stops the tempting re-run of a masked state lock
        r = rows[sid]
        assert r.row.status == "ok" and r.outcome.kind == ESCALATED and r.row.recovery  # type: ignore[union-attr]
        assert r.outcome.gate.decision == "downgraded" and r.outcome.execution is None  # type: ignore[union-attr]


def test_every_drill_ends_correctly(table) -> None:  # type: ignore[no-untyped-def]
    drills = [r for r in table if r.row.scenario_id.startswith("d")]
    assert len(drills) == len(DRILLS)
    assert all(r.row.status == "ok" and r.row.rounds == 2 for r in drills), [
        (r.row.scenario_id, r.row.actual) for r in drills
    ]
    recov = {r.row.scenario_id: r.row.recovery for r in drills}
    assert recov["d1-wrong-rerun"] == "recovered (correct escalation)"
    assert recov["d3-wrong-file"] == "recovered (verified fix)"
    assert recov["d4-wrong-file-twice"] == "not recovered"


def test_headline_metrics_are_computed_from_the_cases(table) -> None:  # type: ignore[no-untyped-def]
    cases = [(r.label, r.outcome) for r in table if r.outcome is not None]
    b = recovery_breakdown(cases)
    assert recovery_rate(cases) == b.fixed == Rate(1, 5)  # PRD: only a verified fix after a failed attempt
    assert b.correct_escalation == Rate(2, 5) and b.not_recovered == Rate(2, 5)
    assert recovery_or_correct_escalation_rate(cases) == Rate(3, 5)  # the earlier, looser figure
    assert b.gate_blocked.numerator == 2  # sc-11 and sc-29: the real gate stopped the wrong first fix
    success = remediation_success_rate(cases)
    assert success.denominator > 10 and 0 < success.numerator < success.denominator
    assert recovery_rate([]).value is None and remediation_success_rate([]).value is None


def test_every_table_report_validates(table, report_validator: Draft202012Validator) -> None:  # type: ignore[no-untyped-def]
    for r in table:
        report_validator.validate(json.loads(json.dumps(r.outcome.report.to_dict())))  # type: ignore[union-attr]


# --- round 1 is kept, not overwritten --------------------------------------------------------------------------
def test_round_one_data_survives_a_round_two_that_escalates_before_executing(run) -> None:  # type: ignore[no-untyped-def]
    out, orch, rig = run("sc-06", "correct", "revise_escalate", verified=False)
    (attempt,) = orch.attempts.all()
    assert out.rounds == 2 and out.kind == ESCALATED and out.execution is None  # round 2 never executed anything
    assert out.attempt_id == attempt.attempt_id  # the last real attempt id is not blanked
    snap = out.first_round
    assert snap is not None and snap.attempt_id == attempt.attempt_id and snap.rounds == 1
    assert snap.report is not out.report and snap.report.hypothesis != out.report.hypothesis
    assert snap.kind == EXECUTED and snap.execution is not None
    # round-1 tool results and ids are held in the earlier_* fields, and match what round 1 ran
    assert [r.source for r in out.earlier_tool_results] == out.earlier_tool_call_ids != []
    assert set(out.earlier_tool_call_ids).isdisjoint(out.investigation.tool_call_ids)  # type: ignore[union-attr]
    assert out.earlier_model_calls >= 1 and out.model_calls > out.earlier_model_calls


# --- rollback: protocol, availability, kill switch ---------------------------------------------------------------
class NoRollbackTarget:
    """A RemediationTarget without the optional rollback capability (delegates the three protocol methods)."""

    def __init__(self, inner: SyntheticTarget) -> None:
        self._inner = inner

    def dry_run(self, action: Remediation):  # type: ignore[no-untyped-def]
        return self._inner.dry_run(action)

    def execute(self, action: Remediation):  # type: ignore[no-untyped-def]
        return self._inner.execute(action)

    def verify(self, action: Remediation):  # type: ignore[no-untyped-def]
        return self._inner.verify(action)


def _verifier_case(dataset_dir: Path, tmp_path: Path, target: Any, kill: Any = None):  # type: ignore[no-untyped-def]
    source = SyntheticSource(dataset_dir)
    eid = next(e.execution_id for e in source.list_executions(None, {"status": "failed"}))
    analysis = FailureAnalyzer(source).analyze(eid)
    assert analysis is not None
    attempts, audit = AttemptStore(), AuditLog(tmp_path / "a.jsonl", FakeClock())
    rem = Remediation(0, "rerun_failed_job", "x", reversible=True, gate="auto")
    report = Report(eid, analysis.fingerprint, "transient", "L4", 0.5, "h", remediation=rem)
    out = Outcome(eid, EXECUTED, report, attempt_id="a1")
    attempts.add(RemediationAttempt("a1", analysis.fingerprint, eid, 0, rem.action, "executed"))
    verifier = Verifier(target, attempts, audit, FailureAnalyzer(source), kill)
    return verifier, out, attempts, audit


def test_target_without_rollback_is_audited_as_rollback_unavailable(dataset_dir: Path, tmp_path: Path) -> None:
    target = NoRollbackTarget(SyntheticTarget(default=SimulatedOutcome(verified=False)))
    verifier, out, attempts, audit = _verifier_case(dataset_dir, tmp_path, target)
    v = verifier.verify(out)
    assert not v.verified and v.rollback_blocked == ""
    assert attempts.get("a1").verification["rolled_back"] is False  # type: ignore[union-attr]
    (entry,) = [e for e in audit.read() if e.payload.get("stage") == "rollback"]
    assert "rollback unavailable" in entry.payload["description"] and entry.payload["rolled_back"] is False


def test_tripped_kill_switch_skips_the_rollback_and_says_so(dataset_dir: Path, tmp_path: Path) -> None:
    from driftgate.gates import KillSwitch

    kill = tmp_path / "kill.json"
    kill.write_text(json.dumps({"global": False, "tiers": {"0": True}}), encoding="utf-8")
    target = SyntheticTarget(default=SimulatedOutcome(verified=False))
    verifier, out, attempts, audit = _verifier_case(dataset_dir, tmp_path, target, KillSwitch(kill))
    v = verifier.verify(out)
    assert "global kill switch" in v.rollback_blocked and target.rolled_back == []
    assert attempts.get("a1").verification["rolled_back"] is False  # type: ignore[union-attr]
    (entry,) = [e for e in audit.read() if e.payload.get("stage") == "rollback"]
    assert entry.payload["skipped_by_kill_switch"] is True and "rollback skipped" in entry.payload["description"]


def test_kill_switch_tripped_after_execution_escalates_without_reinvestigating(  # type: ignore[no-untyped-def]
    make_orch: MakeOrch, dataset_dir: Path, by_sid, kill_file: Path
) -> None:
    label = by_sid("sc-06")
    inner = SyntheticTarget(default=SimulatedOutcome(verified=False), data_dir=dataset_dir)

    class TripOnVerify(NoRollbackTarget):
        rollback = inner.rollback

        def verify(self, action: Remediation):  # type: ignore[no-untyped-def]
            kill_file.write_text(json.dumps({"global": False, "tiers": {}}), encoding="utf-8")
            return inner.verify(action)

    rig = Rig(dataset_dir, label, "correct", "revise_escalate")
    orch = make_orch(label, model_factory=rig, verification=True, target=TripOnVerify(inner))
    out = orch.handle(label.execution_id)
    assert out.kind == ESCALATED and out.rounds == 1 and len(rig.models) == 1  # no second investigation
    assert "kill switch" in out.reason and inner.rolled_back == []
    assert any(e.payload.get("skipped_by_kill_switch") for e in orch.audit.read())
