"""Read-only GitHub client for `holdout-fetch` (MVP 2, N1). NOT the playground adapter and it shares none of its code.

What it can do, and nothing else:
- GET only. The transport interface has a single method, `get`; there is no way to express another verb.
- Three API paths on `api.github.com`, built from validated parts and re-checked against an allow-list before every
  request: run metadata, the jobs list of a run, and one job's logs.
- The logs endpoint answers with one redirect to a log blob. That single hop is followed only to an https URL whose
  host matches `BLOB_HOST_PATTERNS`, and WITHOUT the Authorization header (the blob URL is itself a signed secret and
  is never printed or stored). A second redirect, or any redirect from the other two paths, is refused.
- Bounded response size and a timeout on every request. A rate-limit answer stops the run with a clear message; there
  is no retry and no sleeping.

The token is passed in by the caller (the CLI reads `HOLDOUT_FETCH_TOKEN`, see `holdout_fetch`). It is placed in the
Authorization header only, and every exception message here is built from fixed text plus a status code or host name,
so the token cannot appear in one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

API_HOST = "api.github.com"
USER_AGENT = "driftgate-holdout-fetch"
API_VERSION = "2022-11-28"
TIMEOUT_S = 30.0
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_LOG_BYTES = 32 * 1024 * 1024
JOBS_PER_PAGE = 100
MAX_JOB_PAGES = 5

# Hosts GitHub redirects job-log downloads to. Both historical and current forms are listed explicitly; anything else
# is refused, so a changed GitHub deployment fails loudly (add the new host here after looking at it) rather than
# sending a request somewhere unexpected. The Authorization header is never sent to any of them.
#   pipelines.actions.githubusercontent.com / pipelinesghubeus<N>.actions.githubusercontent.com : Actions log service
#   productionresultssa<N>.blob.core.windows.net : Azure blob storage account of the newer results service
BLOB_HOST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^pipelines\.actions\.githubusercontent\.com$", re.ASCII),
    re.compile(r"^pipelinesghub[a-z]{2,4}[0-9]{1,3}\.actions\.githubusercontent\.com$", re.ASCII),
    re.compile(r"^productionresultssa[0-9]{1,3}\.blob\.core\.windows\.net$", re.ASCII),
)

OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$", re.ASCII)
REPO_RE = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9_.-]{1,100}$", re.ASCII)
MAX_ID = 2**63 - 1

_P = r"/repos/[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.{1,2}/)[A-Za-z0-9_.-]{1,100}/actions"
ALLOWED_API_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^{_P}/runs/[1-9][0-9]{{0,18}}$", re.ASCII),
    re.compile(rf"^{_P}/runs/[1-9][0-9]{{0,18}}/jobs$", re.ASCII),
    re.compile(rf"^{_P}/jobs/[1-9][0-9]{{0,18}}/logs$", re.ASCII),
)
ALLOWED_API_QUERY = re.compile(
    r"^(?:per_page=100(?:&page=[1-9])?|filter=latest&per_page=100(?:&page=[1-9])?)?$", re.ASCII
)


class FetchError(Exception):
    """A fetch problem with a message that is safe to print (no token, no signed URL, no response body)."""


class RateLimited(FetchError):
    """GitHub says the rate limit is used up. The run stops; nothing sleeps or retries."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]  # keys lower-cased by the transport
    body: bytes


class Transport(Protocol):
    """GET only. `max_bytes` bounds the body; a larger body must raise `FetchError` (or be returned truncated+1)."""

    def get(self, url: str, headers: Mapping[str, str], *, timeout: float, max_bytes: int) -> HttpResponse: ...


class RequestsTransport:
    """The real transport. `requests` is imported lazily so importing this module never needs it; never follows
    redirects (the client decides what to follow), ignores host proxy and netrc settings, streams the body so the size
    cap is enforced while reading, makes exactly one attempt (no retry) and refuses credentials for any host other than
    the API host. `session_factory` lets tests inject a fake session; the default is `requests.Session`."""

    def __init__(self, session_factory: Callable[[], Any] | None = None) -> None:
        self._session_factory = session_factory

    def get(self, url: str, headers: Mapping[str, str], *, timeout: float, max_bytes: int) -> HttpResponse:
        import requests

        if any(k.lower() == "authorization" for k in headers) and urlsplit(url).netloc != API_HOST:
            raise FetchError("refusing to send credentials to a host other than the GitHub API")
        factory = self._session_factory or requests.Session
        try:
            with factory() as session:
                session.trust_env = False  # no proxy / netrc credentials picked up from the host
                resp = session.get(url, headers=dict(headers), timeout=timeout, allow_redirects=False, stream=True)
                try:
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_content(chunk_size=65536):
                        total += len(chunk)
                        if total > max_bytes:
                            raise FetchError(f"response larger than the {max_bytes} byte limit")
                        chunks.append(chunk)
                    hdrs = {k.lower(): v for k, v in resp.headers.items()}
                    return HttpResponse(resp.status_code, hdrs, b"".join(chunks))
                finally:
                    resp.close()
        except FetchError:
            raise
        except requests.RequestException as exc:
            raise FetchError(f"transport error ({type(exc).__name__})") from None


def valid_owner(owner: object) -> str:
    if not isinstance(owner, str) or not OWNER_RE.fullmatch(owner):
        raise FetchError("invalid repository owner")
    return owner


def valid_repo_name(repo: object) -> str:
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        raise FetchError("invalid repository name")
    return repo


def split_repo(full: object) -> tuple[str, str]:
    if not isinstance(full, str) or full.count("/") != 1:
        raise FetchError("repo must be owner/name")
    owner, name = full.split("/")
    return valid_owner(owner), valid_repo_name(name)


def valid_id(value: object, what: str) -> int:
    if type(value) is not int or not 0 < value <= MAX_ID:
        raise FetchError(f"invalid {what}")
    return value


def blob_host_allowed(url: str) -> bool:
    """https, an allow-listed host, no userinfo, default port."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (
        parts.scheme == "https"
        and parts.username is None
        and parts.password is None
        and port in (None, 443)
        and any(p.fullmatch(host) for p in BLOB_HOST_PATTERNS)
    )


def _check_api_url(url: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != API_HOST  # exact: no userinfo, no port, no other host
        or parts.fragment
        or not any(p.fullmatch(parts.path) for p in ALLOWED_API_PATHS)
        or not ALLOWED_API_QUERY.fullmatch(parts.query)
    ):
        raise FetchError("refusing a request outside the three allowed GitHub API paths")


def _reset_text(headers: Mapping[str, str]) -> str:
    parts = []
    reset = headers.get("x-ratelimit-reset", "")
    if reset.isdigit():
        parts.append("resets at " + datetime.fromtimestamp(int(reset), UTC).strftime("%Y-%m-%d %H:%M:%SZ"))
    retry = headers.get("retry-after", "")
    if retry.isdigit():
        parts.append(f"retry after {retry}s")
    return ("; " + "; ".join(parts)) if parts else ""


def _is_rate_limited(resp: HttpResponse) -> bool:
    if resp.status == 429:
        return True
    if resp.status != 403:
        return False
    return (
        resp.headers.get("x-ratelimit-remaining") == "0"
        or "retry-after" in resp.headers
        or b"rate limit" in resp.body[:2000].lower()
    )


class ReadOnlyGitHub:
    """Three read calls over an injected GET-only transport."""

    def __init__(self, transport: Transport, token: str) -> None:
        if not isinstance(token, str) or not token or re.search(r"\s", token) or not token.isascii():
            raise FetchError("the fetch token is missing or malformed")
        self._transport = transport
        self._token = token
        self.requests_made = 0

    def _headers(self, *, auth: bool, accept: str) -> dict[str, str]:
        h = {"Accept": accept, "User-Agent": USER_AGENT, "X-GitHub-Api-Version": API_VERSION}
        if auth:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _send(self, url: str, *, auth: bool, accept: str, max_bytes: int) -> HttpResponse:
        self.requests_made += 1
        resp = self._transport.get(url, self._headers(auth=auth, accept=accept), timeout=TIMEOUT_S, max_bytes=max_bytes)
        if len(resp.body) > max_bytes:
            raise FetchError(f"response larger than the {max_bytes} byte limit")
        if _is_rate_limited(resp):
            raise RateLimited(
                "GitHub rate limit reached" + _reset_text(resp.headers) + "; stopping, nothing is retried"
            )
        return resp

    def _api_json(self, path: str, query: str = "") -> Any:
        url = f"https://{API_HOST}{path}" + (f"?{query}" if query else "")
        _check_api_url(url)
        resp = self._send(url, auth=True, accept="application/vnd.github+json", max_bytes=MAX_JSON_BYTES)
        if 300 <= resp.status < 400:
            raise FetchError("unexpected redirect from the GitHub API (not followed)")
        if resp.status != 200:
            raise FetchError(f"GitHub API answered HTTP {resp.status}")
        try:
            return json.loads(resp.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise FetchError("GitHub API answered with invalid JSON") from None

    def get_run(self, repo: str, run_id: int) -> dict[str, Any]:
        owner, name = split_repo(repo)
        data = self._api_json(f"/repos/{owner}/{name}/actions/runs/{valid_id(run_id, 'run id')}")
        if not isinstance(data, dict):
            raise FetchError("unexpected run metadata shape")
        return data

    def list_jobs(self, repo: str, run_id: int) -> list[dict[str, Any]]:
        """Jobs of the run's latest attempt (the API default), up to `MAX_JOB_PAGES` pages of 100."""
        owner, name = split_repo(repo)
        rid = valid_id(run_id, "run id")
        jobs: list[dict[str, Any]] = []
        for page in range(1, MAX_JOB_PAGES + 1):
            query = f"filter=latest&per_page={JOBS_PER_PAGE}" + (f"&page={page}" if page > 1 else "")
            data = self._api_json(f"/repos/{owner}/{name}/actions/runs/{rid}/jobs", query)
            batch = data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(batch, list) or not all(isinstance(j, dict) for j in batch):
                raise FetchError("unexpected jobs list shape")
            jobs += batch
            total = data.get("total_count")
            if len(batch) < JOBS_PER_PAGE or (isinstance(total, int) and len(jobs) >= total):
                return jobs
        raise FetchError(f"run has more than {MAX_JOB_PAGES * JOBS_PER_PAGE} jobs; not supported")

    def get_job_log(self, repo: str, job_id: int) -> bytes:
        """The raw job log. One redirect to an allow-listed blob host is followed, without credentials."""
        owner, name = split_repo(repo)
        url = f"https://{API_HOST}/repos/{owner}/{name}/actions/jobs/{valid_id(job_id, 'job id')}/logs"
        _check_api_url(url)
        resp = self._send(url, auth=True, accept="application/vnd.github+json", max_bytes=MAX_LOG_BYTES)
        if resp.status == 200:
            return resp.body  # served inline without a redirect
        if resp.status not in (301, 302, 303, 307, 308):
            raise FetchError(f"GitHub API answered HTTP {resp.status} for the job log")
        location = resp.headers.get("location", "")
        if not blob_host_allowed(location):
            host = (urlsplit(location).hostname or "?") if location else "none"
            raise FetchError(f"refusing the log redirect: host '{host[:80]}' is not an allowed log host")
        blob = self._send(location, auth=False, accept="*/*", max_bytes=MAX_LOG_BYTES)
        if blob.status != 200:
            raise FetchError(f"log blob answered HTTP {blob.status} (logs expire after 90 days)")
        return blob.body
