"""Offline labels skeleton for fetched-but-unlabelled holdout cases (`python tasks.py holdout-fetch --skeleton`).

Prints, to stdout, a JSON array of case records to paste into `holdout/labels.json` under `"cases"`. The source block
and `log_sha256` come from `holdout/fetched/<id>.json`; every label field is a placeholder that FAILS
`holdout-validate` (an unknown enum value, an empty rationale, a non-timestamp), so a forgotten field cannot pass. The
human chooses layer, classification, action, rationale, split and `labeled_at`, before the agent ever runs.

Invariant 15: this reads fetched METADATA only. It never decodes or prints any log text; for each log it hashes the
bytes to check them against the record. Per case it reports (to stderr) only the id, repo, run id, job, step name and
size. It does not look at `split` because none is assigned yet.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TextIO

from jsonschema import Draft202012Validator

from driftgate.eval.holdout import sha256_bytes

PLACEHOLDER = "FILL_ME"  # not a member of any enum, not a timestamp, so validation rejects it


def _fetched_validator() -> Draft202012Validator:
    schema = json.loads(
        (Path(__file__).resolve().parents[3] / "contracts" / "holdout_candidates.schema.json").read_text("utf-8")
    )
    return Draft202012Validator(
        {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": "#/$defs/fetched_record"}
    )


def _labelled_ids(root: Path) -> set[str]:
    try:
        doc = json.loads((root / "labels.json").read_text(encoding="utf-8"))
        return {c["id"] for c in doc["cases"]}
    except (OSError, ValueError, KeyError, TypeError):
        return set()


def build_skeleton(root: Path) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """(case skeletons, summary lines, problems)."""
    validator = _fetched_validator()
    done = _labelled_ids(root)
    cases: list[dict[str, Any]] = []
    summary: list[str] = []
    problems: list[str] = []
    fetched = root / "fetched"
    for path in sorted(fetched.glob("*.json")) if fetched.is_dir() else []:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            problems.append(f"{path.name}: unreadable")
            continue
        if next(validator.iter_errors(rec), None) is not None or rec["case_id"] != path.stem:
            problems.append(f"{path.name}: not a valid fetched record")
            continue
        cid = rec["case_id"]
        if cid in done:
            continue
        log = root / "logs" / f"{cid}.log"
        if not log.is_file() or sha256_bytes(log.read_bytes()) != rec["log_sha256"]:
            problems.append(f"{cid}: stored log is missing or does not match the fetched record")
            continue
        src = rec["source"]
        cases.append(
            {
                "id": cid,
                "split": PLACEHOLDER,
                "layer": PLACEHOLDER,
                "classification": PLACEHOLDER,
                "correct_action": {"kind": PLACEHOLDER},
                "rationale": "",
                "source": src,
                "log_sha256": rec["log_sha256"],
                "labeled_at": PLACEHOLDER,
            }
        )
        summary.append(
            f"# {cid}  {src['repo']}  run {src['run_id']}  job {src['job']!r}  step {rec['step']['name']!r}  "
            f"{rec['log_bytes']} bytes"
        )
    return cases, summary, problems


def print_skeleton(root: Path, *, out: TextIO, err: TextIO) -> int:
    cases, summary, problems = build_skeleton(root)
    for line in summary:
        print(line, file=err)
    for p in problems:
        print(f"problem: {p}", file=err)
    if not cases:
        print("# no unlabelled fetched cases", file=err)
    print(json.dumps(cases, indent=2), file=out)
    return 1 if problems else 0
