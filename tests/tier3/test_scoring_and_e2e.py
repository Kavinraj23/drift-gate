"""Tier 3 PR-quality scoring (used later by M9a) and the e2e table's Tier 3 column."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from driftgate.adapters.synthetic import SyntheticSource
from driftgate.eval import e2e
from driftgate.eval.e2e import MISMATCH, OK, format_table, run_all, run_scenario
from driftgate.eval.ground_truth import FailureLabel
from driftgate.eval.scripted_agent import scripted_model
from driftgate.eval.tier3_scripts import BAD_KINDS, bad_diff, score_tier3, tier3_quality
from driftgate.llm.config import GatewayConfig
from driftgate.llm.replay import FixtureStore, request_key
from driftgate.orchestrator import build_synthetic_orchestrator

RunT3 = Callable[..., Any]


def test_correct_diff_scores_as_matching_and_approved(
    run_t3: RunT3, t3_labels: list[FailureLabel], dataset_dir: Path
) -> None:
    src = SyntheticSource(dataset_dir)
    for label in t3_labels:
        run = run_t3(label)
        s = score_tier3(label, src, run.outcome, run.audit)
        assert s.proposed_diff and s.matches_fix and not s.bad_diff and s.pr_opened
        assert s.correct_diff_approved and not s.bad_diff_missed and s.reviewer_verdicts == ("approve",)


def test_scoring_compares_resulting_file_hashes_not_diff_text(t3_labels: list[FailureLabel], dataset_dir: Path) -> None:
    """A differently written diff (no context lines) that produces the same files still matches the fix."""
    import difflib

    from driftgate.eval.tier3_scripts import _fixed, _Repo

    class Outcome:
        kind = "pr_proposed"

        def __init__(self, text: str) -> None:
            self.investigation = type("I", (), {"proposal": type("P", (), {"diff": text})()})()

    src = SyntheticSource(dataset_dir)
    label = next(lb for lb in t3_labels if lb.correct_action == "fix_requirement_pin")
    repo = _Repo(src, label)
    path, fixed = next(iter(_fixed(label, repo).items()))
    lean = "".join(
        difflib.unified_diff(
            (repo.read(path) or "").splitlines(keepends=True),
            fixed.splitlines(keepends=True),
            f"a/{path}",
            f"b/{path}",
            n=0,
        )
    )
    assert lean != label.fix_diff
    assert score_tier3(label, src, Outcome(lean), []).matches_fix
    assert score_tier3(label, src, Outcome(label.fix_diff or ""), []).matches_fix
    assert not score_tier3(label, src, Outcome("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"), []).matches_fix


def test_every_seeded_bad_diff_is_scored_as_caught(
    run_t3: RunT3, t3_labels: list[FailureLabel], dataset_dir: Path
) -> None:
    src = SyntheticSource(dataset_dir)
    scores = []
    for kind in BAD_KINDS:
        for label in t3_labels:
            if bad_diff(kind, label, src) is None:
                continue
            run = run_t3(label, f"diff:{kind}")
            s = score_tier3(label, src, run.outcome, run.audit)
            assert s.bad_diff and not s.matches_fix and s.bad_diff_caught and not s.pr_opened, (kind, label.fault_id)
            assert s.caught_by in ("validator", "reviewer")
            scores.append(s)
    q = tier3_quality(scores)
    assert q["bad_diffs"] == q["bad_diffs_caught"] == len(scores) and q["bad_diff_catch_rate"] == 1.0
    assert q["bad_diffs_missed"] == 0 and {s.caught_by for s in scores} == {"validator", "reviewer"}


def test_a_missed_bad_diff_shows_up_in_the_quality_numbers(
    run_t3: RunT3, t3_labels: list[FailureLabel], dataset_dir: Path
) -> None:
    src = SyntheticSource(dataset_dir)
    run = run_t3(t3_labels[0], "diff:over_broad", "lenient")
    s = score_tier3(t3_labels[0], src, run.outcome, run.audit)
    assert s.bad_diff_missed and not s.bad_diff_caught
    q = tier3_quality([s])
    assert q["bad_diffs_missed"] == 1 and q["bad_diff_catch_rate"] == 0.0


def test_a_correct_diff_rejected_by_the_reviewer_is_counted_as_a_false_rejection(
    run_t3: RunT3, t3_labels: list[FailureLabel], dataset_dir: Path
) -> None:
    from driftgate.llm.fake import FakeModel
    from driftgate.llm.types import ModelResponse, ToolCall, Usage

    def paranoid() -> FakeModel:
        verdict = {"verdict": "reject", "comments": "I do not like it."}
        return FakeModel([ModelResponse(tool_calls=[ToolCall("v", "submit_review", verdict)], usage=Usage(100, 10))])

    run = run_t3(t3_labels[0], reviewer_model=paranoid)
    s = score_tier3(t3_labels[0], SyntheticSource(dataset_dir), run.outcome, run.audit)
    q = tier3_quality([s])
    assert s.matches_fix and not s.pr_opened and s.caught_by == "reviewer"
    assert q["correct_diffs"] == 1 and q["correct_diffs_rejected"] == 1 and q["correct_diffs_approved"] == 0


def test_escalations_without_a_diff_are_not_scored_as_bad(dataset_dir: Path, pick: Callable[..., FailureLabel]) -> None:
    label = pick("transient_throttling", fleet=False)
    res = run_scenario(dataset_dir, label, False)
    s = score_tier3(label, SyntheticSource(dataset_dir), res.outcome, res.audit)
    assert not s.proposed_diff and not s.bad_diff and tier3_quality([s])["bad_diff_catch_rate"] == 1.0


# --- e2e table -----------------------------------------------------------------------------------------------
def test_e2e_table_reports_the_pr_outcome_of_every_tier3_scenario(dataset_dir: Path) -> None:
    results = run_all(dataset_dir)
    rows = [r.row for r in results]
    table = format_table(rows, "fake")
    assert "tier 3 pr" in table
    pr_rows = [r for r in rows if r.expected.startswith("T3")]
    assert len(pr_rows) == 9 and all(r.status == OK and r.pr == "opened (approve)" for r in pr_rows)
    assert all(r.pr == "-" for r in rows if not r.expected.startswith("T3") and r.actual.split()[0] != "escalated")


def test_e2e_seeded_bad_diff_shows_as_a_stopped_pr_and_a_mismatch(
    dataset_dir: Path, pick: Callable[..., FailureLabel]
) -> None:
    label = pick("user_lockfile_mismatch")
    res = run_scenario(dataset_dir, label, False, variant="diff:wrong_file")
    assert res.row.status == MISMATCH and res.row.pr.startswith("stopped (reject: reviewer verdict reject")
    assert res.row.model_calls == 4  # the investigator's turns; reviewer usage is rolled into tokens and tool calls


def test_e2e_revise_then_approve_recovers(dataset_dir: Path, pick: Callable[..., FailureLabel]) -> None:
    label = pick("user_lockfile_mismatch")
    res = run_scenario(dataset_dir, label, False, variant="revise:wrong_file", reviewer="revising")
    assert res.row.status == OK and res.row.pr == "opened (approve after revision)"
    assert res.outcome is not None and res.outcome.revisions == 1


class _Recorder:
    """Plays the scripted investigator while saving each exchange as a replay fixture (what `record` does)."""

    def __init__(self, inner: Any, store: Any, model: str) -> None:
        self._inner, self._store, self._model = inner, store, model

    def complete(self, request: Any) -> Any:
        response = self._inner.complete(request)
        self._store.save(request_key(request, self._model), request, self._model, response)
        return response


def test_e2e_replay_without_reviewer_fixtures_uses_and_labels_the_scripted_reviewer(
    dataset_dir: Path, pick: Callable[..., FailureLabel], tmp_path: Path
) -> None:
    label = pick("user_lockfile_mismatch")
    store = FixtureStore(tmp_path / "fixtures")
    source = SyntheticSource(dataset_dir)
    model = GatewayConfig().investigator_model
    kill = tmp_path / "kill.json"
    kill.write_text(json.dumps(e2e._KILL_SWITCH_ON), encoding="utf-8")
    build_synthetic_orchestrator(
        dataset_dir,
        lambda eid, b: _Recorder(scripted_model(label, source), store, model),
        audit_path=tmp_path / "a.jsonl",
        clock=e2e.StepClock(),
        kill_switch_path=kill,
    ).handle(label.execution_id)
    res = run_scenario(dataset_dir, label, False, mode="replay", fixtures_dir=tmp_path / "fixtures")
    assert res.row.model == "replay" and res.row.status == OK
    assert res.row.pr == "opened (approve, scripted reviewer)"
    assert run_scenario(dataset_dir, label, False, mode="fake").row.pr == "opened (approve)"
