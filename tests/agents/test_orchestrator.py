"""Orchestrator: pre-filter, gate facts computed in code, SafetyGate integration, target access, audit, hooks."""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from driftgate.adapters.synthetic import SyntheticTarget
from driftgate.agents.investigator import SUBMIT_REPORT
from driftgate.domain import Review
from driftgate.eval.e2e import StepClock
from driftgate.eval.ground_truth import FailureLabel
from driftgate.llm.fake import FakeModel
from driftgate.llm.types import ModelResponse, ToolCall, Usage
from driftgate.orchestrator import (
    AWAITING_APPROVAL,
    BRANCH_PREFIX,
    CLOSED,
    ESCALATED,
    EXECUTED,
    FLEET_ABORT_CEILING,
    PR_PROPOSED,
    Orchestrator,
    Outcome,
)
from driftgate.tools.attempts import RemediationAttempt

SRC = Path(__file__).resolve().parents[2] / "src" / "driftgate"
MakeOrch = Callable[..., Orchestrator]


def hand_model(
    label: FailureLabel, remediation: dict | None, confidence: float = 0.9, tools=("get_execution",)
) -> FakeModel:  # type: ignore[no-untyped-def]
    """A model that calls only `tools` and then reports, proposing `remediation` (whatever the facts are)."""
    eid = label.execution_id
    calls = [ToolCall(f"c{i}", name, {"execution_id": eid}) for i, name in enumerate(tools)]
    body = {
        "classification": "transient",
        "layer": "L4",
        "confidence": confidence,
        "hypothesis": "h",
        "evidence": [{"source": c.id, "finding": "f", "supports": "s"} for c in calls],
    }
    if remediation:
        body["remediation"] = remediation
    else:
        body["escalation_reason"] = "nothing safe to do"
    first = ModelResponse(tool_calls=calls, usage=Usage(800, 60), stop_reason="tool_use")
    final = ModelResponse(
        tool_calls=[ToolCall("cfinal", SUBMIT_REPORT, body)], usage=Usage(900, 80), stop_reason="tool_use"
    )
    return FakeModel([first, final])


RERUN = {"tier": 0, "action": "rerun_failed_job", "rationale": "transient", "reversible": True}


def kinds(orch: Orchestrator) -> list[str]:
    return [e.kind for e in orch.audit.read()]


# --- pre-filter -----------------------------------------------------------------------------------------------


def test_governance_is_closed_by_the_prefilter_with_zero_model_calls(make_orch: MakeOrch, pick) -> None:
    label = pick("governance_approval_rejected")
    calls: list[str] = []

    def must_not_be_asked(eid: str, budget: object) -> FakeModel:
        calls.append(eid)
        raise AssertionError("the model factory was called for a governance outcome")

    orch = make_orch(label, model_factory=must_not_be_asked)
    out = orch.handle(label.execution_id)
    assert out.kind == CLOSED and calls == [] and out.model_calls == 0 and out.tool_calls == 0
    assert out.report.classification == "governance" and out.report.layer == "L5" and out.report.remediation is None
    assert [(e.kind, e.payload["stage"]) for e in orch.audit.read()] == [("rejected", "prefilter")]


def test_a_successful_execution_is_not_investigated(make_orch: MakeOrch, pick, dataset_dir: Path) -> None:
    from driftgate.adapters.synthetic import SyntheticSource

    ok = next(s for s in SyntheticSource(dataset_dir).list_executions(None, {"status": "success"}))
    orch = make_orch(pick("transient_throttling", fleet=False))
    with pytest.raises(ValueError):
        orch.handle(ok.execution_id)


# --- the happy paths ------------------------------------------------------------------------------------------


def test_tier0_throttling_runs_dry_run_then_execute_and_audits_every_step(make_orch: MakeOrch, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    target = SyntheticTarget()
    orch = make_orch(label, target=target)
    out = orch.handle(label.execution_id)
    assert out.kind == EXECUTED
    assert target.calls == [("dry_run", "rerun_failed_job"), ("execute", "rerun_failed_job")]
    assert out.gate is not None and out.gate.allowed and "known-transient rule" in out.gate.reason
    assert out.report.remediation is not None and out.report.remediation.gate_decision == "allowed"
    assert out.report.escalation_reason == ""
    assert kinds(orch) == ["proposal", "proposal", "gate_decision", "execution", "execution"]
    assert [e.payload.get("stage") for e in orch.audit.read("execution")] == ["dry_run", "execute"]
    assert len(orch.attempts.by_fingerprint(out.report.fingerprint)) == 1


def test_run_stats_come_from_the_budget_rollup(make_orch: MakeOrch, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    out = make_orch(label).handle(label.execution_id)
    run = out.report.run
    assert run.tool_calls == 3 == out.investigation.budget.tool_calls  # type: ignore[union-attr]
    assert (run.input_tokens, run.output_tokens) == (900 + 1600 + 2300, 3 * 140)
    assert out.model_calls == 3 and out.tokens == run.input_tokens + run.output_tokens


def test_tier3_stops_at_pr_proposed_and_never_touches_the_target(make_orch: MakeOrch, pick) -> None:
    label = pick("user_lockfile_mismatch")
    target = SyntheticTarget()
    orch = make_orch(label, target=target)
    out = orch.handle(label.execution_id)
    assert out.kind == PR_PROPOSED and target.calls == [] and target.executed == []
    rem = out.report.remediation
    assert rem is not None and rem.tier == 3 and rem.gate == "pull_request" and rem.gate_decision == "allowed"
    assert rem.dry_run["branch"].startswith(BRANCH_PREFIX)
    assert rem.dry_run["paths"] == list(label.fix_paths)
    last = orch.audit.read("execution")[-1]
    assert last.payload["stage"] == "pr_proposed" and last.payload["merged"] is False


def test_tier1_is_dry_run_only_and_awaits_approval(make_orch: MakeOrch, pick) -> None:
    label = pick("platform_expired_token", fleet=False)
    target = SyntheticTarget()
    out = make_orch(label, target=target).handle(label.execution_id)
    assert out.kind == AWAITING_APPROVAL and out.report.remediation.tier == 1  # type: ignore[union-attr]
    assert target.calls == [("dry_run", "refresh_connector_token")] and target.executed == []


def test_tier2_is_downgraded_because_holder_death_cannot_be_proven(make_orch: MakeOrch, pick) -> None:
    label = pick("platform_state_lock")
    target = SyntheticTarget()
    out = make_orch(label, target=target).handle(label.execution_id)
    assert out.kind == ESCALATED and out.gate.decision == "downgraded"  # type: ignore[union-attr]
    assert "holder death" in out.reason and target.calls == []


# --- the gate is independent of the model ---------------------------------------------------------------------


def test_gate_facts_come_from_code_not_from_what_the_model_called(make_orch: MakeOrch, pick) -> None:
    """The model calls only get_execution and proposes Tier 0; the orchestrator derives signature and rule itself."""
    label = pick("transient_throttling", fleet=False)
    target = SyntheticTarget()
    orch = make_orch(label, model_factory=lambda e, b: hand_model(label, RERUN), target=target)
    out = orch.handle(label.execution_id)
    assert out.kind == EXECUTED
    facts = orch.audit.read("proposal")[1].payload["facts"]
    assert facts["signature_match"] and facts["known_transient_rule"] and not facts["flake_precedent"]
    assert [r.tool for r in out.gate_tool_results] == ["classify_signature", "fleet_correlate", "flake_history"]
    assert all(r.source.startswith("gate:") for r in out.gate_tool_results)
    assert out.investigation is not None and {e.source for e in out.report.evidence} <= set(
        out.investigation.tool_call_ids
    )


def test_overconfident_tier0_without_a_signature_match_is_downgraded(
    make_orch: MakeOrch, pick, dataset_dir: Path
) -> None:
    label = pick("user_script_failure")
    target = SyntheticTarget()
    orch = make_orch(label, "overconfident_tier0", target=target)
    out = orch.handle(label.execution_id)
    assert out.kind == ESCALATED and out.gate.decision == "downgraded"  # type: ignore[union-attr]
    assert out.gate.reason == "no deterministic signature match" and target.calls == []  # type: ignore[union-attr]
    assert out.report.remediation.gate_decision == "downgraded"  # type: ignore[union-attr]
    assert out.report.confidence == 0.99 and out.gate.agent_confidence == 0.99  # type: ignore[union-attr]
    assert out.report.escalation_reason == out.reason
    entry = orch.audit.read("gate_decision")[0]
    assert entry.payload["decision"] == "downgraded" and entry.payload["agent_confidence"] == 0.99


def test_overconfident_tier0_with_a_match_but_no_rule_or_precedent_is_downgraded(make_orch: MakeOrch, pick) -> None:
    label = pick("transient_image_pull")
    out = make_orch(label, "overconfident_tier0").handle(label.execution_id)
    assert out.kind == ESCALATED
    assert out.gate.reason == "no flake precedent and no Tier 0 known-transient rule"  # type: ignore[union-attr]


def test_flaky_precedent_without_a_signature_is_not_acted_on(make_orch: MakeOrch, pick) -> None:
    """Precedent exists, the only signature is a bare exit code, and the agent overreaches with Tier 0: the gate holds.

    The honest scripted agent now escalates this case itself (label: escalate, see BLOCKERS-B.md), so the overreach
    variant is what exercises invariant 3 in the gate.
    """
    label = pick("transient_flaky_test")
    target = SyntheticTarget()
    orch = make_orch(label, "overconfident_tier0", target=target)
    out = orch.handle(label.execution_id)
    assert out.kind == ESCALATED and target.calls == []
    facts = orch.audit.read("proposal")[1].payload["facts"]
    assert facts["flake_precedent"] is True and facts["signature_match"] is False
    assert out.gate.reason == "no deterministic signature match"  # type: ignore[union-attr]


def test_fleet_wide_failure_is_escalated_by_the_abort_ceiling_even_if_proposed(make_orch: MakeOrch, pick) -> None:
    label = pick("platform_expired_token", fleet=True)
    target = SyntheticTarget()
    orch = make_orch(label, "ignore_fleet", target=target)
    out = orch.handle(label.execution_id)
    assert out.kind == ESCALATED and out.gate.decision == "downgraded" and target.calls == []  # type: ignore[union-attr]
    assert "fleet-wide" in out.reason
    assert out.report.blast_radius.executions_affected > FLEET_ABORT_CEILING
    assert out.report.blast_radius.shared_dimension


def test_blast_radius_is_populated_explicitly_for_every_investigated_case(make_orch: MakeOrch, pick) -> None:
    quiet = pick("transient_throttling", fleet=False)
    out = make_orch(quiet).handle(quiet.execution_id)
    assert (out.report.blast_radius.executions_affected, out.report.blast_radius.shared_dimension) == (0, "")
    assert [r.tool for r in out.gate_tool_results][:2] == ["classify_signature", "fleet_correlate"]
    fleet = pick("platform_expired_token", fleet=True)
    out = make_orch(fleet).handle(fleet.execution_id)
    assert out.report.blast_radius.executions_affected == fleet.blast_executions_affected >= 3


def test_the_models_fingerprint_is_not_trusted(make_orch: MakeOrch, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    out = make_orch(label).handle(label.execution_id)
    classify = next(r for r in out.gate_tool_results if r.tool == "classify_signature")
    assert out.report.fingerprint == classify.data["fingerprint"]


def test_actions_outside_the_catalog_are_rejected_before_the_gate(make_orch: MakeOrch, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    target = SyntheticTarget()
    bad = {**RERUN, "action": "drop_all_tables"}
    orch = make_orch(label, model_factory=lambda e, b: hand_model(label, bad), target=target)
    out = orch.handle(label.execution_id)
    assert out.kind == ESCALATED and "catalog" in out.reason and target.calls == []
    assert orch.audit.read("rejected")[-1].payload["stage"] == "validation"
    assert orch.audit.read("gate_decision") == []


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "C:\\x.txt", ""])
def test_tier3_paths_must_stay_inside_the_repository(make_orch: MakeOrch, pick, path: str) -> None:
    label = pick("user_lockfile_mismatch")
    rem = {"tier": 3, "action": "regenerate_lockfile", "rationale": "r", "reversible": True, "paths": [path]}
    out = make_orch(label, model_factory=lambda e, b: hand_model(label, rem)).handle(label.execution_id)
    assert out.kind == ESCALATED and "relative paths" in out.reason


def test_tier3_with_no_paths_is_refused_by_the_gate_as_an_empty_set(make_orch: MakeOrch, pick) -> None:
    label = pick("user_lockfile_mismatch")
    rem = {"tier": 3, "action": "regenerate_lockfile", "rationale": "r", "reversible": True, "paths": []}
    out = make_orch(label, model_factory=lambda e, b: hand_model(label, rem)).handle(label.execution_id)
    assert out.kind == ESCALATED and out.gate.decision == "refused" and "empty proposed set" in out.reason  # type: ignore[union-attr]


def test_a_wrong_gate_for_the_tier_is_downgraded(make_orch: MakeOrch, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    wrong = {**RERUN, "gate": "pull_request"}
    out = make_orch(label, model_factory=lambda e, b: hand_model(label, wrong)).handle(label.execution_id)
    assert out.kind == ESCALATED and out.gate.decision == "downgraded"  # type: ignore[union-attr]


# --- kill switch, rate limit, attempt history -----------------------------------------------------------------


def _set_kill(path: Path, global_on: bool = True, **tiers: bool) -> None:
    state = {"0": True, "1": True, "2": True, "3": True} | {k.lstrip("t"): v for k, v in tiers.items()}
    path.write_text(json.dumps({"global": global_on, "tiers": state}), encoding="utf-8")


def test_the_kill_switch_is_read_on_every_decision(make_orch: MakeOrch, pick, kill_file: Path) -> None:
    label = pick("transient_throttling", fleet=False)
    target = SyntheticTarget()
    orch = make_orch(label, target=target)
    _set_kill(kill_file, global_on=False)
    out = orch.handle(label.execution_id)
    assert out.kind == ESCALATED and out.gate.decision == "refused" and "global kill switch" in out.reason  # type: ignore[union-attr]
    assert target.calls == []
    _set_kill(kill_file, t0=False)
    assert "tier 0 kill switch" in orch.handle(label.execution_id).reason
    _set_kill(kill_file)  # re-enabled between decisions, no restart
    assert orch.handle(label.execution_id).kind == EXECUTED


def test_a_per_tier_kill_switch_only_blocks_that_tier(make_orch: MakeOrch, pick, kill_file: Path) -> None:
    _set_kill(kill_file, t3=False)
    t0 = pick("transient_throttling", fleet=False)
    assert make_orch(t0).handle(t0.execution_id).kind == EXECUTED
    t3 = pick("user_lockfile_mismatch")
    out = make_orch(t3).handle(t3.execution_id)
    assert out.kind == ESCALATED and "tier 3 kill switch" in out.reason


def test_a_missing_kill_switch_file_fails_closed(make_orch: MakeOrch, pick, tmp_path: Path) -> None:
    label = pick("transient_throttling", fleet=False)
    out = make_orch(label, kill_switch_path=tmp_path / "absent.json").handle(label.execution_id)
    assert out.kind == ESCALATED and out.gate.decision == "refused"  # type: ignore[union-attr]


def test_the_third_attempt_on_a_fingerprint_within_an_hour_hard_escalates(make_orch: MakeOrch, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    now = [1_700_000_000.0]
    target = SyntheticTarget()
    orch = make_orch(label, target=target, clock=lambda: now[0])
    outs = []
    for _ in range(3):
        outs.append(orch.handle(label.execution_id))
        now[0] += 60
    assert [o.kind for o in outs] == [EXECUTED, EXECUTED, ESCALATED]
    assert outs[2].gate.decision == "refused" and "rate limit" in outs[2].reason  # type: ignore[union-attr]
    assert [c for c in target.calls if c[0] == "execute"] == [("execute", "rerun_failed_job")] * 2
    now[0] += 3600  # the window slides; no sleeping
    assert orch.handle(label.execution_id).kind == EXECUTED


def test_a_second_failure_after_a_tier0_retry_hard_escalates(make_orch: MakeOrch, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    orch = make_orch(label)
    first = orch.handle(label.execution_id)
    fp = first.report.fingerprint
    orch.attempts.add(
        RemediationAttempt("a-failed", fp, label.execution_id, 0, "rerun_failed_job", "executed", {"verified": False})
    )
    second = orch.handle(label.execution_id)
    assert second.kind == ESCALATED and second.gate.decision == "refused" and "second failure" in second.reason  # type: ignore[union-attr]


# --- budget truncation and abstention reach the audit ---------------------------------------------------------


def test_a_budget_truncated_investigation_escalates_with_partial_evidence_and_is_audited(
    make_orch: MakeOrch, pick
) -> None:
    label = pick("transient_throttling", fleet=False)
    target = SyntheticTarget()
    orch = make_orch(label, "budget_calls", target=target)
    out = orch.handle(label.execution_id)
    assert out.kind == ESCALATED and out.report.budget_truncated and out.report.abstained
    assert out.report.run.tool_calls == 8 and len(out.report.evidence) == 8 and target.calls == []
    assert kinds(orch) == ["proposal", "budget_truncated", "rejected"]
    assert orch.audit.read("rejected")[0].payload["stage"] == "agent"


def test_uncited_proposals_never_reach_the_gate(make_orch: MakeOrch, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    target = SyntheticTarget()
    orch = make_orch(label, "all_bogus_evidence", target=target)
    out = orch.handle(label.execution_id)
    assert out.kind == ESCALATED and out.gate is None and target.calls == []
    proposal = orch.audit.read("proposal")[0].payload
    assert proposal["evidence_dropped"] == 1 and proposal["abstained"] is True


# --- extension points -----------------------------------------------------------------------------------------


def test_tier3_review_hook_can_reject_and_approve(make_orch: MakeOrch, pick) -> None:
    label = pick("user_lockfile_mismatch")
    rejecting = make_orch(label, tier3_review=lambda inv, rem: Review("reject", "touches an unrelated file"))
    out = rejecting.handle(label.execution_id)
    assert out.kind == ESCALATED and "reviewer verdict reject" in out.reason
    assert out.report.review == Review("reject", "touches an unrelated file")
    approving = make_orch(label, tier3_review=lambda inv, rem: Review("approve", "ok"))
    assert approving.handle(label.execution_id).kind == PR_PROPOSED


def test_after_execution_hook_sees_executed_and_proposed_outcomes_only(make_orch: MakeOrch, pick) -> None:
    seen: list[Outcome] = []
    t0 = pick("transient_throttling", fleet=False)
    make_orch(t0, after_execution=seen.append).handle(t0.execution_id)
    t3 = pick("user_lockfile_mismatch")
    make_orch(t3, after_execution=seen.append).handle(t3.execution_id)
    esc = pick("user_script_failure")
    make_orch(esc, after_execution=seen.append).handle(esc.execution_id)
    assert [o.kind for o in seen] == [EXECUTED, PR_PROPOSED]


def test_clock_is_injected(make_orch: MakeOrch, pick) -> None:
    label = pick("transient_throttling", fleet=False)
    orch = make_orch(label, clock=StepClock(10.0))
    orch.handle(label.execution_id)
    ts = [e.ts for e in orch.audit.read()]
    assert ts == sorted(ts) and ts[0] > 10.0


# --- agents stay read-only ------------------------------------------------------------------------------------


def _imports(path: Path) -> list[str]:
    out: list[str] = []
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            out += [n.module or ""] + [f"{n.module or ''}.{a.name}" for a in n.names]
    return out


def test_only_the_orchestrator_wires_the_target() -> None:
    wiring = []
    for p in SRC.rglob("*.py"):
        rel = p.relative_to(SRC).as_posix()
        if rel.startswith("adapters/"):
            continue
        if any(n.endswith(("SyntheticTarget", "GitHubActionsTarget")) for n in _imports(p)):
            wiring.append(rel)
    assert wiring == ["orchestrator.py"]


def test_agents_and_tools_do_not_import_the_orchestrator_or_adapters() -> None:
    offenders = []
    for sub in ("agents", "tools"):
        for p in (SRC / sub).rglob("*.py"):
            for name in _imports(p):
                if name.startswith(("driftgate.orchestrator", "driftgate.adapters", "driftgate.eval")):
                    offenders.append((p.name, name))
    assert offenders == []


def test_the_model_is_only_offered_read_only_tools_and_the_report_tool() -> None:
    from driftgate.agents.investigator import agent_tool_definitions

    names = {t["name"] for t in agent_tool_definitions()}
    assert names == {
        "get_execution",
        "classify_signature",
        "fleet_correlate",
        "flake_history",
        "get_step_logs",
        "read_repo_file",
        "get_remediation_attempt",
        "submit_report",
    }
    assert not any(any(w in n for w in ("execute", "rerun", "merge", "delete", "unlock")) for n in names)


def test_tier3_pull_request_title_and_body_are_redacted(make_orch: MakeOrch, pick) -> None:
    label = pick("user_lockfile_mismatch")
    target = SyntheticTarget()
    orch = make_orch(label, target=target, tier3_review=lambda inv, rem: Review("approve", "ok"))
    secret = "ghp_" + "a1B2c3D4e5" * 4
    original = orch._model_factory

    def factory(eid: str, budget: object) -> FakeModel:  # type: ignore[no-untyped-def]
        model = original(eid, budget)
        real = model.complete

        def complete(request):  # type: ignore[no-untyped-def]
            resp = real(request)
            for call in resp.tool_calls:
                if call.name == SUBMIT_REPORT and "remediation" in call.input:
                    call.input["remediation"]["rationale"] = f"use token {secret} to fix"
            return resp

        model.complete = complete  # type: ignore[method-assign]
        return model

    orch._model_factory = factory
    out = orch.handle(label.execution_id)
    assert out.kind == PR_PROPOSED
    pr = out.pull_request
    assert pr is not None
    assert secret not in pr.body and secret not in pr.title and "[REDACTED]" in pr.body
