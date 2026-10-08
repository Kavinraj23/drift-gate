"""fleet_correlate: other executions failing with the same signature on a shared dimension around this one."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .base import ToolContext, ToolSpec

WINDOW_MINUTES = 30
FLEET_MIN_EXECUTIONS = 3  # including the execution under investigation
FLEET_MIN_PIPELINES = 2
DIMENSIONS = ("connector", "template", "runner_pool", "infra_def")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fleet_correlate(ctx: ToolContext, execution_id: str) -> dict[str, Any]:
    """Same-signature failures within +/- WINDOW_MINUTES sharing a connector, template@version, pool or infra def.

    The window is symmetric so that late members of a burst see the earlier ones and early members the later ones
    (an investigation may run any time after the failure). Dimension values of "none" are ignored.
    """
    ex = ctx.source.get_execution(execution_id)
    analysis = ctx.analyzer.analyze(execution_id)
    base: dict[str, Any] = {
        "execution_id": execution_id,
        "window_minutes": WINDOW_MINUTES,
        "fleet_wide": False,
        "executions_affected": 0,
        "shared_dimension": "",
        "dimensions": [],
    }
    if analysis is None:
        return {**base, "note": "execution did not fail"}
    t = parse_time(ex.started_at)
    window = (_iso(t - timedelta(minutes=WINDOW_MINUTES)), _iso(t + timedelta(minutes=WINDOW_MINUTES)))
    rows: list[dict[str, Any]] = []
    for order, dim in enumerate(DIMENSIONS):
        value = ex.refs.get(dim, "")
        if not value or value == "none":
            continue
        peers = ctx.source.list_executions(window, {dim: value})
        failed = [
            p
            for p in peers
            if p.status == "failed"
            and (a := ctx.analyzer.analyze(p.execution_id)) is not None
            and a.primary.id == analysis.primary.id
        ]
        ids = sorted({p.execution_id for p in failed} | {execution_id})
        rows.append(
            {
                "dimension": dim,
                "value": value,
                "executions": ids,
                "count": len(ids),
                "pipelines": sorted({p.pipeline for p in failed} | {ex.pipeline}),
                "window_executions": len(peers),
                "_order": order,
            }
        )
    rows.sort(key=lambda r: (-r["count"], -r["count"] / max(r["window_executions"], 1), r["_order"]))
    for r in rows:
        del r["_order"]
    best = rows[0] if rows else None
    fleet = best is not None and best["count"] >= FLEET_MIN_EXECUTIONS and len(best["pipelines"]) >= FLEET_MIN_PIPELINES
    return {
        **base,
        "signature": analysis.primary.id,
        "fleet_wide": fleet,
        "executions_affected": best["count"] if fleet and best else 0,
        "shared_dimension": f"{best['dimension']}:{best['value']}" if fleet and best else "",
        "dimensions": rows,
    }


def _handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return fleet_correlate(ctx, args["execution_id"])


SPEC = ToolSpec(
    name="fleet_correlate",
    description=(
        f"Find other executions failing with the same signature within {WINDOW_MINUTES} minutes of this one "
        "that share a connector, template version, runner pool or infra definition. fleet_wide=true means "
        f"{FLEET_MIN_EXECUTIONS}+ executions across {FLEET_MIN_PIPELINES}+ pipelines share a dimension, which "
        "points at the platform rather than at one pipeline's code. Gives the blast radius. Cheap."
    ),
    input_schema={
        "type": "object",
        "properties": {"execution_id": {"type": "string"}},
        "required": ["execution_id"],
        "additionalProperties": False,
    },
    handler=_handle,
)
