"""GitHubActionsSource and GitHubActionsTarget for the drift-gate-playground repo only.

Mapping to the domain model:

- an **execution** is one *attempt* of a workflow run, with the id `"<run_id>.<run_attempt>"` (a re-run is a new
  attempt of the same run, so "failed then passed on retry" is two executions, the second with `refs.retry_of`);
- `Execution.pipeline` is `"<owner>/<repo>:<workflow file name>"`; `read_file(repo=...)` accepts that string, or the
  bare configured repo, and nothing else;
- the **node tree** is run -> jobs -> steps; the deepest failed leaf (a failed step, or a failed job that never
  started a step) is the failed step. Node ids are `run:<id>`, `job:<job_id>`, `step:<job_id>:<number>`;
- statuses use the synthetic adapter's lowercase strings: `success`, `failed`, `skipped`, `cancelled`, `rejected`,
  `running`, `pending` (and `approval_rejected` for a run whose jobs were never started because a deployment
  approval was rejected; UNVERIFIED against the live API, see docs/design/M8a.md).

The target does Tier 0 (re-run the workflow, or only its failed jobs) and Tier 3 (a `driftgate/` branch with a
commit built from the checked file contents, and a pull request). It has no merge operation of any kind.
"""

from __future__ import annotations

import base64
import binascii
import io
import re
import time
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from driftgate.adapters.github_client import (
    BRANCH_PREFIX,
    GitHubClient,
    GitHubError,
    require_repo,
    valid_agent_branch,
)
from driftgate.domain import (
    DryRunResult,
    Execution,
    ExecutionResult,
    ExecutionSummary,
    FileContent,
    LogChunk,
    Node,
    Remediation,
    SourceError,
    VerificationResult,
)
from driftgate.tier3 import PullRequest, PullRequestRecord, _unsafe_path, is_protected

_EXECUTION_ID = re.compile(r"(\d{1,20})\.(\d{1,4})")
_NODE_ID = re.compile(r"(run|job):(\d{1,20})|step:(\d{1,20}):(\d{1,4})")
_WORKFLOW_NAME = re.compile(r"[A-Za-z0-9._-]+")
_REF = re.compile(r"[A-Za-z0-9._/-]{1,255}")
_SHA = re.compile(r"[0-9a-f]{40}")
_MAX_PR_BODY = 60_000

RERUN_ACTIONS = {"rerun_workflow": "rerun", "rerun_failed_job": "rerun-failed-jobs"}
VERIFIED_MARKER = "driftgate:verified-by-ci"

_FAILED = {"failure", "timed_out", "startup_failure", "stale", "action_required"}


def node_status(status: str | None, conclusion: str | None) -> str:
    """GitHub (status, conclusion) -> the lowercase strings the synthetic adapter uses."""
    if status != "completed":
        return "running" if status == "in_progress" else "pending"
    if conclusion in ("success", "neutral"):
        return "success"
    if conclusion == "skipped":
        return "skipped"
    if conclusion == "cancelled":
        return "cancelled"
    if conclusion in _FAILED:
        return "failed"
    return "failed" if conclusion else "success"


def workflow_name(run: dict[str, Any]) -> str:
    """File name of the workflow ('flaky.yml'), from run.path (which may carry an '@ref' suffix)."""
    path = str(run.get("path") or "").split("@")[0]
    name = path.rsplit("/", 1)[-1]
    return name if _WORKFLOW_NAME.fullmatch(name) else "workflow"


def split_execution_id(execution_id: str) -> tuple[int, int]:
    m = _EXECUTION_ID.fullmatch(execution_id or "")
    if not m:
        raise SourceError("execution ids look like '<run_id>.<attempt>'")
    return int(m.group(1)), int(m.group(2))


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _github_time(value: Any) -> str:
    dt = _parse_time(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def decode_log(data: bytes) -> str:
    """Job logs arrive as plain text; run logs arrive as a zip of per-step files. Handle both."""
    if data[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = sorted(n for n in zf.namelist() if not n.endswith("/"))
            return "\n".join(zf.read(n).decode("utf-8", errors="replace") for n in names)
    return data.decode("utf-8", errors="replace")


class GitHubActionsSource:
    """ExecutionSource over the GitHub Actions REST API for the configured playground repo."""

    def __init__(self, client: GitHubClient, *, max_pages: int = 2, per_page: int = 50, max_attempt_lookups: int = 20):
        self._c = client
        self._max_pages = max_pages
        self._per_page = per_page
        self._max_attempt_lookups = max_attempt_lookups
        self._cache: dict[str, tuple[Execution, dict[int, dict[str, Any]]]] = {}

    # -- mapping ---------------------------------------------------------------------------------
    def _refs(self, run: dict[str, Any]) -> dict[str, str]:
        attempt = int(run.get("run_attempt") or 1)
        sha = str(run.get("head_sha") or "")
        refs = {
            "connector": "none",
            "template": f"{workflow_name(run)}@{sha}",
            "runner_pool": "none",
            "infra_def": "none",
            "commit": sha,
            "branch": str(run.get("head_branch") or ""),
            "run_attempt": str(attempt),
            "event": str(run.get("event") or ""),
        }
        if attempt > 1:
            refs["retry_of"] = f"{run['id']}.{attempt - 1}"
        return refs

    def _pipeline(self, run: dict[str, Any]) -> str:
        return f"{self._c.repo}:{workflow_name(run)}"

    @staticmethod
    def _rejected_before_start(jobs: list[dict[str, Any]]) -> bool:
        """UNVERIFIED heuristic: every job failed without ever getting a runner or running a step."""
        return bool(jobs) and all(
            j.get("conclusion") == "failure" and not j.get("steps") and not j.get("runner_id") for j in jobs
        )

    def _build(self, run: dict[str, Any], jobs: list[dict[str, Any]]) -> Execution:
        rid, attempt = int(run["id"]), int(run.get("run_attempt") or 1)
        rejected = self._rejected_before_start(jobs)
        job_nodes: list[Node] = []
        for j in jobs:
            jid = f"job:{j['id']}"
            steps = [
                Node(
                    f"step:{j['id']}:{s['number']}",
                    str(s["name"]),
                    node_status(s.get("status"), s.get("conclusion")),
                    parent_id=jid,
                )
                for s in j.get("steps") or []
            ]
            status = "rejected" if rejected else node_status(j.get("status"), j.get("conclusion"))
            job_nodes.append(Node(jid, str(j["name"]), status, f"run:{rid}", steps))
        status = node_status(run.get("status"), run.get("conclusion"))
        if rejected and status == "failed":
            status = "approval_rejected"
        root = Node(f"run:{rid}", str(run.get("name") or workflow_name(run)), status, None, job_nodes)
        started = str(run.get("run_started_at") or run.get("created_at") or "")
        finished = str(run["updated_at"]) if run.get("status") == "completed" and run.get("updated_at") else None
        return Execution(
            execution_id=f"{rid}.{attempt}",
            pipeline=self._pipeline(run),
            status=status,
            started_at=started,
            finished_at=finished,
            root=root,
            refs=self._refs(run),
        )

    def _load(self, execution_id: str) -> tuple[Execution, dict[int, dict[str, Any]]]:
        rid, attempt = split_execution_id(execution_id)
        key = f"{rid}.{attempt}"
        if key in self._cache:
            return self._cache[key]
        try:
            run = self._c.get_json(f"/actions/runs/{rid}/attempts/{attempt}")
            jobs = self._c.get_json(f"/actions/runs/{rid}/attempts/{attempt}/jobs", params={"per_page": 100})["jobs"]
        except GitHubError as e:
            if e.status == 404:
                raise SourceError(f"unknown execution: {key}") from None
            raise SourceError(str(e)) from None
        loaded = (self._build(run, jobs), {int(j["id"]): j for j in jobs})
        if run.get("status") == "completed":  # a finished attempt never changes
            self._cache[key] = loaded
        return loaded

    # -- ExecutionSource ---------------------------------------------------------------------------
    def get_execution(self, execution_id: str) -> Execution:
        return self._load(execution_id)[0]

    def get_failed_leaf_nodes(self, execution_id: str) -> list[Node]:
        root = self.get_execution(execution_id).root
        found: list[Node] = []

        def walk(n: Node) -> None:
            if not n.children and n.status == "failed":
                found.append(n)
            for c in n.children:
                walk(c)

        if root is not None:
            walk(root)
        return found

    def get_step_logs(self, execution_id: str, node_id: str, budget: int) -> LogChunk:
        """The log of the job that contains the node. GitHub serves logs per job, so a step's log is its job's log.

        A log over `budget` keeps its tail (the failure is at the end), with `truncated=True`.
        """
        _, jobs = self._load(execution_id)
        m = _NODE_ID.fullmatch(node_id or "")
        if not m:
            raise SourceError("bad node id")
        job_id = int(m.group(2) or m.group(3))
        if m.group(1) == "run" or job_id not in jobs:
            raise SourceError(f"no log for {execution_id}/{node_id}")
        path = f"/actions/jobs/{job_id}/logs"
        resp = self._c.request("GET", path)
        try:
            self._c.raise_for(resp, "GET", path)
        except GitHubError as e:
            raise SourceError(str(e)) from None
        text = decode_log(resp.content)
        if len(text) > budget:
            return LogChunk(execution_id, node_id, text[-budget:], True)
        return LogChunk(execution_id, node_id, text, False)

    def list_executions(self, window: Any, filter: Any) -> list[ExecutionSummary]:
        """Runs (and their earlier attempts) in a window. Filter keys: status, pipeline, or any refs key."""
        flt: dict[str, Any] = dict(filter or {})
        params: dict[str, Any] = {"per_page": self._per_page}
        if window:
            params["created"] = f"{_github_time(window[0])}..{_github_time(window[1])}"
        if flt.get("status") in ("success", "failed"):
            params["status"] = "success" if flt["status"] == "success" else "failure"
        runs: list[dict[str, Any]] = []
        for page in range(1, self._max_pages + 1):
            body = self._c.get_json("/actions/runs", params={**params, "page": page})
            batch = body.get("workflow_runs", [])
            runs.extend(batch)
            if len(batch) < self._per_page:
                break
        lookups = 0
        out: list[ExecutionSummary] = []
        for run in runs:
            attempts = [run]
            latest = int(run.get("run_attempt") or 1)
            for n in range(1, latest):
                if lookups >= self._max_attempt_lookups:
                    break
                lookups += 1
                try:
                    attempts.append(self._c.get_json(f"/actions/runs/{run['id']}/attempts/{n}"))
                except GitHubError:
                    continue
            for r in attempts:
                summary = ExecutionSummary(
                    f"{r['id']}.{int(r.get('run_attempt') or 1)}",
                    self._pipeline(r),
                    node_status(r.get("status"), r.get("conclusion")),
                    str(r.get("run_started_at") or r.get("created_at") or ""),
                    self._refs(r),
                )
                if window:
                    t = _parse_time(summary.started_at)
                    if t < _parse_time(_github_time(window[0])) or t > _parse_time(_github_time(window[1])):
                        continue
                if all(
                    (getattr(summary, k) if k in ("status", "pipeline") else summary.refs.get(k)) == v
                    for k, v in flt.items()
                ):
                    out.append(summary)
        return sorted(out, key=lambda s: (s.started_at, s.execution_id))

    def read_file(self, repo: str, path: str, ref: str, max_bytes: int) -> FileContent:
        """A file at a ref, size-capped. `repo` must be the configured repo (optionally `<repo>:<workflow>`)."""
        require_repo(self._c.repo, str(repo).partition(":")[0])
        if not _REF.fullmatch(ref or "") or ".." in ref:
            raise SourceError("bad ref")
        if _unsafe_path(path) or "?" in path or "#" in path:
            raise SourceError("bad path")
        api_path = f"/contents/{quote(path, safe='/')}"
        resp = self._c.request("GET", api_path, params={"ref": ref}, accept="application/vnd.github.raw+json")
        if resp.status_code == 404:
            raise SourceError(f"no such file: {path}@{ref}")
        try:
            self._c.raise_for(resp, "GET", api_path)
        except GitHubError as e:
            raise SourceError(str(e)) from None
        data = resp.content
        text = data[:max_bytes].decode("utf-8", errors="replace")
        return FileContent(self._c.repo, path, ref, text, len(data) > max_bytes)

    # -- conveniences for harnesses (not part of the protocol) ------------------------------------------------
    def latest_execution_id(self, run_id: int) -> str:
        run = self._c.get_json(f"/actions/runs/{int(run_id)}")
        return f"{run['id']}.{int(run.get('run_attempt') or 1)}"


class GitHubTargetError(ValueError):
    """The target refused or could not complete a request. The orchestrator turns it into an escalation."""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class GitHubActionsTarget:
    """RemediationTarget for the playground: Tier 0 re-runs and Tier 3 pull requests. No merge operation exists."""

    real_pull_requests = True

    def __init__(
        self,
        client: GitHubClient,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        verify_timeout_s: float = 900.0,
        poll_interval_s: float = 15.0,
    ) -> None:
        self._c = client
        self._clock = clock
        self._sleep = sleep
        self._timeout = verify_timeout_s
        self._poll = poll_interval_s

    # -- shared ------------------------------------------------------------------------------------
    def _execution(self, action: Remediation) -> tuple[int, int]:
        declared = action.dry_run.get("repo")
        if declared is not None:
            require_repo(self._c.repo, str(declared).partition(":")[0])
        return split_execution_id(str(action.dry_run.get("execution_id", "")))

    @staticmethod
    def _refuse(action: Remediation, tiers: tuple[int, ...]) -> str:
        if action.gate_decision != "allowed":
            return f"refused: gate decision is {action.gate_decision!r}"
        if action.tier not in tiers:
            return f"refused: Tier {action.tier} is not supported on GitHub Actions here"
        return ""

    def _run(self, run_id: int) -> dict[str, Any]:
        return self._c.get_json(f"/actions/runs/{run_id}")  # type: ignore[no-any-return]

    # -- Tier 0 ------------------------------------------------------------------------------------
    def dry_run(self, action: Remediation) -> DryRunResult:
        bad = self._refuse(action, (0, 3))
        if bad:
            return DryRunResult(False, bad, {})
        if action.tier == 3:
            return DryRunResult(True, "would open a pull request from a driftgate/ branch", {"tier": 3})
        endpoint = RERUN_ACTIONS.get(action.action)
        if endpoint is None:
            return DryRunResult(False, f"unsupported Tier 0 action {action.action!r}", {})
        try:
            rid, attempt = self._execution(action)
            run = self._run(rid)
        except (GitHubError, SourceError) as e:
            return DryRunResult(False, str(e), {})
        details = {"run_id": rid, "attempt": attempt, "endpoint": endpoint, "run_attempt_now": run.get("run_attempt")}
        if int(run.get("run_attempt") or 1) > attempt:
            return DryRunResult(True, f"already re-run (attempt {run.get('run_attempt')}); nothing to do", details)
        if run.get("status") != "completed":
            return DryRunResult(False, "the run has not finished; it cannot be re-run yet", details)
        if node_status("completed", run.get("conclusion")) == "success":
            return DryRunResult(False, "the run did not fail; nothing to re-run", details)
        return DryRunResult(True, f"would POST /actions/runs/{rid}/{endpoint}", details)

    def execute(self, action: Remediation) -> ExecutionResult:
        bad = self._refuse(action, (0,))
        if bad:
            return ExecutionResult(False, bad, {})
        endpoint = RERUN_ACTIONS.get(action.action)
        if endpoint is None:
            return ExecutionResult(False, f"unsupported Tier 0 action {action.action!r}", {})
        try:
            rid, attempt = self._execution(action)
            run = self._run(rid)
            now = int(run.get("run_attempt") or 1)
            details: dict[str, Any] = {"run_id": rid, "attempt": attempt, "endpoint": endpoint}
            if now > attempt:  # idempotent: someone (or an earlier call) already re-ran it
                return ExecutionResult(True, f"already re-run (attempt {now})", {**details, "already": True})
            if run.get("status") != "completed":
                return ExecutionResult(False, "the run has not finished; it cannot be re-run yet", details)
            if node_status("completed", run.get("conclusion")) == "success":
                return ExecutionResult(False, "the run did not fail; nothing to re-run", details)
            path = f"/actions/runs/{rid}/{endpoint}"
            resp = self._c.request("POST", path)
            self._c.raise_for(resp, "POST", path, ok=(201, 200, 204))
        except (GitHubError, SourceError) as e:
            return ExecutionResult(False, str(e), {})
        return ExecutionResult(True, f"requested {endpoint} of run {rid}", details)

    # -- pull request (Tier 3) -----------------------------------------------------------------------
    def open_pull_request(self, pr: PullRequest) -> PullRequestRecord:
        """Branch from the failing commit, commit the CHECKED contents, open the PR. Never merges."""
        if not valid_agent_branch(pr.branch):
            raise GitHubTargetError(
                f"refusing branch {pr.branch[:60]!r}: agent branches must be under {BRANCH_PREFIX!r}"
            )
        if not pr.base or not _REF.fullmatch(pr.base) or ".." in pr.base or pr.base == pr.branch:
            raise GitHubTargetError("refusing the pull request: bad base branch")
        if not pr.files:
            raise GitHubTargetError("refusing the pull request: no checked file contents to commit")
        paths = [p for p, _ in pr.files]
        if len(set(paths)) != len(paths) or set(paths) != set(pr.paths):
            raise GitHubTargetError("refusing the pull request: committed files differ from the declared paths")
        for p in paths:
            if _unsafe_path(p) or is_protected(p) or "?" in p or "#" in p:
                raise GitHubTargetError(f"refusing path {p[:60]!r}: not a safe repository path")
        c = self._c
        try:
            base_sha = pr.base_sha if _SHA.fullmatch(pr.base_sha) else self._ref_sha(pr.base)
            if self._ref_sha(pr.branch, missing_ok=True) is not None:
                raise GitHubTargetError(f"refusing: branch {pr.branch} already exists (no force, no overwrite)")
            # 1. additive: the branch
            resp = c.request("POST", "/git/refs", json={"ref": f"refs/heads/{pr.branch}", "sha": base_sha})
            c.raise_for(resp, "POST", "/git/refs", ok=(201,))
            # 2. read what is there, then commit growing files before shrinking ones (invariant 13)
            existing = {p: self._blob(p, pr.branch) for p in paths}

            def shrinks(item: tuple[str, str]) -> bool:
                before = existing[item[0]]
                return before is not None and len(item[1]) < len(before[1])

            for path, content in sorted(pr.files, key=shrinks):  # stable: shrinking files last
                api = f"/contents/{quote(path, safe='/')}"
                body: dict[str, Any] = {
                    "message": f"DriftGate: update {path}",
                    "content": _b64(content),
                    "branch": pr.branch,
                }
                before = existing[path]
                if before is not None:
                    body["sha"] = before[0]
                resp = c.request("PUT", api, json=body)
                c.raise_for(resp, "PUT", api, ok=(200, 201))
            # 3. the pull request
            pr_body = {
                "title": pr.title[:200],
                "head": pr.branch,
                "base": pr.base,
                "body": pr.body[:_MAX_PR_BODY],
                "draft": False,
            }
            resp = c.request("POST", "/pulls", json=pr_body)
            c.raise_for(resp, "POST", "/pulls", ok=(201,))
            data = resp.json()
        except GitHubError as e:
            raise GitHubTargetError(str(e)) from None
        return PullRequestRecord(int(data["number"]), pr.branch, str(data["html_url"]), merged=False, simulated=False)

    def _ref_sha(self, branch: str, missing_ok: bool = False) -> str | None:
        path = f"/git/ref/heads/{quote(branch, safe='/')}"
        resp = self._c.request("GET", path)
        if resp.status_code == 404 and missing_ok:
            return None
        self._c.raise_for(resp, "GET", path)
        return str(resp.json()["object"]["sha"])

    def _blob(self, path: str, ref: str) -> tuple[str, str] | None:
        """(blob sha, current text) of a file on a branch, or None when it does not exist."""
        api = f"/contents/{quote(path, safe='/')}"
        resp = self._c.request("GET", api, params={"ref": ref})
        if resp.status_code == 404:
            return None
        self._c.raise_for(resp, "GET", api)
        data = resp.json()
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            raise GitHubError(f"GET {api[:80]}: unexpected contents shape")
        try:
            text = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except (binascii.Error, KeyError):
            raise GitHubError(f"GET {api[:80]}: undecodable contents") from None
        return str(data["sha"]), text

    # -- verification ---------------------------------------------------------------------------------
    def verify(self, action: Remediation) -> VerificationResult:
        try:
            if action.tier == 3:
                return self._verify_pr(action)
            if action.tier == 0:
                return self._verify_rerun(action)
        except (GitHubError, SourceError, ValueError) as e:
            return VerificationResult(False, str(e), {})
        return VerificationResult(False, f"Tier {action.tier} cannot be verified on GitHub Actions here", {})

    def _poll_until(self, probe: Callable[[], VerificationResult | None]) -> VerificationResult:
        deadline = self._clock() + self._timeout
        while True:
            done = probe()
            if done is not None:
                return done
            if self._clock() >= deadline:
                return VerificationResult(False, "timed out waiting for CI", {"pending": True, "timed_out": True})
            self._sleep(self._poll)

    def _verify_rerun(self, action: Remediation) -> VerificationResult:
        rid, attempt = self._execution(action)

        def probe() -> VerificationResult | None:
            run = self._run(rid)
            now = int(run.get("run_attempt") or 1)
            if now <= attempt or run.get("status") != "completed":
                return None
            ok = node_status("completed", run.get("conclusion")) == "success"
            return VerificationResult(
                ok,
                f"re-run attempt {now} {'passed' if ok else 'failed'}",
                {"run_id": rid, "attempt": now, "conclusion": run.get("conclusion"), "url": run.get("html_url")},
            )

        return self._poll_until(probe)

    def _verify_pr(self, action: Remediation) -> VerificationResult:
        branch = str(action.dry_run.get("branch", ""))
        if not valid_agent_branch(branch):
            return VerificationResult(False, "no agent branch to verify", {})
        sha = self._ref_sha(branch)
        dispatched = False

        def probe() -> VerificationResult | None:
            nonlocal dispatched
            body = self._c.get_json("/actions/runs", params={"head_sha": sha, "per_page": 50})
            runs = [r for r in body.get("workflow_runs", []) if r.get("head_sha") == sha]
            if not runs and not dispatched:
                dispatched = self._dispatch_ci(action, branch)
            if not runs or any(r.get("status") != "completed" for r in runs):
                return None
            ok = all(node_status("completed", r.get("conclusion")) == "success" for r in runs)
            details: dict[str, Any] = {"sha": sha, "runs": [r.get("html_url") for r in runs], "branch": branch}
            if ok:
                details["comment"] = self._comment_verified(action, str(sha), details["runs"])
            return VerificationResult(ok, "CI on the PR branch " + ("passed" if ok else "failed"), details)

        return self._poll_until(probe)

    def _dispatch_ci(self, action: Remediation, branch: str) -> bool:
        """Workflows here are `workflow_dispatch`-only, so CI on the PR branch is started by dispatching it."""
        rid, _ = self._execution(action)
        wf = workflow_name(self._run(rid))
        path = f"/actions/workflows/{wf}/dispatches"
        resp = self._c.request("POST", path, json={"ref": branch})
        self._c.raise_for(resp, "POST", path, ok=(204, 200))
        return True

    def _pr_number(self, action: Remediation, branch: str) -> int | None:
        pull = action.dry_run.get("pull_request")
        if isinstance(pull, dict) and isinstance(pull.get("number"), int):
            return int(pull["number"])
        owner = self._c.repo.split("/")[0]
        found = self._c.get_json("/pulls", params={"head": f"{owner}:{branch}", "state": "open"})
        return int(found[0]["number"]) if found else None

    def _comment_verified(self, action: Remediation, sha: str, urls: list[Any]) -> str:
        number = self._pr_number(action, str(action.dry_run.get("branch", "")))
        if number is None:
            return "no pull request found"
        marker = f"<!-- {VERIFIED_MARKER}:{sha} -->"
        existing = self._c.get_json(f"/issues/{number}/comments", params={"per_page": 100})
        if any(marker in str(c.get("body", "")) for c in existing):
            return "already commented"
        lines = "\n".join(f"- {u}" for u in urls)
        body = f"{marker}\nVerified by CI: every workflow run on `{sha[:7]}` passed.\n\n{lines}\n"
        path = f"/issues/{number}/comments"
        self._c.raise_for(self._c.request("POST", path, json={"body": body}), "POST", path, ok=(201,))
        return "commented"


__all__ = [
    "GitHubActionsSource",
    "GitHubActionsTarget",
    "GitHubTargetError",
    "RERUN_ACTIONS",
    "decode_log",
    "node_status",
    "split_execution_id",
    "workflow_name",
]
