"""Shared stubs for the holdout-fetch tests: a GET-only fake transport and a job-log fixture.

Error text: the only tool error text in the fixture log is read at test time from the verified (`captured-verbatim`)
entry `docker_pull_rate_limit` of `generator/error_catalog.json` (source recorded there), and the runner line
`##[error]Process completed with exit code 1.` is the runner's own wording as in tests/github/fixtures/job_9001.log.
The rest is STRUCTURAL SCAFFOLDING, not error text: timestamps, `##[group]` / `##[endgroup]` framing, and the `Run ...`
header titles. Repo names, ids and SHAs are placeholders.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from driftgate.eval.holdout_gh import HttpResponse
from driftgate.generator.catalog import load_catalog

REPO = "example-org/example-repo"
RUN_ID = 5001
JOB_ID = 9001
SHA = "ab" * 20
TOKEN = "FAKE-HOLDOUT-TOKEN-0123456789"  # not a real credential
API = "https://api.github.com"
BLOB = "https://pipelines.actions.githubusercontent.com/some/signed/path?sig=SIGNED-URL-SECRET"
CATALOG_LINE = "\n".join(load_catalog()["docker_pull_rate_limit"].lines)
RUNNER_ERROR = "##[error]Process completed with exit code 1."


def _t(sec: int) -> str:
    return f"2026-10-01T10:00:{sec:02d}.0000000Z "


def log_text(nl: str = "\n") -> str:
    lines = [
        (0, "##[group]Set up job"),
        (0, "##[endgroup]"),
        (3, "##[group]Run actions/checkout@v4"),
        (4, "##[endgroup]"),  # step 2's own closing line, stamped in the second step 3 starts
        (4, "##[group]Run docker pull example"),  # step 3 (the failed one) starts here
        (4, "##[endgroup]"),
        (5, CATALOG_LINE),
        (6, RUNNER_ERROR),
        (6, "##[group]Post Run actions/checkout@v4"),  # step 4 starts in the second step 3 ends
        (6, "##[endgroup]"),
        (7, "##[group]Complete job"),
    ]
    return nl.join(_t(s) + text for s, text in lines) + nl


def expected_section(nl: str = "\n") -> str:
    """Step 3 only: from its `Run` header to the runner error, nothing of steps 2 and 4."""
    lines = [(4, "##[group]Run docker pull example"), (4, "##[endgroup]"), (5, CATALOG_LINE), (6, RUNNER_ERROR)]
    return nl.join(_t(s) + text for s, text in lines) + nl


def step(name: str, number: int, conclusion: str, start: int, end: int) -> dict[str, Any]:
    return {
        "name": name,
        "number": number,
        "conclusion": conclusion,
        "started_at": f"2026-10-01T10:00:{start:02d}Z",
        "completed_at": f"2026-10-01T10:00:{end:02d}Z",
    }


def failed_job(job_id: int = JOB_ID, name: str = "build") -> dict[str, Any]:
    return {
        "id": job_id,
        "name": name,
        "conclusion": "failure",
        "steps": [
            step("Set up job", 1, "success", 0, 2),
            step("Run actions/checkout@v4", 2, "success", 3, 4),
            step("Pull image", 3, "failure", 4, 6),
            step("Post Run actions/checkout@v4", 4, "success", 6, 7),
            step("Complete job", 5, "success", 7, 7),
        ],
    }


def ok_job(job_id: int = 9000, name: str = "lint") -> dict[str, Any]:
    return {"id": job_id, "name": name, "conclusion": "success", "steps": []}


def run_doc(repo: str = REPO, run_id: int = RUN_ID, **over: Any) -> dict[str, Any]:
    doc = {
        "status": "completed",
        "conclusion": "failure",
        "head_sha": SHA,
        "html_url": f"https://github.com/{repo}/actions/runs/{run_id}",
        "run_attempt": 1,
    }
    return {**doc, **over}


def js(obj: Any, status: int = 200, headers: Mapping[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status, dict(headers or {}), json.dumps(obj).encode("utf-8"))


class FakeTransport:
    """GET only (the Transport protocol has no other method). Unrouted URLs fail the test."""

    def __init__(self, routes: dict[str, HttpResponse | Callable[[], HttpResponse]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, headers: Mapping[str, str], *, timeout: float, max_bytes: int) -> HttpResponse:
        self.calls.append((url, dict(headers)))
        route = self.routes.get(url)
        assert route is not None, f"unexpected request to {url[:60]}"
        return route() if callable(route) else route


def routes_for(
    repo: str = REPO,
    run_id: int = RUN_ID,
    jobs: list[dict[str, Any]] | None = None,
    log: bytes | None = None,
    blob: str = BLOB,
) -> dict[str, HttpResponse | Callable[[], HttpResponse]]:
    jobs = jobs if jobs is not None else [ok_job(), failed_job()]
    base = f"{API}/repos/{repo}/actions"
    r: dict[str, HttpResponse | Callable[[], HttpResponse]] = {
        f"{base}/runs/{run_id}": js(run_doc(repo, run_id)),
        f"{base}/runs/{run_id}/jobs?filter=latest&per_page=100": js({"total_count": len(jobs), "jobs": jobs}),
        blob: HttpResponse(200, {}, log if log is not None else log_text().encode("utf-8")),
    }
    for job in jobs:
        r[f"{base}/jobs/{job['id']}/logs"] = HttpResponse(302, {"location": blob}, b"")
    return r
