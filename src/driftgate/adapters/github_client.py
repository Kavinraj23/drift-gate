"""A guarded GitHub REST client for the playground repo only.

Everything GitHub-facing goes through `GitHubClient.request`, which enforces, in code and independent of the
callers above it:

- **one repository** (invariant 12): the client builds every URL as `{api}/repos/{configured repo}{path}`; callers
  pass repo-relative paths only, and `require_repo` refuses any other repo name (exact match, no normalisation);
- **an allow-list of writes**: anything that is not a GET must match `WRITE_RULES` (re-run, branch/ref creation under
  `driftgate/`, contents commits to `driftgate/` branches, pull request creation, comments, workflow dispatch on a
  `driftgate/` ref). There is no DELETE, no force push, no merge, no auto-merge: any path that mentions a merge is
  refused outright (invariant 4);
- **rate-limit awareness**: remaining/reset/retry-after headers are honoured with an injected clock and sleep;
- **a write budget** per client, so a runaway loop cannot hammer the API;
- **the token never leaves the Authorization header**: it is excluded from `repr`, and scrubbed from every message.

The session is injected (a `requests.Session`-like object with `request(...)`), so tests use stubs and nothing here
touches the network by itself.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from driftgate.domain import SourceError

API_URL = "https://api.github.com"
BRANCH_PREFIX = "driftgate/"
PLAYGROUND_REPO_ENV = "DRIFTGATE_PLAYGROUND_REPO"
TOKEN_ENV = "GITHUB_TOKEN"

_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+")


class GitHubError(Exception):
    """A GitHub call failed. Messages carry the method, repo-relative path and status; never headers or the token."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RepoRefused(SourceError, ValueError):
    """The requested repository is not the configured playground (invariant 12)."""


class WriteRefused(GitHubError, ValueError):
    """The request is not on the allow-list of writes (this also covers every merge-shaped request)."""


class RateLimited(GitHubError):
    """The API asked us to wait longer than `max_wait_s`."""


@dataclass(frozen=True)
class GitHubConfig:
    repo: str  # "owner/name" of the playground; the only repo this adapter will ever touch
    token: str = field(repr=False)
    api_url: str = API_URL
    timeout_s: float = 30.0
    max_wait_s: float = 900.0  # longest single rate-limit wait before giving up
    max_retries: int = 2
    max_writes: int = 60  # write calls per client instance

    def __post_init__(self) -> None:
        if not _REPO.fullmatch(self.repo or ""):
            raise ValueError("repo must look like 'owner/name'")
        if not self.token:
            raise ValueError("a GitHub token is required")
        if not self.api_url.startswith("https://"):
            raise ValueError("api_url must be https")


def load_github_config(env: Mapping[str, str] | None = None, dotenv_path: str | None = None) -> GitHubConfig:
    """Build the config from the environment (and the gitignored `.env` when `env` is not given).

    Not used by tests. The token is read only here and handed straight to the config; it is never printed or logged.
    """
    import os

    if env is None:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path)
        env = os.environ
    repo, token = env.get(PLAYGROUND_REPO_ENV, ""), env.get(TOKEN_ENV, "")
    if not repo or not token:
        raise ValueError(f"set {PLAYGROUND_REPO_ENV} and {TOKEN_ENV} in the environment or .env")
    return GitHubConfig(repo=repo, token=token)


class Response(Protocol):
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    text: str

    def json(self) -> Any: ...


class Session(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Response: ...


# (method, path regex, body check). The body check receives the JSON body and returns True when acceptable.
def _agent_ref(body: Mapping[str, Any]) -> bool:
    return str(body.get("ref", "")).startswith(f"refs/heads/{BRANCH_PREFIX}")


def _agent_branch(body: Mapping[str, Any]) -> bool:
    return valid_agent_branch(str(body.get("branch", "")))


def _agent_head(body: Mapping[str, Any]) -> bool:
    return valid_agent_branch(str(body.get("head", "")))


def _dispatch_ref(body: Mapping[str, Any]) -> bool:
    return valid_agent_branch(str(body.get("ref", "")))


def _any(_: Mapping[str, Any]) -> bool:
    return True


WRITE_RULES: tuple[tuple[str, re.Pattern[str], Callable[[Mapping[str, Any]], bool]], ...] = (
    ("POST", re.compile(r"/actions/runs/\d+/rerun"), _any),
    ("POST", re.compile(r"/actions/runs/\d+/rerun-failed-jobs"), _any),
    ("POST", re.compile(r"/actions/workflows/[A-Za-z0-9._-]+\.ya?ml/dispatches"), _dispatch_ref),
    ("POST", re.compile(r"/git/refs"), _agent_ref),
    ("PUT", re.compile(r"/contents/[^?#]+"), _agent_branch),
    ("POST", re.compile(r"/pulls"), _agent_head),
    ("POST", re.compile(r"/issues/\d+/comments"), _any),
)
_MERGEISH = re.compile(r"(^|/)(auto[-_]?merge|merges?)($|/)|merge", re.IGNORECASE)
_BRANCH_OK = re.compile(r"driftgate/[A-Za-z0-9._/-]+")


def valid_agent_branch(branch: str) -> bool:
    """A branch the agent may write: under `driftgate/`, ordinary characters, no tricks."""
    return bool(
        _BRANCH_OK.fullmatch(branch)
        and ".." not in branch
        and "//" not in branch
        and not branch.endswith(("/", ".", ".lock"))
    )


def require_repo(configured: str, repo: str) -> None:
    """Exact match only: case variants, suffixes, URLs and paths are all refused."""
    if repo != configured:
        shown = repr(repo[:60])
        raise RepoRefused(f"refusing repo {shown}: this adapter only works on the configured playground repo")


class GitHubClient:
    def __init__(
        self,
        config: GitHubConfig,
        session: Session,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._session = session
        self._clock = clock
        self._sleep = sleep
        self._remaining: int | None = None
        self._reset_at: float | None = None
        self.writes = 0
        self.waited_s = 0.0

    @property
    def repo(self) -> str:
        return self._config.repo

    # -- helpers ---------------------------------------------------------------------------------
    def _scrub(self, text: str) -> str:
        return text.replace(self._config.token, "***")

    def _headers(self, accept: str | None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.token}",
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "driftgate",
        }

    def _check_write(self, method: str, path: str, body: Mapping[str, Any]) -> None:
        for m, pattern, ok in WRITE_RULES:
            if m == method and pattern.fullmatch(path) and ok(body):
                return
        raise WriteRefused(f"refusing {method} {path[:80]}: not an allowed write", None)

    def _note_limits(self, headers: Mapping[str, str]) -> None:
        low = {k.lower(): v for k, v in headers.items()}
        try:
            if "x-ratelimit-remaining" in low:
                self._remaining = int(low["x-ratelimit-remaining"])
            if "x-ratelimit-reset" in low:
                self._reset_at = float(low["x-ratelimit-reset"])
        except ValueError:
            pass

    def _wait(self, seconds: float, method: str, path: str) -> None:
        seconds = max(seconds, 0.0)
        if seconds > self._config.max_wait_s:
            raise RateLimited(f"{method} {path[:80]}: rate limited for {int(seconds)}s, over the wait limit", 429)
        self.waited_s += seconds
        self._sleep(seconds)

    def _wait_if_exhausted(self, method: str, path: str) -> None:
        if self._remaining == 0 and self._reset_at is not None:
            wait = self._reset_at - self._clock() + 1.0
            if wait > 0:
                self._wait(wait, method, path)
            self._remaining = None

    # -- requests ----------------------------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        accept: str | None = None,
        follow_redirects: bool = True,
    ) -> Response:
        """One repo-scoped call. `path` starts with "/" and is relative to the configured repository."""
        method = method.upper()
        if not path.startswith("/") or "//" in path or ".." in path or "?" in path or "#" in path:
            raise GitHubError(f"malformed path {path[:80]!r}")
        if not path.startswith("/contents/") and _MERGEISH.search(path):
            raise WriteRefused("refusing a merge-shaped request: DriftGate never merges", None)
        if method != "GET":
            self._check_write(method, path, json or {})
            if self.writes >= self._config.max_writes:
                raise WriteRefused("write budget for this client is exhausted", None)
        url = f"{self._config.api_url}/repos/{self._config.repo}{path}"
        attempt = 0
        while True:
            self._wait_if_exhausted(method, path)
            try:
                resp = self._session.request(
                    method,
                    url,
                    headers=self._headers(accept),
                    params=dict(params) if params else None,
                    json=dict(json) if json is not None else None,
                    timeout=self._config.timeout_s,
                    allow_redirects=False,
                )
            except Exception as e:  # network-level failures: report the class only, never the message
                raise GitHubError(f"{method} {path[:80]} failed: {type(e).__name__}") from None
            if method != "GET":
                self.writes += 1
            self._note_limits(resp.headers)
            if resp.status_code in (403, 429) and attempt < self._config.max_retries:
                low = {k.lower(): v for k, v in resp.headers.items()}
                retry_after = low.get("retry-after")
                if retry_after is not None:
                    attempt += 1
                    self._wait(float(retry_after) + 1.0, method, path)
                    continue
                if self._remaining == 0:
                    attempt += 1
                    continue  # _wait_if_exhausted sleeps until reset on the next loop
            if resp.status_code in (301, 302, 303, 307, 308) and follow_redirects:
                return self.fetch_redirect(resp)
            return resp

    def fetch_redirect(self, resp: Response) -> Response:
        """Follow a redirect to a signed storage URL (job logs). The token is NOT sent to the other host."""
        low = {k.lower(): v for k, v in resp.headers.items()}
        location = low.get("location", "")
        if not location.startswith("https://"):
            raise GitHubError("redirect without an https location")
        try:
            return self._session.request(
                "GET", location, headers={"User-Agent": "driftgate"}, timeout=self._config.timeout_s
            )
        except Exception as e:
            raise GitHubError(f"redirected fetch failed: {type(e).__name__}") from None

    def get_json(self, path: str, **kw: Any) -> Any:
        resp = self.request("GET", path, **kw)
        self.raise_for(resp, "GET", path)
        return resp.json()

    def raise_for(self, resp: Response, method: str, path: str, ok: tuple[int, ...] = (200,)) -> None:
        if resp.status_code in ok:
            return
        detail = ""
        try:
            detail = str(resp.json().get("message", ""))[:120]
        except Exception:
            pass
        raise GitHubError(
            self._scrub(f"{method} {path[:80]} returned {resp.status_code} {detail}".strip()), resp.status_code
        )


__all__ = [
    "API_URL",
    "BRANCH_PREFIX",
    "GitHubClient",
    "GitHubConfig",
    "GitHubError",
    "RateLimited",
    "RepoRefused",
    "WriteRefused",
    "load_github_config",
    "require_repo",
    "valid_agent_branch",
]
