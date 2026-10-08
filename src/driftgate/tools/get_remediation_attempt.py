"""get_remediation_attempt: a prior attempt and its verification result (re-investigation only)."""

from __future__ import annotations

from typing import Any

from driftgate.domain import SourceError

from .base import ToolContext, ToolSpec


def get_remediation_attempt(
    ctx: ToolContext, fingerprint: str | None = None, attempt_id: str | None = None
) -> dict[str, Any]:
    if attempt_id is not None:
        a = ctx.attempts.get(attempt_id)
        if a is None:
            raise SourceError(f"unknown attempt: {attempt_id}")
        return {"attempts": [a.to_dict()]}
    if fingerprint is None:
        raise ValueError("give fingerprint or attempt_id")
    return {"fingerprint": fingerprint, "attempts": [a.to_dict() for a in ctx.attempts.by_fingerprint(fingerprint)]}


def _handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return get_remediation_attempt(ctx, args.get("fingerprint"), args.get("attempt_id"))


SPEC = ToolSpec(
    name="get_remediation_attempt",
    description=(
        "Prior remediation attempts for a fingerprint (or one attempt by id) with their verification result. "
        "Only meaningful during re-investigation after a fix did not hold. Cheap."
    ),
    input_schema={
        "type": "object",
        "properties": {"fingerprint": {"type": "string"}, "attempt_id": {"type": "string"}},
        "additionalProperties": False,
    },
    handler=_handle,
)
