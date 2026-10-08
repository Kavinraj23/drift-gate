"""M9a: the offline eval report. Metric definitions on hand-built cases, then the real run end to end."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from driftgate.eval import report
from driftgate.eval.metrics import Rate
from driftgate.eval.report import CaseResult, ReportData, compute_metrics, render_markdown

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("tasks_m9a", ROOT / "tasks.py")
tasks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tasks)


def case(sid: str = "sc-x", **kw: object) -> CaseResult:
    base: dict[str, object] = dict(
        scenario_id=sid,
        held_out=False,
        conflict=False,
        disposition="escalate",
        correct_action=None,
        true_layer="L1",
        true_classification="user",
        acted=False,
        action="",
        layer="L1",
        classification="user",
        verified=None,
        input_tokens=0,
        output_tokens=0,
        model_calls=0,
        tool_calls=2,
        tools=("get_execution", "classify_signature"),
    )
    base.update(kw)
    return CaseResult(**base)  # type: ignore[arg-type]


def correct_t0(sid: str = "ok", **kw: object) -> CaseResult:
    return case(
        sid, disposition="remediate", correct_action="rerun_failed_job", acted=True, action="rerun_failed_job", **kw
    )


# -- metric definitions ------------------------------------------------------------------------------------------------
def test_false_remediation_counts_acting_on_escalate_and_wrong_action() -> None:
    cases = [
        correct_t0("good", verified=True),
        case("acted-on-escalate", acted=True, action="rerun_failed_job"),
        case("close-but-acted", disposition="close", acted=True, action="rerun_failed_job"),
        case(
            "wrong-action",
            disposition="remediate",
            correct_action="pin_provider_version",
            acted=True,
            action="rerun_failed_job",
        ),
        case("declined-correctly"),
    ]
    m = compute_metrics(cases, has_verification=True)
    assert m.false_remediation == Rate(3, 5)
    assert m.false_on_escalate_close == Rate(2, 3)  # escalate, close and declined-correctly are label-not-remediate
    assert m.false_wrong_action == Rate(1, 2)  # of the 2 label-remediate cases, one was acted on wrongly
    assert m.correct_action_share == Rate(1, 4)


def test_wrong_action_denominator_is_label_remediate_cases() -> None:
    cases = [correct_t0("good"), case("w", disposition="remediate", correct_action="x", acted=True, action="y")]
    m = compute_metrics(cases, has_verification=False)
    assert m.false_wrong_action == Rate(1, 2) and m.remediation_success is None


def test_a_failed_first_attempt_that_was_acted_is_a_false_remediation_when_wrong() -> None:
    wrong_first = case("w", disposition="remediate", correct_action="pin", acted=True, action="rerun_failed_job")
    assert wrong_first.false_remediation


def test_escalation_precision_and_recall() -> None:
    cases = [
        case("right-decline"),
        case("conflict-decline", disposition="remediate", correct_action="rerun_failed_job"),
        correct_t0("fixed"),
    ]
    m = compute_metrics(cases, has_verification=False)
    assert m.escalation_precision == Rate(1, 2)
    assert m.remediation_recall == Rate(1, 2)


def test_remediation_success_is_verified_over_attempted() -> None:
    cases = [correct_t0("a", verified=True), correct_t0("b", verified=False), case("none")]
    assert compute_metrics(cases, has_verification=True).remediation_success == Rate(1, 2)


def test_tool_efficiency_counts_resolved_cases_without_medium_tools() -> None:
    cases = [
        case("cheap"),
        case("logs", tools=("get_execution", "get_step_logs")),
        case("repo", tools=("read_repo_file",), acted=True, action="x"),  # wrong action: not resolved
    ]
    assert compute_metrics(cases, has_verification=True).tool_efficiency == Rate(1, 2)


def test_classification_and_layer_accuracy() -> None:
    cases = [case("a"), case("b", classification="platform"), case("c", layer="L2")]
    m = compute_metrics(cases, has_verification=False)
    assert m.classification_accuracy == Rate(2, 3) and m.layer_accuracy == Rate(2, 3)


def test_cost_math_comes_from_the_price_table() -> None:
    c = case("c", input_tokens=1_000_000, output_tokens=1_000_000, model_calls=2)
    assert report.case_cost(c) == pytest.approx(1.0 + 5.0)  # haiku: $1/Mtok in, $5/Mtok out
    sonnet = report.case_cost(c, model="claude-sonnet-5-5")
    assert sonnet == pytest.approx(3.0 + 15.0)
    m = compute_metrics([c, case("free")], has_verification=True)
    assert m.mean_cost_usd == pytest.approx(3.0) and m.mean_input_tokens == 500_000 and m.mean_model_calls == 1.0
    assert report.case_cost(case("zero")) == 0.0


def test_empty_set_does_not_divide_by_zero() -> None:
    m = compute_metrics([], has_verification=True)
    assert m.n == 0 and m.false_remediation.value is None and m.mean_cost_usd == 0.0
    assert report.fmt(m.false_remediation) == "n/a (n=0)"


def test_small_n_is_flagged_and_large_n_is_not() -> None:
    assert report.fmt(Rate(3, 9)).endswith("*") and not report.fmt(Rate(30, 90)).endswith("*")


# -- rendering on a hand-built report ----------------------------------------------------------------------------------
def tiny_report() -> ReportData:
    dev = [
        correct_t0("sc-1", verified=True),
        case("sc-2", disposition="remediate", correct_action="rerun_failed_job", conflict=True),
    ]
    held = [case("sc-3", held_out=True), case("sc-4", held_out=True, acted=True, action="rerun_failed_job")]
    cases = {"agent": {"dev": dev, "held-out": held}, "baseline": {"dev": dev, "held-out": held}}
    zero = {"numerator": 0, "denominator": 0, "value": None}
    recovery = {
        "cases": 4,
        "drills_run": 0,
        "success": zero,
        "recovered_by_verified_fix": zero,
        "correct_escalation_after_attempt": zero,
        "not_recovered": zero,
        "wrong_first_fix_stopped_by_gate": zero,
    }
    t3 = {"diffs_proposed": 0, "correct_diffs": 0, "correct_diffs_approved": 0, "correct_diffs_rejected": 0}
    return ReportData("fake", ["fake"], cases, ["sc-2"], recovery, t3, {})


def test_report_separates_held_out_and_counts_conflicts_both_ways() -> None:
    text = render_markdown(tiny_report())
    assert "baseline / dev (n=2)" in text and "agent / held-out (n=2)" in text
    assert "counted as misses" in text and "excluded" in text
    assert "baseline / dev (n=1)" in text  # the excluded table drops the conflict case from dev
    assert "sc-2" in text.split("Label-conflict footnote")[1]
    # held-out false remediation: sc-4 acted on an escalate label
    assert "1/2 = 50%*" in text


def test_report_states_the_honesty_caveat_and_the_agent_is_scripted() -> None:
    text = render_markdown(tiny_report())
    assert "SCRIPTED" in text and "correct by construction" in text
    assert "NOTHING about how a real model performs" in text
    assert "eval-live" in text and "n < 10" in text


# -- the real run ------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def data(dataset_dir: Path) -> ReportData:
    return report.build_report(dataset_dir, mode="auto", fixtures_dir=dataset_dir / "no-fixtures")


def test_both_systems_cover_the_same_scenarios_and_sets_are_separate(data: ReportData) -> None:
    for sys_ in report.SYSTEMS:
        assert [len(data.cases[sys_][s]) for s in report.SETS] == [24, 9]
        assert all(c.held_out for c in data.cases[sys_]["held-out"])
    ids = {s: [c.scenario_id for c in data.cases[s]["dev"]] for s in report.SYSTEMS}
    assert ids["agent"] == ids["baseline"]


def test_known_label_conflicts_are_reported_explicitly(data: ReportData) -> None:
    assert data.conflict_ids == ["sc-05", "sc-07", "sc-13"]
    text = render_markdown(data)
    assert "sc-05, sc-07, sc-13" in text
    agent = report.compute_metrics(data.cases["agent"]["dev"], has_verification=True)
    kept = report.compute_metrics([c for c in data.cases["agent"]["dev"] if not c.conflict], has_verification=True)
    assert agent.remediation_recall.denominator == kept.remediation_recall.denominator + 3
    assert agent.remediation_recall.numerator == kept.remediation_recall.numerator


def test_agent_never_acts_wrongly_offline_and_baseline_recall_is_lower(data: ReportData) -> None:
    for s in report.SETS:
        ag = report.compute_metrics(data.cases["agent"][s], has_verification=True)
        bl = report.compute_metrics(data.cases["baseline"][s], has_verification=False)
        assert ag.false_remediation.numerator == 0
        assert bl.remediation_recall.value is not None and ag.remediation_recall.value is not None
        assert bl.remediation_recall.value < ag.remediation_recall.value


def test_recovery_and_tier3_sections_are_populated(data: ReportData) -> None:
    assert data.recovery["drills_run"] == 5
    assert data.recovery["recovered_by_verified_fix"]["denominator"] >= 1
    assert data.tier3["diffs_proposed"] == 9 and data.tier3["correct_diffs"] == 9
    assert data.bad_diff_drills["bad_diffs"] > 0 and data.bad_diff_drills["bad_diffs_missed"] == 0


def test_main_writes_artifacts_to_the_given_dir_and_is_deterministic(
    data: ReportData, dataset_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "eval-out"
    code = report.main(["--data", str(dataset_dir), "--out", str(out), "--fixtures", str(dataset_dir / "no-fixtures")])
    printed = capsys.readouterr().out
    assert code == 0 and "Honesty" in printed and "wrote" in printed
    md = (out / "report.md").read_text(encoding="utf-8")
    assert md == render_markdown(data)  # same dataset, same output: no timestamps, no ordering noise
    js = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert js["label_conflicts"] == ["sc-05", "sc-07", "sc-13"] and "SCRIPTED" in js["honesty"]
    assert set(js["metrics"]) == {"conflicts_counted", "conflicts_excluded"}
    assert js["metrics"]["conflicts_counted"]["agent/held-out"]["n"] == 9


def test_readme_table_matches_the_current_report(data: ReportData) -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert report.readme_tables(data) in readme, "README eval table is stale; regenerate it from `report.readme_tables`"


# -- tasks.py and docs ---------------------------------------------------------------------------
def test_eval_target_is_registered_and_runs_the_report_module(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "eval" in tasks.HANDLERS and "eval" not in tasks.NOT_IMPLEMENTED and "M9a" in tasks.IMPLEMENTED_MILESTONES
    calls: list[list[str]] = []
    monkeypatch.setattr(tasks, "data", lambda: 0)
    monkeypatch.setattr(
        tasks.subprocess, "run", lambda cmd, **k: calls.append(cmd) or type("R", (), {"returncode": 0})()
    )
    assert tasks.eval_() == 0
    assert "driftgate.eval.report" in calls[-1] and "--out" in calls[-1]


def test_eval_live_stays_unimplemented_and_human_only() -> None:
    assert (
        "eval-live" in tasks.NOT_IMPLEMENTED and "eval-live" in tasks.HUMAN_ONLY and "eval-live" not in tasks.HANDLERS
    )


@pytest.mark.parametrize("doc", ["README.md", "docs/DEMO.md"])
def test_docs_reference_only_existing_targets(doc: str) -> None:
    text = (ROOT / doc).read_text(encoding="utf-8")
    targets = set(re.findall(r"python tasks\.py ([a-z0-9-]+)", text))
    known = set(tasks.HANDLERS) | set(tasks.NOT_IMPLEMENTED)
    assert targets and targets <= known, targets - known


def test_demo_marks_live_steps_human_only() -> None:
    text = (ROOT / "docs" / "DEMO.md").read_text(encoding="utf-8")
    live = text.split("## Part 2")[1]
    assert "HUMAN ONLY" in text and all(t in live for t in tasks.HUMAN_ONLY)
    assert not any(t in text.split("## Part 2")[0] for t in tasks.HUMAN_ONLY)


def test_report_module_cannot_reach_the_network_or_the_sdk() -> None:
    src = (ROOT / "src" / "driftgate" / "eval" / "report.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|from)\s+(anthropic|requests|urllib|socket|dotenv)", src, re.M)
    assert "load_config" not in src and ".env" not in src.replace("`.env`", "")
