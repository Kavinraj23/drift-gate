"""HoldoutSource: an ExecutionSource over ONE stored real CI failure, built from log text and source metadata only.

What it is
- A read-only `ExecutionSource` (the protocol `SyntheticSource` and the GitHub adapter also implement) that exposes a
  single failed execution with one failed leaf step and that step's stored log, so the investigator's existing tools
  work unchanged on a real log.
- It is constructed from a `HoldoutInput`, which carries exactly the fields below. The loader in `holdout_run.py`
  builds that value from `holdout/` and passes nothing else, so this module never sees a label (invariants 8, 14).
  This module performs no file I/O at all and does not import the holdout store.

What it does NOT have, and says so rather than inventing it (invariant 9, and "never synthesize")
- No history: `list_executions` returns only this execution, so `fleet_correlate` and `flake_history` find nothing.
  Their results then read as "no peers / no earlier failures": empty, not evidence of absence.
- No repository content: `read_file` raises `SourceError`, so the agent must escalate when a fix needs a file.
- No change timeline: `list_changes` returns an empty list.
- No join keys (connector, template, runner pool, infra definition): `refs` carries only the source's own identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from driftgate.domain import (
    Execution,
    ExecutionSummary,
    FileContent,
    LogChunk,
    Node,
    SourceError,
)

ROOT_NODE_ID = "job"
STEP_NODE_ID = "step-1"
#: Where the execution's start time came from. The run's real start is not stored with a holdout case.
TIME_BASIS_LOG = "log_timestamp"
TIME_BASIS_FETCH = "fetch_date"


@dataclass(frozen=True)
class HoldoutInput:
    """Everything the source may know about one case. There is deliberately no field for a layer, classification,
    action, rationale or split, and no place to put one."""

    case_id: str
    log_text: str
    repo: str
    run_id: int
    job: str
    step_name: str
    commit_sha: str
    started_at: str  # ISO 8601 UTC, from the first timestamp in the log, else the fetch date at 00:00:00
    finished_at: str  # same, from the last timestamp
    time_basis: str  # TIME_BASIS_LOG | TIME_BASIS_FETCH


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class HoldoutSource:
    """One failed execution. Implements `ExecutionSource` (plus the optional `list_changes`)."""

    def __init__(self, case: HoldoutInput) -> None:
        self._case = case
        self._refs = {
            "commit": case.commit_sha,
            "run_id": str(case.run_id),
            "job": case.job,
            "time_basis": case.time_basis,
        }

    @property
    def execution_id(self) -> str:
        return self._case.case_id

    def _check(self, execution_id: str) -> None:
        if execution_id != self._case.case_id:
            raise SourceError(f"unknown execution: {execution_id}")

    def _tree(self) -> Node:
        leaf = Node(node_id=STEP_NODE_ID, name=self._case.step_name, status="failed", parent_id=ROOT_NODE_ID)
        return Node(node_id=ROOT_NODE_ID, name=self._case.job, status="failed", children=[leaf])

    def get_execution(self, execution_id: str) -> Execution:
        self._check(execution_id)
        c = self._case
        return Execution(
            execution_id=c.case_id,
            pipeline=c.repo,
            status="failed",
            started_at=c.started_at,
            finished_at=c.finished_at,
            root=self._tree(),
            refs=dict(self._refs),
        )

    def get_failed_leaf_nodes(self, execution_id: str) -> list[Node]:
        self._check(execution_id)
        return [self._tree().children[0]]

    def get_step_logs(self, execution_id: str, node_id: str, budget: int) -> LogChunk:
        self._check(execution_id)
        if node_id != STEP_NODE_ID:
            raise SourceError(f"no log for {execution_id}/{node_id}")
        text = self._case.log_text
        return LogChunk(execution_id, node_id, text[:budget], len(text) > budget)

    def list_executions(self, window: Any, filter: Any) -> list[ExecutionSummary]:
        """The only execution this source knows, if it matches the window and the filter; otherwise nothing.

        Same filter semantics as `SyntheticSource`: `status`, `pipeline`, or any `refs` key, compared for equality."""
        c = self._case
        if window:
            lo, hi = _parse(str(window[0])), _parse(str(window[1]))
            if not lo <= _parse(c.started_at) <= hi:
                return []
        flt: dict[str, Any] = dict(filter or {})
        own: dict[str, Any] = {"status": "failed", "pipeline": c.repo}
        for key, want in flt.items():
            have = own[key] if key in own else self._refs.get(key)
            if have != want:
                return []
        return [ExecutionSummary(c.case_id, c.repo, "failed", c.started_at, dict(self._refs))]

    def read_file(self, repo: str, path: str, ref: str, max_bytes: int) -> FileContent:
        raise SourceError("repository content is not available for holdout cases; escalate if a file is needed")

    def list_changes(self, window: Any = None) -> list[dict[str, Any]]:
        """No change timeline exists for a stored real log."""
        return []
