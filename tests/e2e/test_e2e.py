"""End to end: every synthetic scenario through the orchestrator on the scripted model or replay fixtures."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from driftgate.adapters.synthetic import SyntheticSource
from driftgate.eval import e2e
from driftgate.eval.e2e import CONFLICT, MISMATCH, NO_FIXTURE, OK, ScenarioResult, run_all, run_scenario
from driftgate.eval.ground_truth import GroundTruth
from driftgate.eval.scripted_agent import scripted_model
from driftgate.llm.config import GatewayConfig
from driftgate.llm.replay import FixtureStore, request_key
from driftgate.orchestrator import CLOSED, ESCALATED, EXECUTED, PR_PROPOSED

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def results(dataset_dir: Path) -> list[ScenarioResult]:
    return run_all(dataset_dir)


def test_every_scenario_ran_and_none_mismatch(results: list[ScenarioResult], truth: GroundTruth) -> None:
    assert [r.row.scenario_id for r in results] == [
        s.scenario_id for h in (False, True) for s in truth.scenarios(held_out=h)
    ]
    assert len(results) == 33 and sum(r.row.held_out for r in results) == 9
    assert [r.row.scenario_id for r in results if r.row.status == MISMATCH] == []
    assert {r.row.status for r in results} <= {OK, CONFLICT}


def test_each_row_matches_its_expected_outcome(results: list[ScenarioResult], truth: GroundTruth) -> None:
    for r in results:
        label = truth.for_execution(r.row.execution_id)
        assert label is not None and r.outcome is not None
        out = r.outcome
        if label.disposition == "close":
            assert out.kind == CLOSED
        elif label.disposition == "escalate":
            assert out.kind in (ESCALATED, "awaiting_approval"), r.row
            assert out.kind != ESCALATED or out.report.escalation_reason  # escalations carry reasons
        elif r.row.status == OK:
            want = {0: EXECUTED, 3: PR_PROPOSED}[label.correct_tier]
            assert out.kind == want and out.report.remediation.action == label.correct_action  # type: ignore[union-attr]


def test_known_label_conflicts_are_exactly_the_flake_precedent_cases(
    results: list[ScenarioResult], truth: GroundTruth
) -> None:
    conflicts = [r for r in results if r.row.status == CONFLICT]
    assert conflicts
    for r in conflicts:
        label = truth.for_execution(r.row.execution_id)
        assert label.tier0_path == "flake_precedent" and label.disposition == "remediate"  # type: ignore[union-attr]
        assert r.outcome.kind == ESCALATED  # type: ignore[union-attr]
        assert r.outcome.gate.reason == "no deterministic signature match"  # type: ignore[union-attr]
    labels = {r.row.execution_id: truth.for_execution(r.row.execution_id) for r in results}
    expected = [e for e, lb in labels.items() if lb.tier0_path == "flake_precedent" and lb.disposition == "remediate"]  # type: ignore[union-attr]
    assert sorted(r.row.execution_id for r in conflicts) == sorted(expected)


def test_governance_scenarios_make_zero_model_calls(results: list[ScenarioResult]) -> None:
    closed = [r for r in results if r.outcome.kind == CLOSED]  # type: ignore[union-attr]
    assert len(closed) == 2
    for r in closed:
        assert (r.row.model_calls, r.row.tool_calls, r.row.tokens, r.row.model) == (0, 0, 0, "none")


def test_investigated_cases_stay_inside_the_budget(results: list[ScenarioResult]) -> None:
    for r in results:
        assert r.row.tool_calls <= 8 and r.row.tokens <= 40_000
        if r.outcome.kind != CLOSED:  # type: ignore[union-attr]
            assert r.row.model_calls >= 2


def test_every_report_validates_and_cites_only_its_own_tool_calls(
    results: list[ScenarioResult], report_validator: Draft202012Validator
) -> None:
    for r in results:
        out = r.outcome
        assert out is not None
        report_validator.validate(json.loads(json.dumps(out.report.to_dict())))
        if out.investigation is not None:
            own = set(out.investigation.tool_call_ids)
            assert {e.source for e in out.report.evidence} <= own
            assert out.report.evidence
        assert out.report.fingerprint


def test_agent_branches_are_prefixed_and_nothing_is_merged(results: list[ScenarioResult]) -> None:
    prs = [r for r in results if r.outcome.kind == PR_PROPOSED]  # type: ignore[union-attr]
    assert len(prs) >= 8
    for r in prs:
        assert r.outcome.report.remediation.dry_run["branch"].startswith("driftgate/")  # type: ignore[union-attr]
        assert all(e.payload.get("merged") is False for e in r.audit if e.payload.get("stage") == "pr_proposed")


def test_audit_is_complete_for_every_case(results: list[ScenarioResult]) -> None:
    for r in results:
        out = r.outcome
        assert out is not None
        entries = [(e.kind, e.payload.get("stage")) for e in r.audit]
        assert [e.seq for e in r.audit] == list(range(len(r.audit)))
        assert all(e.execution_id == r.row.execution_id for e in r.audit)
        if out.kind == CLOSED:
            assert entries == [("rejected", "prefilter")]
            continue
        assert entries[0] == ("proposal", "agent")  # every investigated case records what the agent proposed
        if out.gate is None:
            assert entries[-1][0] == "rejected" and entries[-1][1] in ("agent", "facts", "validation")
            continue
        assert ("proposal", "gate_facts") in entries
        gates = [e for e in r.audit if e.kind == "gate_decision"]
        assert len(gates) == 1 and gates[0].payload["decision"] == out.gate.decision
        assert gates[0].payload["agent_confidence"] == out.report.confidence
        tail = [e for e in entries if e[0] in ("execution", "rejected", "failed")]
        if out.kind == EXECUTED:
            assert tail == [("execution", "dry_run"), ("execution", "execute")]
        elif out.kind == PR_PROPOSED:
            assert tail == [("execution", "pr_proposed")]
        elif out.kind == "awaiting_approval":
            assert tail == [("execution", "dry_run")]
        else:
            assert tail[-1] == ("rejected", "gate")


def test_run_is_deterministic(dataset_dir: Path, results: list[ScenarioResult]) -> None:
    again = run_all(dataset_dir)
    assert [r.row for r in again] == [r.row for r in results]


def test_table_has_a_row_per_scenario_and_labels_the_fake_model(results: list[ScenarioResult]) -> None:
    text = e2e.format_table([r.row for r in results], "fake")
    for r in results:
        assert r.row.scenario_id in text
    assert "scripted fake model is a test double" in text
    assert "33 scenarios: 30 ok, 3 known label conflict, 0 mismatch" in text
    for col in ("expected", "actual", "model calls", "tool calls", "tokens"):
        assert col in text


def test_main_exits_zero_and_prints_the_table(dataset_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert e2e.main(["--data", str(dataset_dir)]) == 0
    assert "sc-33" in capsys.readouterr().out


# --- replay keys and modes ------------------------------------------------------------------------------------


class _Recorder:
    """Plays the scripted model while saving each exchange as a replay fixture, as `record` would for a real one."""

    def __init__(self, inner, store: FixtureStore, model: str) -> None:  # type: ignore[no-untyped-def]
        self._inner, self._store, self._model = inner, store, model

    def complete(self, request):  # type: ignore[no-untyped-def]
        response = self._inner.complete(request)
        self._store.save(request_key(request, self._model), request, self._model, response)
        return response


def _record(dataset_dir: Path, label, store: FixtureStore) -> None:  # type: ignore[no-untyped-def]
    from driftgate.orchestrator import build_synthetic_orchestrator

    source = SyntheticSource(dataset_dir)
    model = GatewayConfig().investigator_model
    orch_dir = dataset_dir.parent / f"rec-{label.scenario_id}"
    kill = orch_dir / "kill.json"
    kill.parent.mkdir(parents=True, exist_ok=True)
    kill.write_text(json.dumps(e2e._KILL_SWITCH_ON), encoding="utf-8")
    build_synthetic_orchestrator(
        dataset_dir,
        lambda eid, b: _Recorder(scripted_model(label, source), store, model),
        audit_path=orch_dir / "a.jsonl",
        clock=e2e.StepClock(),
        kill_switch_path=kill,
    ).handle(label.execution_id)


def test_replay_over_recorded_fixtures_reproduces_the_fake_run(
    dataset_dir: Path, truth: GroundTruth, tmp_path: Path, pick
) -> None:
    store = FixtureStore(tmp_path / "fixtures")
    label = pick("user_lockfile_mismatch")
    _record(dataset_dir, label, store)
    live_like = run_scenario(dataset_dir, label, False, mode="replay", fixtures_dir=tmp_path / "fixtures")
    fake = run_scenario(dataset_dir, label, False, mode="fake")
    assert live_like.row.model == "replay" and live_like.row.status == OK
    assert (live_like.row.actual, live_like.row.model_calls, live_like.row.tool_calls) == (
        fake.row.actual,
        fake.row.model_calls,
        fake.row.tool_calls,
    )
    assert live_like.outcome.report.remediation.action == label.correct_action  # type: ignore[union-attr]


def test_replay_mode_reports_missing_fixtures_and_never_falls_back(
    dataset_dir: Path, truth: GroundTruth, tmp_path: Path, pick
) -> None:
    label = pick("user_lockfile_mismatch")
    r = run_scenario(dataset_dir, label, False, mode="replay", fixtures_dir=tmp_path / "none")
    assert r.row.status == NO_FIXTURE and r.outcome is None and r.row.model == "replay"


def test_auto_mode_falls_back_to_the_fake_model_and_says_so(dataset_dir: Path, tmp_path: Path, pick) -> None:
    label = pick("user_lockfile_mismatch")
    r = run_scenario(dataset_dir, label, False, mode="auto", fixtures_dir=tmp_path / "none")
    assert r.row.status == OK and r.row.model == "fake (no fixture)"


def test_governance_needs_no_fixture_even_in_replay_mode(dataset_dir: Path, tmp_path: Path, pick) -> None:
    r = run_scenario(
        dataset_dir, pick("governance_approval_rejected"), False, mode="replay", fixtures_dir=tmp_path / "none"
    )
    assert r.row.status == OK and r.row.model_calls == 0


def test_replay_mode_cannot_reach_the_network_or_a_key() -> None:
    src = (ROOT / "src" / "driftgate" / "eval" / "e2e.py").read_text(encoding="utf-8")
    assert 'mode="replay"' in src and '"live"' not in src and '"record"' not in src
    assert "environ" not in src and "dotenv" not in src


# --- the task runner ------------------------------------------------------------------------------------------


def test_tasks_e2e_is_wired() -> None:
    spec = importlib.util.spec_from_file_location("tasks", ROOT / "tasks.py")
    tasks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tasks)
    assert "e2e" in tasks.HANDLERS and "e2e" not in tasks.NOT_IMPLEMENTED
    assert {"M0", "M1", "M2", "M3", "M4", "M5"} <= tasks.IMPLEMENTED_MILESTONES
    assert "record" in tasks.NOT_IMPLEMENTED and "record" in tasks.HUMAN_ONLY
