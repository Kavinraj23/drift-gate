"""Regenerate the committed sample behind `python tasks.py trace-report`.

`python -m driftgate.eval.trace_sample --data data --out fixtures/trace_sample`

The sample is produced by the real orchestrator on synthetic scenarios, with the scripted model (no network, no
key): a Tier 0 execution, a Tier 3 pull request, an escalation, a gate downgrade, a re-investigation after a failed
first attempt (drill d3), and two Tier 1 approval cases (one approved by a second person and executed through the N4
flow, one denied). Each scenario ran with its own audit log; here they are concatenated into one `audit.jsonl` with
`seq` renumbered across the file and each scenario's timestamps (and `expires_at`) shifted so that they do not
overlap. Nothing else is edited. `reports.json` holds `Report.to_dict()` for each execution, because the audit log
keeps evidence counts, not evidence items.

This module lives in `eval/` because choosing scenarios reads the labels; the report builder itself never does.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from driftgate.approval_flow import execute_approved
from driftgate.audit import AuditEntry
from driftgate.eval.e2e import DRILLS, run_drill, run_scenario
from driftgate.eval.ground_truth import FailureLabel, load_ground_truth
from driftgate.eval.tier12_cases import TRIGGER, World, build_world

SCENARIOS = ("sc-06", "sc-01", "sc-15", "sc-11")  # Tier 0, Tier 3, escalation, gate downgrade
RE_INVESTIGATION_DRILL = "d3-wrong-file"  # sc-17: CI red on the wrong file, one re-investigation, revised PR green
TIER1_APPROVED, TIER1_DENIED = "sc-10", "sc-12"
SCENARIO_GAP_S = 10_000.0

NOTE = """Sample audit log for `python tasks.py trace-report`

Produced by `python -m driftgate.eval.trace_sample --data data --out fixtures/trace_sample` (the real orchestrator,
scripted model, synthetic dataset, no network). Scenarios: sc-06 (Tier 0 re-run, verified), sc-01 (Tier 3 pull request),
sc-15 (escalation), sc-11 (a Tier 0 re-run the gate downgrades), sc-17 (drill d3: wrong first PR, CI red,
one re-investigation, revised PR green), sc-10 (Tier 1: self-approval refused, second
person approves, executed through the approval flow), sc-12 (Tier 1: denied).

Each scenario ran with its own audit log; they are concatenated here with `seq` renumbered across the file and
timestamps (and `expires_at`, on the same clock) shifted per scenario. Entries are otherwise untouched.
`reports.json` maps execution id to `Report.to_dict()`, because the audit log keeps evidence counts, not evidence
items. Error text comes from the synthetic dataset's real-source catalog; nothing is synthesized here.
"""


def _merge(parts: list[list[AuditEntry]]) -> list[AuditEntry]:
    merged: list[AuditEntry] = []
    for i, entries in enumerate(parts):
        base = min((e.ts for e in entries), default=0.0)
        shift = i * SCENARIO_GAP_S - base + 1_700_000_000.0
        for e in sorted(entries, key=lambda x: x.seq):
            payload = dict(e.payload)
            if isinstance(payload.get("expires_at"), float):  # an absolute time on the same clock as `ts`
                payload["expires_at"] += shift
            merged.append(replace(e, seq=len(merged), ts=e.ts + shift, payload=payload))
    return merged


def _label(truth_labels: list[FailureLabel], scenario_id: str) -> FailureLabel:
    return next(s for s in truth_labels if s.scenario_id == scenario_id)


def _tier1_world(label: FailureLabel, data_dir: Path, work: Path, *, deny: bool) -> tuple[World, dict[str, Any]]:
    world = build_world(label, data_dir, work)
    rid = world.request_id
    assert rid is not None, "the Tier 1 scenario must open an approval request"
    if deny:
        world.clock.advance(60)
        world.service.deny(rid, "bob.reviewer")
    else:
        world.clock.advance(30)
        try:
            world.service.approve(rid, TRIGGER)  # the person who triggered the run: refused and audited
        except Exception:  # noqa: BLE001 - the refusal is the point; it is in the audit log
            pass
        world.clock.advance(60)
        world.service.approve(rid, "alice.oncall")
        world.clock.advance(60)
        execute_approved(world.orch, world.store, rid, clock=world.clock)
    return world, world.outcome.report.to_dict()


def generate(data_dir: Path, out_dir: Path) -> list[Path]:
    truth = load_ground_truth(data_dir)
    labels = truth.scenarios()
    parts: list[list[AuditEntry]] = []
    reports: dict[str, Any] = {}
    for sid in SCENARIOS:
        label = _label(labels, sid)
        result = run_scenario(data_dir, label, held_out=False, mode="fake")
        assert result.outcome is not None
        parts.append(list(result.audit))
        reports[label.execution_id] = result.outcome.report.to_dict()
    drill = next(d for d in DRILLS if d.drill_id == RE_INVESTIGATION_DRILL)
    redo = run_drill(data_dir, drill, truth)
    assert redo.outcome is not None and redo.label is not None
    parts.append(list(redo.audit))
    reports[redo.label.execution_id] = redo.outcome.report.to_dict()
    for sid, deny in ((TIER1_APPROVED, False), (TIER1_DENIED, True)):
        label = _label(labels, sid)
        with tempfile.TemporaryDirectory(prefix="driftgate-sample-") as tmp:
            world, report = _tier1_world(label, data_dir, Path(tmp), deny=deny)
            try:
                parts.append(world.orch.audit.read())
            finally:
                world.close()
        reports[label.execution_id] = report
    out_dir.mkdir(parents=True, exist_ok=True)
    audit_path, reports_path, note_path = out_dir / "audit.jsonl", out_dir / "reports.json", out_dir / "README.txt"
    lines = [json.dumps(asdict(e), default=str, sort_keys=True) for e in _merge(parts)]
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    reports_path.write_text(json.dumps(reports, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    note_path.write_text(NOTE, encoding="utf-8", newline="\n")
    return [audit_path, reports_path, note_path]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--out", type=Path, default=Path("fixtures") / "trace_sample")
    args = ap.parse_args(argv)
    for p in generate(args.data, args.out):
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
