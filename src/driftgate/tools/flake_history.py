"""flake_history: earlier fail-then-pass episodes for the same fingerprint (pipeline + step + signature)."""

from __future__ import annotations

from typing import Any

from .base import ToolContext, ToolSpec
from .fleet_correlate import parse_time


def flake_history(ctx: ToolContext, execution_id: str) -> dict[str, Any]:
    """An episode is an earlier failure with this fingerprint whose retry (refs.retry_of) passed before now.

    Only retries that started before the execution under investigation count, so precedent never uses the future.
    """
    ex = ctx.source.get_execution(execution_id)
    analysis = ctx.analyzer.analyze(execution_id)
    if analysis is None:
        return {"execution_id": execution_id, "fingerprint": None, "precedent": False, "note": "execution did not fail"}
    now = parse_time(ex.started_at)
    history = ctx.source.list_executions(None, {"pipeline": ex.pipeline})
    retries: dict[str, list[Any]] = {}
    for h in history:
        origin = h.refs.get("retry_of")
        if origin:
            retries.setdefault(origin, []).append(h)
    episodes: list[dict[str, Any]] = []
    prior = 0
    for h in history:
        if h.status != "failed" or h.execution_id == execution_id or parse_time(h.started_at) >= now:
            continue
        a = ctx.analyzer.analyze(h.execution_id)
        if a is None or a.fingerprint != analysis.fingerprint:
            continue
        prior += 1
        for r in retries.get(h.execution_id, []):
            if r.status == "success" and parse_time(r.started_at) < now:
                episodes.append(
                    {
                        "failed_execution": h.execution_id,
                        "failed_at": h.started_at,
                        "passing_retry": r.execution_id,
                        "passed_at": r.started_at,
                    }
                )
                break
    return {
        "execution_id": execution_id,
        "fingerprint": analysis.fingerprint,
        "signature": analysis.primary.id,
        "prior_failures": prior,
        "fail_then_pass_episodes": episodes,
        "precedent": bool(episodes),
    }


def _handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return flake_history(ctx, args["execution_id"])


SPEC = ToolSpec(
    name="flake_history",
    description=(
        "Fail-then-pass history for this execution's fingerprint (same signature, pipeline and step): earlier "
        "failures that were followed by a passing retry. precedent=true is one of the two conditions for "
        "Tier 0 (the other is a Tier 0 known-transient rule from classify_signature). Cheap."
    ),
    input_schema={
        "type": "object",
        "properties": {"execution_id": {"type": "string"}},
        "required": ["execution_id"],
        "additionalProperties": False,
    },
    handler=_handle,
)
