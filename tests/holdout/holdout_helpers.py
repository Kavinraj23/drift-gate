"""Shared scaffolding for the N1 runner tests: a tiny holdout built in tmp_path.

SCAFFOLDING, NOT DATA. Case metadata (repos, run ids, SHAs, labels) is structure invented for the test. The LOG TEXT is
never typed here: it is copied at test time from `captured-verbatim` entries of `generator/error_catalog.json`
(their `source` URLs are recorded there), so no error string is synthesized (invariant 9).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from driftgate.eval import holdout as hd
from driftgate.generator.catalog import load_catalog

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
LABELED = "2026-10-15T09:00:00Z"
FETCHED = "2026-10-14"


def verbatim(entry_id: str) -> str:
    return "\n".join(load_catalog()[entry_id].lines) + "\n"


RATE_LIMIT_LOG = verbatim("docker_pull_rate_limit")  # signature docker_pull_rate_limit: L2, Tier 0 rule exists
EXPIRED_LOG = verbatim("aws_expired_token")  # signature aws_expired_token: L2, no Tier 0 rule

RATIONALES = {
    "hc-01": "DEVRATIONALE-one",
    "hc-02": "DEVRATIONALE-two",
    "hc-11": "TESTRATIONALE-eleven",
    "hc-12": "TESTRATIONALE-twelve",
}


def raw_case(n: int, *, split: str, kind: str, log: str) -> dict[str, Any]:
    action: dict[str, Any] = {"kind": kind}
    if kind == "pr_fix":
        action["fix"] = "fix_secret_scope"
    case_id = f"hc-{n:02d}"
    return {
        "id": case_id,
        "split": split,
        "layer": "L2",
        "classification": "transient" if kind == "rerun" else "user",
        "correct_action": action,
        "rationale": RATIONALES[case_id],
        "source": {
            "repo": "example-org/example-repo",
            "run_id": 1000 + n,
            "job": f"job-{n}",
            "commit_sha": f"{n:040x}",
            "url": f"https://github.com/example-org/example-repo/actions/runs/{1000 + n}",
            "fetch_date": FETCHED,
        },
        "log_sha256": hd.sha256_bytes(log.encode("utf-8")),
        "labeled_at": LABELED,
    }


def make_holdout(root: Path, *, labeled_at: str = LABELED) -> Path:
    """Two dev and two test cases. hc-01 and hc-11 carry a signature with a Tier 0 rule; hc-02 and hc-12 do not."""
    spec = [
        (1, "dev", "rerun", RATE_LIMIT_LOG),
        (2, "dev", "escalate", EXPIRED_LOG),
        (11, "test", "rerun", RATE_LIMIT_LOG),
        (12, "test", "pr_fix", EXPIRED_LOG),
    ]
    cases, logs = [], {}
    for n, split, kind, log in spec:
        case = raw_case(n, split=split, kind=kind, log=log)
        case["labeled_at"] = labeled_at
        cases.append(case)
        logs[case["id"]] = log
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "labels.json").write_text(json.dumps({"schema_version": 1, "cases": cases}), encoding="utf-8")
    (root / "runs.json").write_text(json.dumps({"schema_version": 1, "runs": []}), encoding="utf-8")
    for case_id, text in logs.items():
        (root / "logs" / f"{case_id}.log").write_bytes(text.encode("utf-8"))
    return root


def clean_git(_root: Path) -> str:
    return ""


def dirty_git(_root: Path) -> str:
    return " M labels.json\n"


def unknown_git(_root: Path) -> None:
    return None


def clock_at(moment: datetime = NOW):  # type: ignore[no-untyped-def]
    return lambda: moment
