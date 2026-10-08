"""Loader/renderer for the verbatim error-string catalog (invariant 9: strings are never synthesized)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).with_name("error_catalog.json")


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    tool: str
    source: str
    box: bool
    summary: str
    lines: tuple[str, ...]

    def render(self, params: dict[str, Any]) -> list[str]:
        return [line.format(**params) for line in self.lines]

    def render_summary(self, params: dict[str, Any]) -> str:
        return self.summary.format(**params)


class Catalog:
    def __init__(self, entries: list[CatalogEntry]) -> None:
        self._by_id = {e.id: e for e in entries}

    def __getitem__(self, entry_id: str) -> CatalogEntry:
        return self._by_id[entry_id]

    def ids(self) -> list[str]:
        return sorted(self._by_id)


def load_catalog(path: Path = CATALOG_PATH) -> Catalog:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Catalog(
        [
            CatalogEntry(
                id=e["id"],
                tool=e["tool"],
                source=e["source"],
                box=e["box"],
                summary=e["summary"],
                lines=tuple(e["lines"]),
            )
            for e in raw["entries"]
        ]
    )
