"""similar_history: earlier incidents whose error looks like this one, with what was done and how it ended.

Optional and off by default (see `OPTIONAL_TOOLS`). Read-only: it queries the index, it never writes it. The query
text is derived in code from the case's own signature, step and key error excerpt, not from model-supplied text, and
the index query itself enforces the no-leakage rule (only incidents that finished strictly before this execution
started, never this execution; invariant 16). Results are redacted again on the way out.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from driftgate.domain import SourceError
from driftgate.redaction import redact_value
from driftgate.retrieval import MAX_FIELD_CHARS

from .base import RAW_LOG_CAP, FailureAnalysis, ToolContext, ToolSpec
from .fleet_correlate import parse_time
from .logtext import error_blocks

NAME = "similar_history"


def key_excerpt(ctx: ToolContext, analysis: FailureAnalysis) -> str:
    """The most operative error block of the failed step, redacted and clipped. Empty if the log is unreadable."""
    try:
        text = ctx.source.get_step_logs(analysis.execution_id, analysis.node_id, RAW_LOG_CAP).text
    except SourceError:  # retrieval falls back to the signature and step alone
        return ""
    blocks = error_blocks(text)
    return blocks[0].text[:MAX_FIELD_CHARS] if blocks else ""


def start_epoch(started_at: str) -> float:
    """Epoch seconds of an execution's start; an ISO time without a zone is read as UTC."""
    t = parse_time(started_at)
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).timestamp()


def query_text(analysis: FailureAnalysis, excerpt: str) -> str:
    """Most specific text first: signature id, step name, then the error excerpt."""
    return f"{analysis.primary.id} {analysis.step} {excerpt}"


def similar_history(ctx: ToolContext) -> dict[str, Any]:
    """Earlier incidents like the bound case. The case id and the as-of time come from `ctx.bound_case`, set by code
    before the investigation starts; with no bound case the tool refuses (fail closed)."""
    if ctx.history is None:
        raise ValueError("similar_history is not enabled for this run")
    bound = ctx.bound_case
    if bound is None:
        raise ValueError("similar_history is not bound to a case; refusing")
    execution_id = bound.execution_id
    ex = ctx.source.get_execution(execution_id)
    analysis = ctx.analyzer.analyze(execution_id)
    if analysis is None:
        return {"execution_id": execution_id, "matches": [], "note": "execution did not fail; nothing to look up"}
    matches = ctx.history.query(
        query_text(analysis, key_excerpt(ctx, analysis)), as_of=bound.as_of, case_id=execution_id
    )
    out: dict[str, Any] = {
        "execution_id": execution_id,
        "signature": analysis.primary.id,
        "fingerprint": analysis.fingerprint,
        "searched_incidents_finished_before": ex.started_at,
        "matches": [m.to_dict() for m in matches],
        "note": (
            "Earlier incidents ranked by text similarity. These are leads, not proof; a past outcome does not "
            "satisfy any Tier 0 condition."
            if matches
            else "No earlier incident matched."
        ),
    }
    safe = redact_value(out)
    assert isinstance(safe, dict)
    return safe


def _handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return similar_history(ctx)


SPEC = ToolSpec(
    name=NAME,
    description=(
        "Up to 3 earlier incidents whose signature and error text resemble this failure (BM25), each with the "
        "hypothesis, the action taken and how it ended (verified, not verified, escalated). Takes no arguments: it "
        "looks up the case you are investigating. Only incidents that finished before this execution started are "
        "searched. Leads only: a past outcome never satisfies a "
        "Tier 0 condition and the current evidence still decides. Cheap, but it counts toward your tool-call limit."
    ),
    input_schema={
        "type": "object",
        "properties": {},  # no arguments: the case under investigation is bound by code, never named by the model
        "additionalProperties": False,
    },
    handler=_handle,
)
