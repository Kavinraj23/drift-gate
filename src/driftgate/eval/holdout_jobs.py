"""Real job counts of a fetched run (N1 follow-up), and the strict reader of `holdout/fetched/<id>.json`.

Why: the abort ceiling refuses an action that touches 100% of its resource set. For a Tier 0 re-run of one failed job
the set is "the jobs of the run", so the runner needs the REAL number of jobs the run had. The jobs list the fetcher
already downloads says so. This module turns that list into COUNTS (never names, never log text) and reads them back.

Nothing here is invented: `total` is the length of the latest attempt's jobs list (a truncated list is refused by the
client), and the per-conclusion counts are tallies of that same list. A conclusion GitHub did not report as one of the
four named values (including a missing one) is counted under `other`.

The reader accepts schema version 3 only. A version 2 record (no job counts) is refused with a clear message instead of
being silently treated as a one-job run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "holdout_candidates.schema.json"
FETCHED_VERSION = 3
NAMED = ("success", "failure", "cancelled", "skipped")


class FetchedRecordError(Exception):
    """A fetched record is unusable. The message names the file and the problem, never any content."""


@dataclass(frozen=True)
class JobCounts:
    total: int
    success: int
    failure: int
    cancelled: int
    skipped: int
    other: int

    def as_record(self) -> dict[str, int]:
        return {
            "total": self.total,
            "success": self.success,
            "failure": self.failure,
            "cancelled": self.cancelled,
            "skipped": self.skipped,
            "other": self.other,
        }

    @classmethod
    def from_record(cls, doc: dict[str, Any]) -> JobCounts:
        return cls(**{k: int(doc[k]) for k in ("total", "success", "failure", "cancelled", "skipped", "other")})


def count_jobs(jobs: list[dict[str, Any]]) -> JobCounts:
    """Tally the conclusions of the jobs list (counts only; nothing else of any job is kept)."""
    tally = dict.fromkeys(NAMED, 0)
    other = 0
    for job in jobs:
        c = job.get("conclusion")
        if c in tally:
            tally[str(c)] += 1
        else:
            other += 1
    return JobCounts(len(jobs), tally["success"], tally["failure"], tally["cancelled"], tally["skipped"], other)


def jobs_inconsistency(rec: dict[str, Any]) -> str | None:
    """Cross-field rules a JSON Schema cannot express, for a (schema-valid) fetched record. None when consistent:
    total >= 1, total == the sum of the conclusion counts, and the fetched job is among the counted failures."""
    j = rec["jobs"]
    parts = j["success"] + j["failure"] + j["cancelled"] + j["skipped"] + j["other"]
    if j["total"] < 1:
        return "jobs.total is less than 1"
    if j["total"] != parts:
        return "jobs.total does not equal the sum of the per-conclusion counts"
    if j["failure"] < 1:
        return "jobs.failure is 0, but the fetched job concluded failure"
    return None


def fetched_validator() -> Draft202012Validator:
    schema = json.loads(CONTRACT.read_text("utf-8"))
    return Draft202012Validator(
        {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": "#/$defs/fetched_record"}
    )


def version_problem(rec: Any) -> str | None:
    """A clear message when a record is from an older schema; None otherwise."""
    if isinstance(rec, dict) and rec.get("schema_version") == 2:
        return (
            "fetched record schema version 2 is no longer accepted (it has no job counts); "
            "re-fetch the run with `python tasks.py holdout-fetch` (version 3)"
        )
    return None


@dataclass(frozen=True)
class FetchedFacts:
    step_name: str
    jobs: JobCounts


def load_fetched_facts(root: Path, case_id: str) -> FetchedFacts | None:
    """The step name and job counts of `fetched/<id>.json`, or None when the file does not exist (a case whose
    metadata did not come from `holdout-fetch`). A file that exists but is unreadable, a version 2 record, or one that
    fails the schema or the cross-field checks raises `FetchedRecordError`."""
    path = root / "fetched" / f"{case_id}.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        raise FetchedRecordError(f"{path.name}: unreadable") from None
    try:
        rec = json.loads(text)
    except ValueError:
        raise FetchedRecordError(f"{path.name}: not valid JSON") from None
    if (msg := version_problem(rec)) is not None:
        raise FetchedRecordError(f"{path.name}: {msg}")
    if not isinstance(rec, dict) or next(fetched_validator().iter_errors(rec), None) is not None:
        raise FetchedRecordError(f"{path.name}: not a valid fetched record (schema version {FETCHED_VERSION})")
    if (bad := jobs_inconsistency(rec)) is not None:
        raise FetchedRecordError(f"{path.name}: {bad}")
    return FetchedFacts(rec["step"]["name"], JobCounts.from_record(rec["jobs"]))


def fetched_problems(root: Path) -> list[str]:
    """Counts only (no ids: the test split is sealed). Empty when every fetched record is readable and version 3."""
    folder = root / "fetched"
    bad = 0
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            load_fetched_facts(root, path.stem)
        except FetchedRecordError:
            bad += 1
    if not bad:
        return []
    return [
        f"{bad} fetched record(s) are unreadable, invalid or not schema version {FETCHED_VERSION} "
        "(version 2 records have no job counts); re-fetch them or run holdout-fetch --skeleton for the details"
    ]
