"""Read-only agent tools, one module each, each a thin wrapper over a deterministic function."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from driftgate.domain import SourceError

from . import (
    classify_signature,
    flake_history,
    fleet_correlate,
    get_execution,
    get_remediation_attempt,
    get_step_logs,
    read_repo_file,
)
from .base import ToolContext, ToolResult, ToolSpec

TOOLS: tuple[ToolSpec, ...] = (
    get_execution.SPEC,
    classify_signature.SPEC,
    fleet_correlate.SPEC,
    flake_history.SPEC,
    get_step_logs.SPEC,
    read_repo_file.SPEC,
    get_remediation_attempt.SPEC,
)
_BY_NAME = {t.name: t for t in TOOLS}


def tool_definitions(names: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """Anthropic-format tool definitions (name, description, input_schema), in a stable order."""
    return [t.definition() for t in TOOLS if names is None or t.name in names]


def dispatch(ctx: ToolContext, name: str, args: dict[str, Any], call_id: str) -> ToolResult:
    """Run one tool call. `call_id` becomes the result's `source`, the id evidence must cite.

    Bad names, bad arguments and source errors come back as ok=False results rather than exceptions.
    """
    spec = _BY_NAME.get(name)
    if spec is None:
        return ToolResult(call_id, name, False, error=f"unknown tool: {name}")
    errors = sorted(Draft202012Validator(spec.input_schema).iter_errors(args), key=lambda e: list(e.path))
    if errors:
        return ToolResult(call_id, name, False, error=f"invalid arguments: {errors[0].message}")
    try:
        return ToolResult(call_id, name, True, data=spec.handler(ctx, args))
    except (SourceError, ValueError) as e:
        return ToolResult(call_id, name, False, error=str(e))


__all__ = ["TOOLS", "ToolContext", "ToolResult", "ToolSpec", "dispatch", "tool_definitions"]
