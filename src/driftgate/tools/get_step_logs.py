"""get_step_logs: extracted, redacted, budgeted error blocks from the failed step only."""

from __future__ import annotations

from typing import Any

from driftgate.domain import SourceError

from .base import RAW_LOG_CAP, ToolContext, ToolSpec
from .logtext import clean_lines, collapse_repeats, error_blocks, render_budgeted

DEFAULT_BUDGET_CHARS = 4000
MAX_BUDGET_CHARS = 8000
FALLBACK_TAIL_LINES = 30


def get_step_logs(
    ctx: ToolContext, execution_id: str, node_id: str | None = None, max_chars: int | None = None
) -> dict[str, Any]:
    leaves = ctx.source.get_failed_leaf_nodes(execution_id)
    if not leaves:
        raise SourceError(f"{execution_id} has no failed step")
    if node_id is None:
        leaf = leaves[0]
    else:
        leaf = next((n for n in leaves if n.node_id == node_id), None)
        if leaf is None:
            raise SourceError(f"{node_id} is not a failed step of {execution_id}; only failed steps are readable")
    budget = max(200, min(max_chars or DEFAULT_BUDGET_CHARS, MAX_BUDGET_CHARS))
    chunk = ctx.source.get_step_logs(execution_id, leaf.node_id, RAW_LOG_CAP)
    blocks = error_blocks(chunk.text)
    if blocks:
        text, truncated = render_budgeted(blocks, budget)
    else:
        tail = "\n".join(collapse_repeats(clean_lines(chunk.text))[-FALLBACK_TAIL_LINES:])
        text, truncated = ("[no error blocks found; log tail]\n" + tail)[:budget], len(tail) > budget
    return {
        "execution_id": execution_id,
        "node_id": leaf.node_id,
        "step": leaf.name,
        "error_block_count": len(blocks),
        "blocks": [{"rank": b.rank, "signature": b.signature, "line": b.line_start, "count": b.count} for b in blocks],
        "text": text,
        "budget_chars": budget,
        "truncated": truncated or chunk.truncated,
    }


def _handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return get_step_logs(ctx, args["execution_id"], args.get("node_id"), args.get("max_chars"))


SPEC = ToolSpec(
    name="get_step_logs",
    description=(
        "Error blocks from the failed step's log only (never the whole log), with ANSI codes stripped, repeated "
        "spinner/retry lines collapsed and credentials redacted. Returns ALL error blocks ranked most operative "
        "first: the first error in a log is often not the cause, so read the ranking and the later blocks. "
        f"Output is capped at {DEFAULT_BUDGET_CHARS} characters by default. Medium cost."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "execution_id": {"type": "string"},
            "node_id": {"type": "string", "description": "A failed leaf step; defaults to the first failed leaf."},
            "max_chars": {"type": "integer", "minimum": 200, "maximum": MAX_BUDGET_CHARS},
        },
        "required": ["execution_id"],
        "additionalProperties": False,
    },
    handler=_handle,
)
