"""Real-log holdout store (MVP 2, N1): loading, validation, quotas, label timing, and the sealed test split.

Only `eval/` may read `holdout/` (invariant 8, extended). Nothing here fetches anything or touches the network;
`holdout-fetch` is a separate, human-run target.

What `validate` checks, deterministically and offline:
- labels and run records against `contracts/holdout.schema.json`;
- quotas: 30 cases, 10 dev / 20 test, at least 4 per layer L1..L4, at least 3 escalation cases (each unmet quota is
  reported with its count);
- every case has a stored log whose SHA-256 matches `log_sha256`, no orphan logs, and stored logs are unchanged by
  `redaction.redact` (nothing credential-shaped left);
- labels before runs: `labeled_at` is not in the future (injected clock) and not before the fetch date, and every
  run record has `ran_at` after its case's `labeled_at` and a `label_sha256` equal to the digest of the case's
  current label (so a label edited after a run invalidates that run).

The test split is sealed in code: per-case detail exists only for dev. Test cases are reachable only through
`Holdout.test_aggregate`, which hands each case to a scorer and returns just the aggregate. Messages for test cases
never include label or log content.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, TypeVar

from jsonschema import Draft202012Validator

from driftgate.redaction import redact

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DIR = ROOT / "holdout"
SCHEMA_PATH = ROOT / "contracts" / "holdout.schema.json"

SCHEMA_VERSION = 1
TOTAL = 30
SPLIT_QUOTA = {"dev": 10, "test": 20}
MIN_PER_LAYER = 4
LAYERS_REQUIRED = ("L1", "L2", "L3", "L4")
MIN_ESCALATIONS = 3

T = TypeVar("T")
A = TypeVar("A")


class SealedSplitError(PermissionError):
    """Per-case detail for the test split was requested. Only aggregates are available for it."""


class HoldoutInvalid(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("holdout is invalid: " + "; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class HoldoutCase:
    id: str
    split: str
    layer: str
    classification: str
    action_kind: str  # rerun | pr_fix | escalate
    fix: str | None
    rationale: str
    labeled_at: datetime
    raw: dict[str, Any]

    @property
    def escalation(self) -> bool:
        return self.action_kind == "escalate"


@dataclass(frozen=True)
class RunRecord:
    case_id: str
    ran_at: datetime
    label_sha256: str
    layer: str
    classification: str
    action_kind: str
    fix: str | None


@dataclass(frozen=True)
class ValidationReport:
    problems: tuple[str, ...]  # structural: schema, hashes, redaction, timing. Must be empty for a usable holdout.
    unmet_quotas: tuple[str, ...]  # data not yet collected
    counts: dict[str, int]

    @property
    def ok(self) -> bool:
        return not self.problems and not self.unmet_quotas


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def label_digest(raw_case: dict[str, Any]) -> str:
    """Digest of a case's label record (canonical JSON). Run records pin it, so editing a label afterwards shows."""
    return sha256_bytes(json.dumps(raw_case, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validators() -> tuple[Draft202012Validator, Draft202012Validator]:
    schema = _schema()
    runs = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": "#/$defs/runs_document"}
    return Draft202012Validator(schema), Draft202012Validator(runs)


def _read_json(path: Path, problems: list[str]) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        problems.append(f"{path.name}: missing")
    except (OSError, ValueError) as e:
        problems.append(f"{path.name}: unreadable ({type(e).__name__})")
    else:
        if isinstance(data, dict):
            return data
        problems.append(f"{path.name}: top level must be an object")
    return None


def _schema_messages(validator: Draft202012Validator, doc: dict[str, Any], name: str) -> list[str]:
    """Schema errors without echoing instance values: a test case's content must not leak into messages."""
    out = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: [str(p) for p in e.absolute_path]):
        path = "/".join(str(p) for p in err.absolute_path) or "(root)"
        out.append(f"{name}: schema violation at {path} ({err.validator})")
    return out


def _case(raw: dict[str, Any]) -> HoldoutCase:
    action = raw["correct_action"]
    return HoldoutCase(
        id=raw["id"],
        split=raw["split"],
        layer=raw["layer"],
        classification=raw["classification"],
        action_kind=action["kind"],
        fix=action.get("fix"),
        rationale=raw["rationale"],
        labeled_at=parse_ts(raw["labeled_at"]),
        raw=raw,
    )


def _run(raw: dict[str, Any]) -> RunRecord:
    return RunRecord(
        case_id=raw["case_id"],
        ran_at=parse_ts(raw["ran_at"]),
        label_sha256=raw["label_sha256"],
        layer=raw["layer"],
        classification=raw["classification"],
        action_kind=raw["action"]["kind"],
        fix=raw["action"].get("fix"),
    )


def _quotas(cases: list[HoldoutCase]) -> tuple[list[str], dict[str, int]]:
    counts = {
        "total": len(cases),
        "dev": sum(c.split == "dev" for c in cases),
        "test": sum(c.split == "test" for c in cases),
        "escalations": sum(c.escalation for c in cases),
        **{layer: sum(c.layer == layer for c in cases) for layer in LAYERS_REQUIRED},
    }
    unmet = []
    if counts["total"] != TOTAL:
        unmet.append(f"total cases: {counts['total']}/{TOTAL}")
    for split, want in SPLIT_QUOTA.items():
        if counts[split] != want:
            unmet.append(f"{split} split: {counts[split]}/{want}")
    for layer in LAYERS_REQUIRED:
        if counts[layer] < MIN_PER_LAYER:
            unmet.append(f"layer {layer}: {counts[layer]}/{MIN_PER_LAYER} minimum")
    if counts["escalations"] < MIN_ESCALATIONS:
        unmet.append(f"escalation cases: {counts['escalations']}/{MIN_ESCALATIONS} minimum")
    return unmet, counts


def _check_logs(root: Path, cases: list[HoldoutCase], problems: list[str]) -> None:
    logs = root / "logs"
    stored = {p.stem for p in logs.glob("*.log")} if logs.is_dir() else set()
    for c in cases:
        path = logs / f"{c.id}.log"
        if not path.is_file():
            problems.append(f"{c.id}: logs/{c.id}.log is missing")
            continue
        data = path.read_bytes()
        if sha256_bytes(data) != c.raw["log_sha256"]:
            problems.append(f"{c.id}: stored log does not match log_sha256")
        text = data.decode("utf-8", errors="replace")
        if redact(text) != text:
            problems.append(f"{c.id}: stored log is changed by redaction (credential-shaped text left in it)")
    for orphan in sorted(stored - {c.id for c in cases}):
        problems.append(f"logs/{orphan}.log has no label")


def _check_label_timing(cases: list[HoldoutCase], now: datetime, problems: list[str]) -> None:
    for c in cases:
        if c.labeled_at > now:
            problems.append(f"{c.id}: labeled_at is in the future")
        fetched = date.fromisoformat(c.raw["source"]["fetch_date"])
        if c.labeled_at.date() < fetched:
            problems.append(f"{c.id}: labeled_at is before the log's fetch_date")


def _check_runs(cases: list[HoldoutCase], runs: list[RunRecord], problems: list[str]) -> None:
    by_id = {c.id: c for c in cases}
    for r in runs:
        c = by_id.get(r.case_id)
        if c is None:
            problems.append(f"runs.json: run for unknown case {r.case_id}")
            continue
        if r.ran_at <= c.labeled_at:
            problems.append(f"{r.case_id}: run at or before labeled_at (labels must be written before the agent runs)")
        if r.label_sha256 != label_digest(c.raw):
            problems.append(f"{r.case_id}: label changed after the run (label_sha256 mismatch); the run is void")


def _parse(root: Path) -> tuple[list[HoldoutCase], list[RunRecord], list[str]]:
    """Structural parse. A document that fails the schema contributes no cases or runs (and is reported)."""
    problems: list[str] = []
    labels_v, runs_v = _validators()
    cases: list[HoldoutCase] = []
    runs: list[RunRecord] = []
    labels = _read_json(root / "labels.json", problems)
    if labels is not None:
        msgs = _schema_messages(labels_v, labels, "labels.json")
        problems += msgs
        if not msgs:
            cases = [_case(c) for c in labels["cases"]]
    if (root / "runs.json").exists():
        run_doc = _read_json(root / "runs.json", problems)
        if run_doc is not None:
            msgs = _schema_messages(runs_v, run_doc, "runs.json")
            problems += msgs
            if not msgs:
                runs = [_run(r) for r in run_doc["runs"]]
    return cases, runs, problems


def validate(root: Path = DEFAULT_DIR, *, now: datetime) -> ValidationReport:
    cases, runs, problems = _parse(root)
    id_counts = Counter(c.id for c in cases)
    problems += [f"duplicate case id {i}" for i, n in sorted(id_counts.items()) if n > 1]
    src_counts = Counter((c.raw["source"]["repo"], c.raw["source"]["run_id"], c.raw["source"]["job"]) for c in cases)
    problems += [f"duplicate source run/job {r}#{i} {j}" for (r, i, j), n in sorted(src_counts.items()) if n > 1]
    if not problems:
        _check_logs(root, cases, problems)
        _check_label_timing(cases, now, problems)
        _check_runs(cases, runs, problems)
    unmet, counts = _quotas(cases)
    return ValidationReport(tuple(problems), tuple(unmet), counts)


class Holdout:
    """A validated holdout. Per-case detail is for dev only; the test split yields aggregates through one door."""

    def __init__(self, root: Path, cases: list[HoldoutCase], runs: list[RunRecord], report: ValidationReport) -> None:
        self._root = root
        self._cases = tuple(cases)
        self._runs = {r.case_id: r for r in runs}  # runs arrive sorted by ran_at: the latest record per case wins
        self.report = report

    @classmethod
    def load(cls, root: Path = DEFAULT_DIR, *, now: datetime, allow_unmet_quotas: bool = True) -> Holdout:
        report = validate(root, now=now)
        if report.problems or (report.unmet_quotas and not allow_unmet_quotas):
            raise HoldoutInvalid([*report.problems, *report.unmet_quotas])
        cases, runs, _ = _parse(root)
        return cls(root, cases, sorted(runs, key=lambda r: r.ran_at), report)

    def counts(self) -> dict[str, int]:
        return dict(self.report.counts)

    def _log(self, case: HoldoutCase) -> str:
        return (self._root / "logs" / f"{case.id}.log").read_bytes().decode("utf-8")

    # -- dev: per-case detail allowed -------------------------------------------------------------------------
    def dev_cases(self) -> tuple[HoldoutCase, ...]:
        return tuple(c for c in self._cases if c.split == "dev")

    def case(self, case_id: str) -> HoldoutCase:
        found = next((c for c in self._cases if c.id == case_id), None)
        if found is None:
            raise KeyError(case_id)
        if found.split != "dev":
            raise SealedSplitError("the test split is sealed: only aggregate metrics are available")
        return found

    def dev_log(self, case_id: str) -> str:
        return self._log(self.case(case_id))

    def dev_details(self, scorer: Callable[[HoldoutCase, str, RunRecord | None], T]) -> list[tuple[HoldoutCase, T]]:
        return [(c, scorer(c, self._log(c), self._runs.get(c.id))) for c in self.dev_cases()]

    # -- test: aggregate only ---------------------------------------------------------------------------------
    def test_cases(self) -> tuple[HoldoutCase, ...]:
        raise SealedSplitError("the test split is sealed: only aggregate metrics are available")

    def test_aggregate(
        self,
        scorer: Callable[[HoldoutCase, str, RunRecord | None], T],
        aggregate: Callable[[list[T]], A],
    ) -> A:
        """Score every test case and return only `aggregate(scores)`; the per-case scores are not kept."""
        scores = [scorer(c, self._log(c), self._runs.get(c.id)) for c in self._cases if c.split == "test"]
        return aggregate(scores)
