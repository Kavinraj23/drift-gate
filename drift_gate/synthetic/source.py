"""ExecutionSource implementation served from a generated Population on disk.

Loads dataset.json + logs/ only - never ground_truth.json. Anything written against
ExecutionSource works unchanged against a real GitHubActionsSource later (PRD.md SS9).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from drift_gate.dataset_io import load_dataset
from drift_gate.domain import Execution, ExecutionSummary, LogChunk, Node, NodeStatus


class SyntheticSource:
    def __init__(self, data_dir: Path | str = "data/synthetic"):
        self.data_dir = Path(data_dir)
        executions, events = load_dataset(self.data_dir)
        self._executions: dict[str, Execution] = {e.id: e for e in executions}
        self.events = events

    def get_execution(self, execution_id: str) -> Execution:
        return self._executions[execution_id]

    def get_failed_leaf_nodes(self, execution_id: str) -> list[Node]:
        execution = self.get_execution(execution_id)
        parent_ids = {n.parent_id for n in execution.nodes if n.parent_id is not None}
        return [
            n for n in execution.nodes
            if n.status == NodeStatus.FAILED and n.id not in parent_ids
        ]

    def get_step_logs(self, execution_id: str, node_id: str, budget: int) -> LogChunk:
        path = self.data_dir / "logs" / execution_id / f"{node_id}.txt"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        truncated = len(text) > budget
        return LogChunk(
            text=text[:budget], truncated=truncated, char_budget=budget,
            source_node_id=node_id,
        )

    def list_executions(
        self, window: tuple[datetime, datetime], filter: dict | None = None
    ) -> list[ExecutionSummary]:
        start, end = window
        filter = filter or {}
        out = []
        for e in self._executions.values():
            if not (start <= e.started_at <= end):
                continue
            if any(getattr(e, k, None) != v for k, v in filter.items()):
                continue
            out.append(ExecutionSummary(
                id=e.id, pipeline_id=e.pipeline_id, status=e.status,
                started_at=e.started_at, connector_ref=e.connector_ref,
                template_ref=e.template_ref, runner_pool=e.runner_pool,
            ))
        out.sort(key=lambda s: s.started_at)
        return out
