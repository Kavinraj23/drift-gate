"""get_execution: the execution tree, failed leaf nodes and join-key refs."""

from __future__ import annotations

from typing import Any

from driftgate.domain import ExecutionSource, Node

from .base import ToolContext, ToolSpec


def _tree(n: Node) -> dict[str, Any]:
    out: dict[str, Any] = {"node_id": n.node_id, "name": n.name, "status": n.status}
    if n.children:
        out["children"] = [_tree(c) for c in n.children]
    return out


def get_execution(source: ExecutionSource, execution_id: str) -> dict[str, Any]:
    ex = source.get_execution(execution_id)
    leaves = source.get_failed_leaf_nodes(execution_id)
    return {
        "execution_id": ex.execution_id,
        "pipeline": ex.pipeline,
        "status": ex.status,
        "started_at": ex.started_at,
        "finished_at": ex.finished_at,
        "refs": dict(ex.refs),
        "tree": _tree(ex.root) if ex.root else None,
        "failed_leaves": [{"node_id": n.node_id, "name": n.name, "error_summary": n.error_summary} for n in leaves],
    }


def _handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return get_execution(ctx.source, args["execution_id"])


SPEC = ToolSpec(
    name="get_execution",
    description=(
        "Get an execution: status, pipeline, start/finish time, the stage/step tree, the failed leaf "
        "steps with their error summary, and refs (connector, template@version, runner_pool, infra_def, commit). "
        "Cheap. Call this first."
    ),
    input_schema={
        "type": "object",
        "properties": {"execution_id": {"type": "string"}},
        "required": ["execution_id"],
        "additionalProperties": False,
    },
    handler=_handle,
)
