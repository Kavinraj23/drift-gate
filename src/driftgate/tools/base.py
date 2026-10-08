"""Shared tool types: result envelope, tool spec, injected context, fingerprinting and failure analysis."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from driftgate.domain import ExecutionSource, SourceError

from .attempts import AttemptStore
from .signatures import GENERIC_EXIT_ID, UNKNOWN, Signature, match_all

RAW_LOG_CAP = 400_000  # characters read from the source before extraction; the budget applies after


def fingerprint(signature_id: str, pipeline: str, step: str) -> str:
    """Deterministic hash of the normalized signature and the pipeline/step it occurred in."""
    norm = "|".join(" ".join(part.lower().split()) for part in (signature_id, pipeline, step))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class FailureAnalysis:
    execution_id: str
    pipeline: str
    step: str
    node_id: str
    signature_ids: tuple[str, ...]  # every signature found in the failed step, most operative first
    primary: Signature
    fingerprint: str

    @property
    def deterministic(self) -> bool:
        """True when the match names a cause (not a bare exit code and not unknown)."""
        return self.primary.id not in (GENERIC_EXIT_ID, UNKNOWN.id)


class FailureAnalyzer:
    """Derives the signature and fingerprint of a failed execution from its failed leaf step. Caches per instance."""

    def __init__(self, source: ExecutionSource) -> None:
        self._source = source
        self._cache: dict[str, FailureAnalysis | None] = {}

    def analyze(self, execution_id: str) -> FailureAnalysis | None:
        if execution_id not in self._cache:
            self._cache[execution_id] = self._analyze(execution_id)
        return self._cache[execution_id]

    def _analyze(self, execution_id: str) -> FailureAnalysis | None:
        ex = self._source.get_execution(execution_id)
        if ex.status != "failed":
            return None
        best: tuple[Signature, tuple[str, ...], str, str] | None = None
        for leaf in self._source.get_failed_leaf_nodes(execution_id):
            try:
                text = self._source.get_step_logs(execution_id, leaf.node_id, RAW_LOG_CAP).text
            except SourceError:
                text = ""
            hits = match_all(text) or match_all(leaf.error_summary)
            primary = hits[0] if hits else UNKNOWN
            if best is None or primary.rank > best[0].rank:
                best = (primary, tuple(h.id for h in hits), leaf.node_id, leaf.name)
        if best is None:
            return None
        primary, ids, node_id, step = best
        return FailureAnalysis(
            execution_id, ex.pipeline, step, node_id, ids, primary, fingerprint(primary.id, ex.pipeline, step)
        )


@dataclass(frozen=True)
class ToolResult:
    """What a tool call returns. `source` is the caller-supplied tool call id evidence must cite."""

    source: str
    tool: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "tool": self.tool, "ok": self.ok, "data": self.data, "error": self.error}


@dataclass
class ToolContext:
    """Everything a tool may touch, injected. Tools are read-only: there is no write path in here."""

    source: ExecutionSource
    attempts: AttemptStore = field(default_factory=AttemptStore)
    analyzer: FailureAnalyzer = field(init=False)

    def __post_init__(self) -> None:
        self.analyzer = FailureAnalyzer(self.source)


Handler = Callable[[ToolContext, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler

    def definition(self) -> dict[str, Any]:
        """Anthropic tool-definition format."""
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}
