"""N3 tool: window boundary, decoys, ordering and cap, no ground-truth leak, redaction, off by default."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from driftgate.adapters.synthetic import SyntheticSource, parse_time
from driftgate.agents.fingerprint import prompt_fingerprint
from driftgate.agents.investigator import (
    SUBMIT_REPORT,
    SYSTEM_PROMPT,
    Investigator,
    agent_tool_definitions,
    system_prompt,
)
from driftgate.domain import Execution, ExecutionSummary, FileContent, LogChunk, Node, SourceError
from driftgate.llm.budget import InvestigationBudget
from driftgate.llm.fake import FakeModel
from driftgate.llm.types import ModelResponse, ToolCall, Usage
from driftgate.tools import OPTIONAL_TOOLS, TOOLS, ToolContext, dispatch
from driftgate.tools.changes_in_window import (
    CHANGE_FIELDS,
    DEFAULT_WINDOW_MINUTES,
    MAX_CHANGES,
    changes_in_window,
    first_failure_time,
)

T0 = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)  # a fixed "now": the tool reads times from data, never a clock


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeSource:
    """In-memory source: a run of executions in one pipeline and a change timeline. Counts nothing, sleeps never."""

    def __init__(self, runs: list[tuple[str, str, datetime]], changes: list[dict[str, Any]]) -> None:
        self.runs = runs  # (execution_id, status, started_at)
        self.changes = changes
        self.change_windows: list[Any] = []

    def get_execution(self, execution_id: str) -> Execution:
        for eid, status, at in self.runs:
            if eid == execution_id:
                leaf = Node("leaf-" + eid, "step", "failed", error_summary="Error acquiring the state lock")
                return Execution(eid, "pipe-a", status, iso(at), root=Node("root", "p", status, children=[leaf]))
        raise SourceError(f"unknown execution: {execution_id}")

    def get_failed_leaf_nodes(self, execution_id: str) -> list[Node]:
        ex = self.get_execution(execution_id)
        return [] if ex.status != "failed" or ex.root is None else list(ex.root.children)

    def get_step_logs(self, execution_id: str, node_id: str, budget: int) -> LogChunk:
        return LogChunk(execution_id, node_id, "Error: Error acquiring the state lock", False)

    def list_executions(self, window: Any, filter: Any) -> list[ExecutionSummary]:
        rows = [ExecutionSummary(e, "pipe-a", s, iso(a)) for e, s, a in sorted(self.runs, key=lambda r: r[2])]
        return [r for r in rows if not filter or all(getattr(r, k, None) == v for k, v in filter.items())]

    def read_file(self, repo: str, path: str, ref: str, max_bytes: int) -> FileContent:
        raise SourceError("no files")

    def list_changes(self, window: Any = None) -> list[dict[str, Any]]:
        self.change_windows.append(window)
        if not window:
            return list(self.changes)
        lo, hi = parse_time(window[0]), parse_time(window[1])
        return [c for c in self.changes if lo <= parse_time(c["at"]) <= hi]


def change(n: int, at: datetime, kind: str = "commit", message: str = "msg", files: list[str] | None = None) -> dict:
    return {
        "change_id": f"chg-{n:04d}",
        "at": iso(at),
        "kind": kind,
        "ref": "pipe-a",
        "actor": "dev-ana",
        "message": message,
        "files": files or [],
        "sha": f"{n:040x}",
    }


def ctx_for(source: FakeSource) -> ToolContext:
    return ToolContext(source)  # type: ignore[arg-type]


# --- window boundary ---------------------------------------------------------------------------------------------


def test_returns_only_changes_strictly_before_the_failure() -> None:
    src = FakeSource(
        [("e1", "failed", T0)],
        [
            change(1, T0 - timedelta(minutes=30)),
            change(2, T0 - timedelta(seconds=1)),
            change(3, T0),  # at the failure: excluded
            change(4, T0 + timedelta(seconds=1)),
            change(5, T0 + timedelta(hours=2)),
        ],
    )
    out = changes_in_window(ctx_for(src), "e1")
    assert [c["change_id"] for c in out["changes"]] == ["chg-0002", "chg-0001"]
    assert all(parse_time(c["at"]) < T0 for c in out["changes"])


def test_window_start_is_inclusive_and_older_changes_are_excluded() -> None:
    w = DEFAULT_WINDOW_MINUTES
    src = FakeSource(
        [("e1", "failed", T0)],
        [change(1, T0 - timedelta(minutes=w)), change(2, T0 - timedelta(minutes=w, seconds=1))],
    )
    out = changes_in_window(ctx_for(src), "e1")
    assert [c["change_id"] for c in out["changes"]] == ["chg-0001"]
    assert out["window_minutes"] == w


def test_window_length_is_configurable_and_validated() -> None:
    src = FakeSource(
        [("e1", "failed", T0)], [change(1, T0 - timedelta(minutes=10)), change(2, T0 - timedelta(hours=3))]
    )
    assert [c["change_id"] for c in changes_in_window(ctx_for(src), "e1", 5)["changes"]] == []
    assert len(changes_in_window(ctx_for(src), "e1", 600)["changes"]) == 2
    for bad in (0, -1, 10**6):
        with pytest.raises(ValueError):
            changes_in_window(ctx_for(src), "e1", bad)
    assert not dispatch(
        ctx_for(src),
        "changes_in_window",
        {"execution_id": "e1", "window_minutes": 0},
        "t",
        frozenset({"changes_in_window"}),
    ).ok


def test_anchored_on_first_failure_of_the_episode_not_the_investigated_execution() -> None:
    runs = [
        ("e0", "success", T0 - timedelta(hours=5)),
        ("e1", "failed", T0),
        ("e2", "failed", T0 + timedelta(minutes=40)),
        ("e3", "failed", T0 + timedelta(minutes=80)),
    ]
    src = FakeSource(runs, [change(1, T0 - timedelta(minutes=20)), change(2, T0 + timedelta(minutes=10))])
    ctx = ctx_for(src)
    assert first_failure_time(ctx, "e3") == ("e1", T0)
    out = changes_in_window(ctx, "e3")
    assert out["first_failure_execution"] == "e1" and out["first_failure_at"] == iso(T0)
    assert [c["change_id"] for c in out["changes"]] == [
        "chg-0001"
    ]  # chg-0002 predates e3 but follows the first failure


def test_a_success_ends_the_episode() -> None:
    runs = [
        ("e1", "failed", T0 - timedelta(hours=2)),
        ("e2", "success", T0 - timedelta(hours=1)),
        ("e3", "failed", T0),
    ]
    assert first_failure_time(ctx_for(FakeSource(runs, [])), "e3") == ("e3", T0)


def test_non_failed_execution_returns_nothing() -> None:
    src = FakeSource([("e1", "success", T0)], [change(1, T0 - timedelta(minutes=5))])
    out = changes_in_window(ctx_for(src), "e1")
    assert out["changes"] == [] and "did not fail" in out["note"]


def test_source_without_a_change_timeline_is_a_clean_tool_error() -> None:
    class NoChanges(FakeSource):
        list_changes = None  # type: ignore[assignment]

    res = dispatch(
        ctx_for(NoChanges([("e1", "failed", T0)], [])),
        "changes_in_window",
        {"execution_id": "e1"},
        "t",
        frozenset({"changes_in_window"}),
    )
    assert not res.ok and "change timeline" in res.error


# --- decoys, ordering, cap ---------------------------------------------------------------------------------------


def test_everything_in_the_window_is_returned_related_or_not_nearest_first() -> None:
    src = FakeSource(
        [("e1", "failed", T0)],
        [
            change(1, T0 - timedelta(minutes=50), "template_release"),
            change(2, T0 - timedelta(minutes=10), "commit"),
            change(3, T0 - timedelta(minutes=30), "runner_pool_change"),  # no causal link to anything: still listed
        ],
    )
    out = changes_in_window(ctx_for(src), "e1")
    assert [c["change_id"] for c in out["changes"]] == ["chg-0002", "chg-0003", "chg-0001"]
    assert [c["minutes_before_failure"] for c in out["changes"]] == [10, 30, 50]
    assert out["total_in_window"] == 3 and out["truncated"] is False


def test_cap_keeps_the_nearest_and_says_it_truncated() -> None:
    n = MAX_CHANGES + 7
    src = FakeSource([("e1", "failed", T0)], [change(i, T0 - timedelta(minutes=i)) for i in range(1, n + 1)])
    out = changes_in_window(ctx_for(src), "e1", 1440)
    assert len(out["changes"]) == MAX_CHANGES and out["total_in_window"] == n and out["truncated"] is True
    assert out["changes"][0]["change_id"] == "chg-0001"  # nearest
    assert out["changes"][-1]["change_id"] == f"chg-{MAX_CHANGES:04d}"


def test_equal_timestamps_order_deterministically() -> None:
    at = T0 - timedelta(minutes=5)
    src = FakeSource([("e1", "failed", T0)], [change(2, at), change(1, at), change(3, at)])
    ids = [c["change_id"] for c in changes_in_window(ctx_for(src), "e1")["changes"]]
    assert ids == ["chg-0003", "chg-0002", "chg-0001"]


# --- redaction and field whitelist -------------------------------------------------------------------------------


def test_messages_and_files_are_redacted() -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"
    src = FakeSource(
        [("e1", "failed", T0)],
        [change(1, T0 - timedelta(minutes=5), message=f"rotate key {secret}", files=[f"deploy-{secret}.yml"])],
    )
    text = json.dumps(changes_in_window(ctx_for(src), "e1"))
    assert secret not in text


def test_only_whitelisted_fields_pass_through_even_if_the_source_adds_more() -> None:
    row = change(1, T0 - timedelta(minutes=5))
    row["is_causal"] = True
    row["decoy"] = False
    row["causal_change_ids"] = ["chg-0001"]
    src = FakeSource([("e1", "failed", T0)], [row])
    item = changes_in_window(ctx_for(src), "e1")["changes"][0]
    assert set(item) == set(CHANGE_FIELDS) | {"minutes_before_failure"}
    assert "causal" not in json.dumps(item).lower() and "decoy" not in json.dumps(item).lower()


# --- the real dataset: nothing causal is agent-visible -----------------------------------------------------------


def test_generated_changes_json_carries_no_causal_or_decoy_marker(dataset_dir: Path) -> None:
    rows = json.loads((dataset_dir / "changes.json").read_text(encoding="utf-8"))
    assert rows and {k for r in rows for k in r} == set(CHANGE_FIELDS)
    assert "causal" not in json.dumps(rows).lower() and "decoy" not in json.dumps(rows).lower()


def test_on_real_data_output_is_before_failure_and_never_contains_ground_truth(dataset_dir: Path, truth) -> None:  # type: ignore[no-untyped-def]
    source = SyntheticSource(dataset_dir)
    ctx = ToolContext(source)
    checked = 0
    causal_listed = decoy_listed = 0
    for label in truth.scenarios()[:40]:
        res = dispatch(
            ctx, "changes_in_window", {"execution_id": label.execution_id}, "t1", frozenset({"changes_in_window"})
        )
        assert res.ok
        data = res.to_dict()
        assert set(data["data"]) <= {
            "execution_id",
            "window_minutes",
            "first_failure_execution",
            "first_failure_at",
            "window_start",
            "total_in_window",
            "truncated",
            "changes",
            "note",
        }
        if "note" in res.data:  # not a failed execution (e.g. a governance rejection): nothing to anchor on
            assert res.data["changes"] == []
            continue
        first = parse_time(res.data["first_failure_at"])
        for c in res.data["changes"]:
            assert parse_time(c["at"]) < first
            assert set(c) == set(CHANGE_FIELDS) | {"minutes_before_failure"}
        ids = {c["change_id"] for c in res.data["changes"]}
        causal_listed += bool(ids & set(label.causal_change_ids))
        decoy_listed += bool(ids & set(label.decoy_change_ids))
        checked += 1
    assert checked
    assert causal_listed > 0 and decoy_listed > 0  # the window returns both, undifferentiated


# --- optional and off by default ---------------------------------------------------------------------------------

# Hashes pinned from the MVP 1 definitions (branch mvp2/n1-holdout) before N3 existed. They must never change while
# the tool is off, or every recorded replay fixture is orphaned.
MVP1_PROMPT_FINGERPRINT = "88a8278480e61498"


def test_default_prompt_fingerprint_is_the_mvp1_value() -> None:
    assert prompt_fingerprint() == MVP1_PROMPT_FINGERPRINT


def test_off_by_default_definitions_and_prompt_are_byte_identical_to_mvp1() -> None:
    names = [d["name"] for d in agent_tool_definitions()]
    assert names == [t.name for t in TOOLS] + [SUBMIT_REPORT]
    assert "changes_in_window" not in json.dumps(agent_tool_definitions())
    assert "changes_in_window" not in SYSTEM_PROMPT
    assert system_prompt() == SYSTEM_PROMPT and system_prompt(frozenset()) == SYSTEM_PROMPT
    assert agent_tool_definitions(frozenset()) == agent_tool_definitions()
    assert [t.name for t in OPTIONAL_TOOLS] == ["changes_in_window"] and all(
        t.name != "changes_in_window" for t in TOOLS
    )


def test_investigator_requests_are_unchanged_when_the_tool_is_off() -> None:
    model = FakeModel([ModelResponse(text="x"), ModelResponse(text="y")])
    Investigator(model, ctx_for(FakeSource([("e1", "failed", T0)], [])), InvestigationBudget()).investigate("e1")
    assert {r.system for r in model.requests} == {SYSTEM_PROMPT}
    assert all(r.tools == agent_tool_definitions() for r in model.requests)


def test_enabling_adds_the_tool_before_submit_report_and_the_prompt_addendum() -> None:
    on = frozenset({"changes_in_window"})
    names = [d["name"] for d in agent_tool_definitions(on)]
    assert names == [t.name for t in TOOLS] + ["changes_in_window", SUBMIT_REPORT]
    assert system_prompt(on).startswith(SYSTEM_PROMPT) and system_prompt(on) != SYSTEM_PROMPT


def test_a_disabled_tool_cannot_be_dispatched_even_if_the_model_asks() -> None:
    src = FakeSource([("e1", "failed", T0)], [change(1, T0 - timedelta(minutes=5))])
    off = dispatch(ctx_for(src), "changes_in_window", {"execution_id": "e1"}, "t1")
    assert not off.ok and "unknown tool" in off.error
    on = dispatch(ctx_for(src), "changes_in_window", {"execution_id": "e1"}, "t1", frozenset({"changes_in_window"}))
    assert on.ok and on.source == "t1"


def test_the_call_counts_against_the_existing_budget_and_its_id_is_citable() -> None:
    src = FakeSource([("e1", "failed", T0)], [change(1, T0 - timedelta(minutes=5))])
    report = {
        "classification": "user",
        "layer": "L4",
        "confidence": 0.5,
        "hypothesis": "h",
        "evidence": [{"source": "toolu_c", "finding": "a commit shortly before", "supports": "h"}],
        "suspected_change": {"kind": "commit", "ref": "chg-0001", "at": iso(T0 - timedelta(minutes=5)), "basis": "b"},
    }
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=[ToolCall("toolu_c", "changes_in_window", {"execution_id": "e1"})], usage=Usage(10, 5)
            ),
            ModelResponse(tool_calls=[ToolCall("toolu_f", SUBMIT_REPORT, report)], usage=Usage(10, 5)),
        ]
    )
    budget = InvestigationBudget()
    inv = Investigator(model, ctx_for(src), budget, optional_tools=frozenset({"changes_in_window"})).investigate("e1")
    assert budget.tool_calls == 1 and budget.limits.max_tool_calls == 8  # no budget was raised
    assert inv.tool_call_ids == ["toolu_c"] and inv.evidence_dropped == 0
    assert inv.report.suspected_change is not None and inv.report.suspected_change.ref == "chg-0001"
    assert model.requests[0].system == system_prompt(frozenset({"changes_in_window"}))
