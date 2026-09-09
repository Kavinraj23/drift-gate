"""Tool definitions the agent loop exposes to the model. The model only ever touches
CI data through these - same discipline as the rest of the system talking to providers
only via ExecutionSource/RemediationTarget (PRD.md SS9).
"""
from __future__ import annotations

from datetime import datetime

from drift_gate.protocols import ExecutionSource

TOOLS: list[dict] = [
    {
        "name": "get_execution",
        "description": "Fetch the full execution record: status, pipeline, connector, "
                        "template version, runner pool, infra ref, and step node tree.",
        "input_schema": {
            "type": "object",
            "properties": {"execution_id": {"type": "string"}},
            "required": ["execution_id"],
        },
    },
    {
        "name": "get_failed_leaf_nodes",
        "description": "Return the deepest failed step(s) in the execution's node "
                        "tree - the useful information is always here, not on a "
                        "parent stage that only reports pass/fail.",
        "input_schema": {
            "type": "object",
            "properties": {"execution_id": {"type": "string"}},
            "required": ["execution_id"],
        },
    },
    {
        "name": "get_step_logs",
        "description": "Fetch raw log text for one failed step node, truncated to a "
                        "character budget.",
        "input_schema": {
            "type": "object",
            "properties": {
                "execution_id": {"type": "string"},
                "node_id": {"type": "string"},
                "budget": {"type": "integer", "default": 4000},
            },
            "required": ["execution_id", "node_id"],
        },
    },
    {
        "name": "list_executions",
        "description": "List executions in a time window - e.g. to check whether "
                        "other pipelines sharing a connector/template/runner pool are "
                        "also failing (fleet correlation), or whether this pipeline "
                        "succeeded on a retry shortly after (flake check).",
        "input_schema": {
            "type": "object",
            "properties": {
                "window_start": {"type": "string", "description": "ISO 8601 timestamp"},
                "window_end": {"type": "string", "description": "ISO 8601 timestamp"},
                "pipeline_id": {"type": "string",
                                "description": "optional: restrict to one pipeline"},
            },
            "required": ["window_start", "window_end"],
        },
    },
    {
        "name": "submit_report",
        "description": "Finish the investigation by submitting the output-contract "
                        "report (PRD.md SS10). Call this exactly once, as your final "
                        "action, only once you have enough tool-backed evidence. Every "
                        "evidence item's 'source' must name a tool you actually called "
                        "this run - a claim without a backing tool result is rejected.",
        "input_schema": {
            "type": "object",
            "properties": {
                "classification": {
                    "type": "string",
                    "enum": ["platform", "user", "transient", "governance", "unknown"],
                },
                "layer": {"type": "string", "enum": ["L0", "L1", "L2", "L3", "L4", "L5", "L6"]},
                "confidence": {"type": "number"},
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string"},
                            "finding": {"type": "string"},
                            "supports": {"type": "string"},
                        },
                        "required": ["source", "finding", "supports"],
                    },
                },
                "suspected_change": {"type": ["string", "null"]},
                "remediation": {
                    "type": ["object", "null"],
                    "properties": {
                        "tier": {"type": "integer"},
                        "action": {"type": "string"},
                        "rationale": {"type": "string"},
                        "reversible": {"type": "boolean"},
                        "gate": {
                            "type": "string",
                            "enum": ["auto", "single_approval", "dual_approval", "pull_request"],
                        },
                        "pr_diff": {
                            "type": "string",
                            "description": "For gate=pull_request only: the actual "
                                            "fix content (file diff or new contents).",
                        },
                    },
                },
                "abstained": {"type": "boolean"},
                "escalation_reason": {"type": ["string", "null"]},
            },
            "required": ["classification", "layer", "confidence", "evidence", "abstained"],
        },
    },
]


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def dispatch(source: ExecutionSource, name: str, tool_input: dict) -> dict:
    """Runs one tool call against the ExecutionSource and returns a JSON-safe dict."""
    if name == "get_execution":
        e = source.get_execution(tool_input["execution_id"])
        return {
            "id": e.id, "pipeline_id": e.pipeline_id, "status": e.status.value,
            "started_at": e.started_at.isoformat(),
            "connector_ref": e.connector_ref,
            "template_ref": {"name": e.template_ref.name, "version": e.template_ref.version},
            "runner_pool": e.runner_pool, "infra_ref": e.infra_ref, "trigger": e.trigger,
            "nodes": [
                {"id": n.id, "name": n.name, "step_type": n.step_type,
                 "status": n.status.value, "parent_id": n.parent_id}
                for n in e.nodes
            ],
        }
    if name == "get_failed_leaf_nodes":
        nodes = source.get_failed_leaf_nodes(tool_input["execution_id"])
        return {"failed_leaf_nodes": [
            {"id": n.id, "name": n.name, "step_type": n.step_type} for n in nodes
        ]}
    if name == "get_step_logs":
        chunk = source.get_step_logs(
            tool_input["execution_id"], tool_input["node_id"],
            tool_input.get("budget", 4000),
        )
        return {"text": chunk.text, "truncated": chunk.truncated}
    if name == "list_executions":
        window = (_dt(tool_input["window_start"]), _dt(tool_input["window_end"]))
        filt = {"pipeline_id": tool_input["pipeline_id"]} if tool_input.get("pipeline_id") else None
        summaries = source.list_executions(window, filt)
        return {"executions": [
            {"id": s.id, "pipeline_id": s.pipeline_id, "status": s.status.value,
             "started_at": s.started_at.isoformat(), "connector_ref": s.connector_ref,
             "template_ref": {"name": s.template_ref.name, "version": s.template_ref.version},
             "runner_pool": s.runner_pool}
            for s in summaries
        ]}
    raise ValueError(f"unknown tool {name}")
