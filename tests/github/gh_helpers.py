"""Test doubles for the GitHub adapter: a recording stub session, a fake clock, and fixture loading.

Everything here is offline. The fixtures are hand-written stubs of documented GitHub REST shapes (see
fixtures/README.md); they are to be replaced or validated by human-recorded ones in M8b.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"
REPO = "octo-org/drift-gate-playground"
TOKEN = "ghp_" + "x" * 36  # a made-up value that merely has the shape of a token
API = "https://api.github.com"
PREFIX = f"{API}/repos/{REPO}"
HEAD_SHA = "c0ffee" * 6 + "c0ff"
BASE_SHA = "ba5e" * 10


def fixture(name: str) -> Any:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        body: Any = None,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> None:
        self.status_code = status
        self.headers = headers or {}
        if content is not None:
            self.content = content
        elif body is None:
            self.content = b""
        elif isinstance(body, str):
            self.content = body.encode("utf-8")
        else:
            self.content = json.dumps(body).encode("utf-8")
        self._body = body

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if isinstance(self._body, str) or self._body is None:
            return json.loads(self.content or b"null")
        return self._body


@dataclass
class Call:
    method: str
    url: str
    path: str  # relative to the repo, "" for URLs outside the API (redirect targets)
    params: dict[str, Any] | None
    json: dict[str, Any] | None
    headers: dict[str, str]
    allow_redirects: bool | None


Route = FakeResponse | list[FakeResponse] | Callable[[Call], FakeResponse]


@dataclass
class FakeSession:
    """Routes are keyed by (METHOD, repo-relative path) or (METHOD, full URL). An unrouted call is a test failure."""

    routes: dict[tuple[str, str], Route] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def on(self, method: str, path: str, response: Route) -> FakeSession:
        self.routes[(method.upper(), path)] = response
        return self

    def request(self, method: str, url: str, **kw: Any) -> FakeResponse:
        path = url[len(PREFIX) :] if url.startswith(PREFIX) else ""
        call = Call(
            method,
            url,
            path,
            kw.get("params"),
            kw.get("json"),
            dict(kw.get("headers") or {}),
            kw.get("allow_redirects"),
        )
        self.calls.append(call)
        route = self.routes.get((method, path)) or self.routes.get((method, url))
        if route is None:
            raise AssertionError(f"unrouted call: {method} {path or url}")
        if callable(route):
            return route(call)
        if isinstance(route, list):
            return route.pop(0) if len(route) > 1 else route[0]
        return route

    def paths(self, method: str | None = None) -> list[str]:
        return [c.path for c in self.calls if method is None or c.method == method]

    def writes(self) -> list[Call]:
        return [c for c in self.calls if c.method != "GET"]


class FakeClock:
    """A clock whose sleep advances it; no real time passes in tests."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def route_failed_run(session: FakeSession) -> None:
    """Run 7001: attempt 1 failed in step 'Run canary'; attempt 2 passed."""
    session.on("GET", "/actions/runs/7001/attempts/1", FakeResponse(200, fixture("run_7001_a1_failed.json")))
    session.on("GET", "/actions/runs/7001/attempts/1/jobs", FakeResponse(200, fixture("jobs_7001_a1_failed.json")))
    session.on("GET", "/actions/runs/7001/attempts/2", FakeResponse(200, fixture("run_7001_a2_success.json")))
    session.on("GET", "/actions/runs/7001/attempts/2/jobs", FakeResponse(200, fixture("jobs_7001_a2_success.json")))
