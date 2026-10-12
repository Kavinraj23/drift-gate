"""Fixtures for the N5 trace report tests: the committed sample, an HTML inspector and entry builders."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from report_helpers import SAMPLE

from driftgate.audit import AuditEntry
from driftgate.report import build_pages, load_audit, load_reports


@pytest.fixture(scope="session")
def sample_entries() -> list[AuditEntry]:
    entries, skipped = load_audit(SAMPLE / "audit.jsonl")
    assert skipped == 0
    return entries


@pytest.fixture(scope="session")
def sample_reports() -> dict[str, Any]:
    return load_reports(SAMPLE / "reports.json")


@pytest.fixture(scope="session")
def sample_pages(sample_entries: list[AuditEntry], sample_reports: dict[str, Any]) -> dict[str, str]:
    return build_pages(sample_entries, sample_reports)


Make = Callable[..., AuditEntry]


@pytest.fixture
def make() -> Make:
    counter = {"n": 0}

    def _make(
        kind: str, stage: str = "", execution_id: str = "ex-1", ts: float = 1_700_000_000.0, **payload: Any
    ) -> AuditEntry:
        counter["n"] += 1
        if stage:
            payload["stage"] = stage
        return AuditEntry(counter["n"], ts + counter["n"], kind, "fp-1", execution_id, payload)

    return _make
