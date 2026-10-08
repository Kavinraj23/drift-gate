"""read_repo_file: a file at a ref, size-capped and redacted."""

from __future__ import annotations

from typing import Any

from .base import ToolContext, ToolSpec
from .logtext import redact

DEFAULT_MAX_BYTES = 6000
HARD_MAX_BYTES = 20000


def read_repo_file(ctx: ToolContext, repo: str, path: str, ref: str, max_bytes: int | None = None) -> dict[str, Any]:
    cap = max(1, min(max_bytes or DEFAULT_MAX_BYTES, HARD_MAX_BYTES))
    f = ctx.source.read_file(repo, path, ref, cap)
    return {
        "repo": f.repo,
        "path": f.path,
        "ref": f.ref,
        "content": redact(f.content),
        "truncated": f.truncated,
        "max_bytes": cap,
    }


def _handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return read_repo_file(ctx, args["repo"], args["path"], args["ref"], args.get("max_bytes"))


SPEC = ToolSpec(
    name="read_repo_file",
    description=(
        "Read one file (workflow YAML, lockfile, terraform, config) from a repo at a ref. repo is the pipeline "
        "name; ref is the execution's refs.commit (a sha or 'main'). Size-capped; secrets redacted. Medium cost."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "path": {"type": "string"},
            "ref": {"type": "string"},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": HARD_MAX_BYTES},
        },
        "required": ["repo", "path", "ref"],
        "additionalProperties": False,
    },
    handler=_handle,
)
