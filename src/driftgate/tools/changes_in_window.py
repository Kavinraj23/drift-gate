"""changes_in_window: commits and platform events in a window BEFORE the case's first failure (OPTIONAL tool, N3).

Read-only. A malformed source row (missing `at` or `change_id`, unparseable `at`) makes the call fail with a
SourceError, which `dispatch` returns as an ok=False result; the row is not skipped silently. Every free-text field is
redacted. It returns everything the source's change timeline holds for the window, nearest the failure first and
capped. It does not know which entries are causal and which are decoys; the source only exposes agent-visible fields
(`CHANGE_FIELDS`), so a causal marker added to the data later still could not reach the model through here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol

from driftgate.domain import SourceError
from driftgate.redaction import redact

from .base import ToolContext, ToolSpec
from .fleet_correlate import parse_time

DEFAULT_WINDOW_MINUTES = 360
MAX_WINDOW_MINUTES = 4320
MAX_CHANGES = 25
MAX_STREAK_LOOKBACK = 50  # executions walked back to find where this failure episode began
CHANGE_FIELDS = ("change_id", "at", "kind", "ref", "actor", "message", "files", "sha")  # the whitelist


class ChangeSource(Protocol):
    """Optional, additive companion to `ExecutionSource`: a source that can list its change timeline.

    `window` is (start, end), inclusive; the tool applies the strict "before the failure" bound itself.
    """

    def list_changes(self, window: Any = None) -> list[dict[str, Any]]: ...


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def first_failure_time(ctx: ToolContext, execution_id: str) -> tuple[str, datetime] | None:
    """Start of the failure episode this execution belongs to: the earliest of the unbroken run of earlier failures
    in the same pipeline that share its fingerprint (a success or a different fingerprint ends the episode).

    Returns (execution id, start time), or None when the execution did not fail.
    """
    analysis = ctx.analyzer.analyze(execution_id)
    if analysis is None:
        return None
    ex = ctx.source.get_execution(execution_id)
    now = parse_time(ex.started_at)
    earlier = [
        h
        for h in ctx.source.list_executions(None, {"pipeline": ex.pipeline})
        if parse_time(h.started_at) < now and h.execution_id != execution_id
    ]
    first_id, first_at = execution_id, now
    for h in reversed(earlier[-MAX_STREAK_LOOKBACK:]):
        if h.status != "failed":
            break
        a = ctx.analyzer.analyze(h.execution_id)
        if a is None or a.fingerprint != analysis.fingerprint:
            break
        first_id, first_at = h.execution_id, parse_time(h.started_at)
    return first_id, first_at


def changes_in_window(
    ctx: ToolContext, execution_id: str, window_minutes: int = DEFAULT_WINDOW_MINUTES
) -> dict[str, Any]:
    if not isinstance(window_minutes, int) or isinstance(window_minutes, bool):
        raise ValueError("window_minutes must be an integer")
    if not 1 <= window_minutes <= MAX_WINDOW_MINUTES:
        raise ValueError(f"window_minutes must be between 1 and {MAX_WINDOW_MINUTES}")
    list_changes = getattr(ctx.source, "list_changes", None)
    if list_changes is None:
        raise SourceError("this source does not expose a change timeline")
    anchor = first_failure_time(ctx, execution_id)
    base: dict[str, Any] = {"execution_id": execution_id, "window_minutes": window_minutes}
    if anchor is None:
        return {**base, "note": "execution did not fail", "changes": [], "total_in_window": 0, "truncated": False}
    first_id, first_at = anchor
    start = first_at - timedelta(minutes=window_minutes)
    try:  # a malformed source row is a tool error (ok=False via dispatch), never an exception out of the tool
        rows = [
            c for c in list_changes((_iso(start), _iso(first_at))) if parse_time(c["at"]) < first_at
        ]  # strictly before
        for c in rows:
            if not isinstance(c.get("change_id"), str):
                raise KeyError("change_id")
        rows.sort(key=lambda c: (parse_time(c["at"]), c["change_id"]), reverse=True)  # nearest first
    except (KeyError, ValueError, TypeError, AttributeError) as e:
        raise SourceError(f"malformed change row in the source timeline ({type(e).__name__}: {e})") from e
    out: list[dict[str, Any]] = []
    for c in rows[:MAX_CHANGES]:
        item = {k: c[k] for k in CHANGE_FIELDS if k in c}
        for k in ("change_id", "kind", "ref", "actor", "message", "sha"):  # every free-text field; ids survive intact
            if k in item:
                item[k] = redact(str(item[k]))
        item.setdefault("message", "")
        item["files"] = [redact(str(f)) for f in item.get("files", [])]
        item["minutes_before_failure"] = int((first_at - parse_time(c["at"])).total_seconds() // 60)
        out.append(item)
    return {
        **base,
        "first_failure_execution": first_id,
        "first_failure_at": _iso(first_at),
        "window_start": _iso(start),
        "total_in_window": len(rows),
        "truncated": len(rows) > MAX_CHANGES,
        "changes": out,
    }


def _handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return changes_in_window(ctx, args["execution_id"], args.get("window_minutes", DEFAULT_WINDOW_MINUTES))


SPEC = ToolSpec(
    name="changes_in_window",
    description=(
        "List the commits and platform events (template releases, connector, runner-pool and config changes) in the "
        f"{DEFAULT_WINDOW_MINUTES // 60} hours (configurable with window_minutes, up to {MAX_WINDOW_MINUTES}) before "
        "the FIRST failure of this failure episode, nearest the failure first, at most "
        f"{MAX_CHANGES}. Nothing at or after the failure is returned. It lists everything that changed, related or "
        "not: a change being listed is not evidence that it caused the failure, and unrelated changes are common. "
        "Weigh each against the error, the files and the refs involved before naming one as suspected_change. Cheap."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "execution_id": {"type": "string"},
            "window_minutes": {"type": "integer", "minimum": 1, "maximum": MAX_WINDOW_MINUTES},
        },
        "required": ["execution_id"],
        "additionalProperties": False,
    },
    handler=_handle,
)

#: Added to the investigator system prompt only when this tool is enabled.
PROMPT_ADDENDUM = (
    "\n\nChange correlation (enabled for this run)\n"
    "- changes_in_window lists what changed before the first failure, decoys included. Call it only when a recent "
    "change could explain the failure. If you name a suspected_change, set `ref` to the listed change_id, and "
    "justify it in `basis` from the error and the files; omit suspected_change when no listed change fits."
)
