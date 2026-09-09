"""Serializes a generated Population to disk, and loads it back.

Layout under out_dir:
  dataset.json        pipelines + executions + events (no fault ids, no labels)
  logs/<exec_id>/<node_id>.txt   raw log text for each failed node - real evidence,
                                 not ground truth: a classifier is expected to read
                                 this and derive its own label from it
  ground_truth.json   the answer key - loaded only by eval code, never by
                       SyntheticSource
  fixture_repo/        real backdated git repo (written directly by generator.changes)
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from drift_gate.domain import (
    Execution,
    ExecutionStatus,
    Node,
    NodeStatus,
    TemplateRef,
)
from drift_gate.errors import BY_FAULT_ID as ERROR_BY_FAULT_ID
from drift_gate.generator.pipelines import PipelineDef
from drift_gate.generator.population import Population


def _dt(x: datetime | None) -> str | None:
    return None if x is None else x.isoformat()


def _parse_dt(x: str | None) -> datetime | None:
    return None if x is None else datetime.fromisoformat(x)


def _node_to_json(n: Node) -> dict:
    return {
        "id": n.id, "name": n.name, "step_type": n.step_type,
        "status": n.status.value, "parent_id": n.parent_id,
        "started_at": _dt(n.started_at), "ended_at": _dt(n.ended_at),
    }


def _node_from_json(d: dict) -> Node:
    return Node(
        id=d["id"], name=d["name"], step_type=d["step_type"],
        status=NodeStatus(d["status"]), parent_id=d["parent_id"],
        started_at=_parse_dt(d["started_at"]), ended_at=_parse_dt(d["ended_at"]),
    )


def _execution_to_json(e: Execution) -> dict:
    return {
        "id": e.id, "pipeline_id": e.pipeline_id, "status": e.status.value,
        "started_at": _dt(e.started_at), "ended_at": _dt(e.ended_at),
        "connector_ref": e.connector_ref,
        "template_ref": {"name": e.template_ref.name, "version": e.template_ref.version},
        "runner_pool": e.runner_pool, "infra_ref": e.infra_ref, "trigger": e.trigger,
        "nodes": [_node_to_json(n) for n in e.nodes],
    }


def _execution_from_json(d: dict) -> Execution:
    return Execution(
        id=d["id"], pipeline_id=d["pipeline_id"], status=ExecutionStatus(d["status"]),
        started_at=_parse_dt(d["started_at"]), ended_at=_parse_dt(d["ended_at"]),
        connector_ref=d["connector_ref"],
        template_ref=TemplateRef(d["template_ref"]["name"], d["template_ref"]["version"]),
        runner_pool=d["runner_pool"], infra_ref=d["infra_ref"], trigger=d["trigger"],
        nodes=tuple(_node_from_json(n) for n in d["nodes"]),
    )


def _pipeline_to_json(p: PipelineDef) -> dict:
    return {
        "id": p.id, "connector_ref": p.connector_ref,
        "template_ref": {"name": p.template_ref.name, "version": p.template_ref.version},
        "runner_pool": p.runner_pool, "infra_ref": p.infra_ref,
        "step_chain": list(p.step_chain), "flaky": p.flaky,
    }


def save_population(population: Population, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    dataset = {
        "pipelines": [_pipeline_to_json(p) for p in population.pipelines],
        "executions": [_execution_to_json(e) for e in population.executions],
        "events": [e.to_json() for e in population.events],
    }
    (out_dir / "dataset.json").write_text(json.dumps(dataset, indent=2), encoding="utf-8")

    ground_truth = [g.to_json() for g in population.ground_truth]
    (out_dir / "ground_truth.json").write_text(
        json.dumps(ground_truth, indent=2), encoding="utf-8"
    )

    logs_dir = out_dir / "logs"
    executions_by_id = {e.id: e for e in population.executions}
    for gt in population.ground_truth:
        execution = executions_by_id[gt.execution_id]
        failed = [n for n in execution.nodes if n.status == NodeStatus.FAILED]
        if not failed:
            continue
        node = failed[0]
        text = ERROR_BY_FAULT_ID[gt.injected_fault_id].text
        node_dir = logs_dir / gt.execution_id
        node_dir.mkdir(parents=True, exist_ok=True)
        (node_dir / f"{node.id}.txt").write_text(text, encoding="utf-8")


def load_dataset(out_dir: Path) -> tuple[list[Execution], list[dict]]:
    """Loads executions + raw events (as dicts) for SyntheticSource. Never touches
    ground_truth.json."""
    data = json.loads((out_dir / "dataset.json").read_text(encoding="utf-8"))
    executions = [_execution_from_json(e) for e in data["executions"]]
    return executions, data["events"]


def load_ground_truth(out_dir: Path) -> list[dict]:
    """Eval-only. Do not call from anything the agent/classifier can see."""
    return json.loads((out_dir / "ground_truth.json").read_text(encoding="utf-8"))
