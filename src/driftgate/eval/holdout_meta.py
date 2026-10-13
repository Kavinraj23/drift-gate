"""Run-record provenance for the real-log holdout: the sidecar `holdout/runs_meta.json` and the check that ties it to
`holdout/runs.json`.

`runs.json` follows an unchanged schema, so it cannot say WHO produced a record. The sidecar does, per case: the mode
(`record` = live against the real API, `replay` = served from fixtures that `record-holdout` wrote), the model, the
fixture id, an explicit `provenance` of `recorded_real_model`, and a digest of the exact run record it describes.

`eval-holdout` refuses to build the agent column unless EVERY latest run record has such an entry that matches it:
- a scripted double / fake-mode entry                                  -> refused (never an agent result);
- no sidecar entry at all (unknown provenance)                         -> refused;
- an entry whose digest does not match the latest record, or whose provenance is not the real-model value
  (for example a record appended by hand after the real one)           -> refused.
The messages carry COUNTS only, never case ids (the test split is sealed).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from driftgate.eval.holdout import parse_ts

RUNS_FILE = "runs.json"
META_FILE = "runs_meta.json"
META_VERSION = 2

MODE_RECORD = "record"
MODE_REPLAY = "replay"
MODE_FAKE = "fake"
REAL_MODES = frozenset({MODE_RECORD, MODE_REPLAY})
PROVENANCE_REAL = "recorded_real_model"
PROVENANCE_SCRIPTED = "scripted_double"
SCRIPTED_MODES = frozenset({MODE_FAKE, "scripted"})


def record_digest(record: dict[str, Any]) -> str:
    """Digest of one run record (canonical JSON). The sidecar pins the record it describes."""
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def provenance_for(mode: str) -> str:
    return PROVENANCE_REAL if mode in REAL_MODES else PROVENANCE_SCRIPTED


def _read(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def latest_records(root: Path) -> dict[str, dict[str, Any]]:
    """The latest raw run record per case, chosen exactly as `Holdout` does (stable sort by ran_at; later wins)."""
    runs = [r for r in _read(root / RUNS_FILE).get("runs", []) if isinstance(r, dict)]
    ordered = sorted(enumerate(runs), key=lambda p: (parse_ts(p[1]["ran_at"]), p[0]))
    return {r["case_id"]: r for _i, r in ordered}


def load_meta(root: Path) -> dict[str, dict[str, Any]]:
    runs = _read(root / META_FILE).get("runs", {})
    return {k: v for k, v in runs.items() if isinstance(v, dict)} if isinstance(runs, dict) else {}


@dataclass(frozen=True)
class Provenance:
    """Counts only. `ok` means every latest run record is backed by a matching real-model sidecar entry."""

    records: int = 0
    scripted: int = 0
    missing: int = 0
    mismatched: int = 0
    models: tuple[str, ...] = ()
    modes: dict[str, int] = field(default_factory=dict)
    tools: tuple[str, ...] = ()
    tools_backing: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not (self.scripted or self.missing or self.mismatched)

    def problems(self) -> list[str]:
        out: list[str] = []
        if self.scripted:
            out.append(f"{self.scripted} run record(s) come from a fake/scripted double (never an agent result)")
        if self.missing:
            out.append(f"{self.missing} run record(s) have no provenance entry in {META_FILE} (unknown provenance)")
        if self.mismatched:
            out.append(
                f"{self.mismatched} run record(s) do not match their {META_FILE} entry (changed or appended after "
                "the run, or not marked as a recorded real-model run)"
            )
        return out

    def header(self) -> str:
        replay, live = self.modes.get(MODE_REPLAY, 0), self.modes.get(MODE_RECORD, 0)
        return (
            f"Agent provenance: {PROVENANCE_REAL}; model {', '.join(self.models) or 'n/a'}; mode: {replay} case(s) "
            f"replayed from recorded fixtures, {live} recorded live; tool set: {', '.join(self.tools) or 'n/a'} "
            f"(optional tools backing: {', '.join(self.tools_backing) or 'n/a'}); {self.records} run record(s)."
        )


def check_provenance(root: Path) -> Provenance:
    latest, meta = latest_records(root), load_meta(root)
    scripted = missing = mismatched = 0
    models: set[str] = set()
    tools: set[str] = set()
    backing: set[str] = set()
    modes: Counter[str] = Counter()
    for case_id, record in latest.items():
        m = meta.get(case_id)
        if m is None:
            missing += 1
            continue
        mode, model = str(m.get("mode", "")), str(m.get("model", ""))
        if mode in SCRIPTED_MODES or m.get("provenance") == PROVENANCE_SCRIPTED or model.lower().startswith("fake"):
            scripted += 1
            continue
        valid = (
            m.get("provenance") == PROVENANCE_REAL
            and mode in REAL_MODES
            and bool(model)
            and bool(m.get("fixture_id"))
            and m.get("record_sha256") == record_digest(record)
        )
        if not valid:
            mismatched += 1
            continue
        models.add(model)
        tools.add(str(m.get("tools", "")))
        backing.add(str(m.get("tools_backing", "")))
        modes[mode] += 1
    return Provenance(
        records=len(latest),
        scripted=scripted,
        missing=missing,
        mismatched=mismatched,
        models=tuple(sorted(models)),
        modes=dict(modes),
        tools=tuple(sorted(tools)),
        tools_backing=tuple(sorted(backing)),
    )
