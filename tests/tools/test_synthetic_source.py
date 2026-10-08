"""SyntheticSource reads only agent-visible files, never ground truth or simulator state."""

from __future__ import annotations

import builtins
import os
import shutil
from pathlib import Path

import pytest

from driftgate.adapters.synthetic import AGENT_VISIBLE_ENTRIES, EXECUTION_STATUSES, NODE_STATUSES, SyntheticSource
from driftgate.baseline import run_baseline
from driftgate.domain import SourceError
from driftgate.tools import ToolContext

HIDDEN = ("ground_truth", "world")


def _touches_hidden(path: object) -> bool:
    parts = Path(os.fspath(path)).parts if isinstance(path, (str, os.PathLike)) else ()
    return any(h in parts for h in HIDDEN)


def test_source_works_on_a_dataset_with_hidden_dirs_deleted(dataset_dir: Path, tmp_path: Path) -> None:
    visible = tmp_path / "visible"
    shutil.copytree(dataset_dir, visible, ignore=shutil.ignore_patterns(*HIDDEN, "manifest.json"))
    assert not (visible / "ground_truth").exists() and not (visible / "world").exists()
    src = SyntheticSource(visible)
    ctx = ToolContext(src)
    failed = src.list_executions(None, {"status": "failed"})
    assert len(failed) > 100
    for s in failed:  # the whole baseline runs: nothing it needs lives in ground_truth/ or world/
        run_baseline(ctx, s.execution_id)


def test_no_file_access_touches_ground_truth_or_world(dataset_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def spy(fn):  # type: ignore[no-untyped-def]
        def wrapper(self_or_path, *a, **k):  # type: ignore[no-untyped-def]
            seen.append(os.fspath(self_or_path))
            return fn(self_or_path, *a, **k)

        return wrapper

    monkeypatch.setattr(Path, "read_text", spy(Path.read_text))
    monkeypatch.setattr(Path, "read_bytes", spy(Path.read_bytes))
    monkeypatch.setattr(Path, "open", spy(Path.open))
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", spy(real_open))
    monkeypatch.setattr(os, "scandir", spy(os.scandir))
    monkeypatch.setattr(os, "listdir", spy(os.listdir))

    src = SyntheticSource(dataset_dir)
    ctx = ToolContext(src)
    for s in src.list_executions(None, {"status": "failed"})[:60]:
        run_baseline(ctx, s.execution_id)
        if (dataset_dir / "repos" / s.pipeline / "main" / "README.md").exists():
            src.read_file(s.pipeline, "README.md", "main", 1000)
    src.list_changes()

    assert seen, "spy did not record any access"
    assert [p for p in seen if _touches_hidden(p)] == []


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.read_file("..", "labels.json", "main", 100),
        lambda s: s.read_file("api-build", "../../ground_truth/labels.json", "main", 100),
        lambda s: s.read_file("api-build", "../../../world/followups.json", "main", 100),
        lambda s: s.read_file("api-build", "package.json", "../../world", 100),
        lambda s: s.get_step_logs("ex-000008", "../../ground_truth/labels", 100),
        lambda s: s.get_step_logs("ex-missing", "n", 100),
    ],
)
def test_traversal_into_hidden_data_is_refused(dataset_dir: Path, call) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SourceError):
        call(SyntheticSource(dataset_dir))


def test_resolve_allowlist_is_exactly_the_agent_visible_entries(dataset_dir: Path) -> None:
    assert AGENT_VISIBLE_ENTRIES == {"executions.json", "changes.json", "logs", "repos"}
    src = SyntheticSource(dataset_dir)
    for hidden in ("ground_truth/labels.json", "world/followups.json", "manifest.json"):
        with pytest.raises(SourceError):
            src._resolve(hidden)


def test_status_strings_are_the_documented_lowercase_set(dataset_dir: Path) -> None:
    src = SyntheticSource(dataset_dir)
    all_runs = src.list_executions(None, None)
    assert {s.status for s in all_runs} == set(EXECUTION_STATUSES)
    nodes: set[str] = set()

    def walk(n) -> None:  # type: ignore[no-untyped-def]
        nodes.add(n.status)
        for c in n.children:
            walk(c)

    for s in all_runs[::25]:
        walk(src.get_execution(s.execution_id).root)
    assert nodes <= set(NODE_STATUSES)


def test_list_executions_filters_and_window(dataset_dir: Path) -> None:
    src = SyntheticSource(dataset_dir)
    everything = src.list_executions(None, None)
    assert [e.started_at for e in everything] == sorted(e.started_at for e in everything)
    pool = everything[0].refs["runner_pool"]
    failed_pool = src.list_executions(None, {"status": "failed", "runner_pool": pool})
    assert failed_pool and all(e.status == "failed" and e.refs["runner_pool"] == pool for e in failed_pool)
    lo, hi = everything[100].started_at, everything[200].started_at
    windowed = src.list_executions((lo, hi), None)
    assert windowed[0].started_at >= lo and windowed[-1].started_at <= hi
    assert 100 <= len(windowed) <= 101 + 5
    assert src.list_executions(None, {"pipeline": "no-such"}) == []


def test_failed_leaf_nodes_are_leaves_and_governance_has_none(dataset_dir: Path) -> None:
    src = SyntheticSource(dataset_dir)
    for s in src.list_executions(None, {"status": "failed"})[:20]:
        leaves = src.get_failed_leaf_nodes(s.execution_id)
        assert leaves and all(not n.children and n.status == "failed" for n in leaves)
    rejected = src.list_executions(None, {"status": "approval_rejected"})
    assert rejected and src.get_failed_leaf_nodes(rejected[0].execution_id) == []


def test_log_budget_truncates(dataset_dir: Path) -> None:
    src = SyntheticSource(dataset_dir)
    s = src.list_executions(None, {"status": "failed"})[0]
    leaf = src.get_failed_leaf_nodes(s.execution_id)[0]
    chunk = src.get_step_logs(s.execution_id, leaf.node_id, 40)
    assert len(chunk.text) == 40 and chunk.truncated
    assert not src.get_step_logs(s.execution_id, leaf.node_id, 10**6).truncated
