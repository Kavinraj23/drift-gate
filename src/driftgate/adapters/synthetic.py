"""Synthetic adapters: SyntheticTarget (simulated RemediationTarget) and SyntheticSource (simulated ExecutionSource)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from driftgate.tier3 import BRANCH_PREFIX, PullRequest, PullRequestRecord


@dataclass
class LockEntry:
    holder: str
    alive: bool


@dataclass
class LockTable:
    """Simulated state-lock table with holder liveness."""

    locks: dict[str, LockEntry] = field(default_factory=dict)

    def holder_status(self, lock_id: str) -> str:
        entry = self.locks.get(lock_id)
        if entry is None:
            return "none"
        return "running" if entry.alive else "dead"


@dataclass
class SimulatedOutcome:
    dry_run_ok: bool = True
    execute_ok: bool = True
    verified: bool = True


def _load_followups(data_dir: Path | str | None) -> dict[str, Any]:
    """The simulator's world response (rerun results, accepted fix files); only this adapter reads it."""
    if data_dir is None:
        return {}
    path = Path(data_dir) / "world" / "followups.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


class SyntheticTarget:
    """Outcomes are keyed by action name; unlisted actions use `default`. Records every call.

    Tier 2 `force_unlock` reads `dry_run["lock_id"]` and acts on the simulated lock table, and
    independently refuses a running holder (defense in depth behind SafetyGate).
    """

    def __init__(
        self,
        outcomes: dict[str, SimulatedOutcome] | None = None,
        default: SimulatedOutcome | None = None,
        lock_table: LockTable | None = None,
        data_dir: Path | str | None = None,
    ) -> None:
        self.outcomes = outcomes or {}
        self._data_root = Path(data_dir).resolve() if data_dir is not None else None
        self._followups = _load_followups(data_dir)
        self.rolled_back: list[Remediation] = []
        self.default = default or SimulatedOutcome()
        self.locks = lock_table or LockTable()
        self.calls: list[tuple[str, str]] = []  # (method, action)
        self.executed: list[Remediation] = []
        self.pull_requests: list[PullRequest] = []  # simulated Tier 3 pull requests; there is no merge operation

    def _outcome(self, action: Remediation) -> SimulatedOutcome:
        return self.outcomes.get(action.action, self.default)

    def _lock_id(self, action: Remediation) -> str | None:
        return action.dry_run.get("lock_id") if action.action == "force_unlock" else None

    def dry_run(self, action: Remediation) -> DryRunResult:
        self.calls.append(("dry_run", action.action))
        if action.gate_decision != "allowed":
            return DryRunResult(False, f"refused: gate decision is {action.gate_decision!r}", {})
        lock_id = self._lock_id(action)
        if lock_id is not None:
            status = self.locks.holder_status(lock_id)
            entry = self.locks.locks.get(lock_id)
            details = {"lock_id": lock_id, "holder_status": status, "holder": entry.holder if entry else None}
            ok = status == "dead" and self._outcome(action).dry_run_ok
            return DryRunResult(ok, f"holder {status}", details)
        out = self._outcome(action)
        return DryRunResult(out.dry_run_ok, f"simulated dry run of {action.action}", {"tier": action.tier})

    def execute(self, action: Remediation) -> ExecutionResult:
        self.calls.append(("execute", action.action))
        if action.gate_decision != "allowed":
            return ExecutionResult(False, f"refused: gate decision is {action.gate_decision!r}", {})
        lock_id = self._lock_id(action)
        if lock_id is not None:
            if self.locks.holder_status(lock_id) != "dead":
                return ExecutionResult(False, "refused: lock holder is not provably dead", {"lock_id": lock_id})
            if self._outcome(action).execute_ok:
                del self.locks.locks[lock_id]
                self.executed.append(action)
                return ExecutionResult(True, "lock released", {"lock_id": lock_id})
            return ExecutionResult(False, "simulated failure", {"lock_id": lock_id})
        out = self._outcome(action)
        if out.execute_ok:
            self.executed.append(action)
        return ExecutionResult(out.execute_ok, f"simulated execution of {action.action}", {"tier": action.tier})

    def open_pull_request(self, pr: PullRequest) -> PullRequestRecord:
        """Record a simulated PR. Refuses any branch outside the agent's `driftgate/` namespace (invariant 4)."""
        if not pr.branch.startswith(BRANCH_PREFIX) or pr.branch == BRANCH_PREFIX:
            raise ValueError(f"refusing branch {pr.branch!r}: agent branches must start with {BRANCH_PREFIX!r}")
        self.pull_requests.append(pr)
        number = len(self.pull_requests)
        return PullRequestRecord(number, pr.branch, f"synthetic://pull/{number}", merged=False)

    def verify(self, action: Remediation) -> VerificationResult:
        """Observe the simulated next execution. `action.dry_run["execution_id"]` names the failure it answers.

        Without a dataset (or an execution id) the per-action `SimulatedOutcome.verified` decides, as before.
        With one, the dataset's follow-up decides: a Tier 0 rerun yields the recorded status (and log text, which
        is the new evidence for a failed attempt); a Tier 3 CI run on the PR branch is green only when the
        proposed paths cover every file the correct fix touches. `SimulatedOutcome.verified=False` always wins.
        """
        self.calls.append(("verify", action.action))
        lock_id = self._lock_id(action)
        if lock_id is not None:
            gone = lock_id not in self.locks.locks
            return VerificationResult(gone and self._outcome(action).verified, "lock table checked")
        out = self._outcome(action)
        follow = self._followups.get(str(action.dry_run.get("execution_id", "")))
        if follow is None:
            return VerificationResult(out.verified, f"simulated verification of {action.action}")
        if action.tier == 3:
            accepted = set(follow["fix_applied"]["accepted_files"]) if follow.get("fix_applied") else set()
            paths = {str(p) for p in action.dry_run.get("paths", [])}
            green = out.verified and bool(accepted) and accepted <= paths
            return VerificationResult(
                green,
                "CI run on the PR branch is green" if green else "CI run on the PR branch is red",
                {"ci": "green" if green else "red", "branch": action.dry_run.get("branch")},
            )
        rerun = follow["rerun"]
        passed = out.verified and rerun["status"] == "success"
        details: dict[str, Any] = {"next_status": "success" if passed else "failed"}
        if not passed and rerun.get("log") and self._data_root is not None:
            details["next_log"] = (self._data_root / rerun["log"]).read_text(encoding="utf-8")
        return VerificationResult(passed, "next execution passed" if passed else "next execution failed again", details)

    def rollback(self, action: Remediation) -> ExecutionResult:
        """Undo a reversible executed action (simulated; a re-run leaves nothing behind to undo)."""
        self.calls.append(("rollback", action.action))
        if not action.reversible:
            return ExecutionResult(False, "refused: action is not reversible", {})
        self.rolled_back.append(action)
        return ExecutionResult(True, f"simulated rollback of {action.action}", {"tier": action.tier})


# ---------------------------------------------------------------------------------------------
# SyntheticSource: the simulated CI provider over the generated dataset's agent-visible files.
# ---------------------------------------------------------------------------------------------

#: Top-level entries of the dataset directory that an agent-facing source may read. Anything else
#: (labels, simulator state) is unreachable by construction: every read goes through `_resolve`.
AGENT_VISIBLE_ENTRIES: frozenset[str] = frozenset({"executions.json", "changes.json", "logs", "repos"})

#: Execution/node status strings used by the generator (confirmed by M3; domain does not constrain them).
EXECUTION_STATUSES = ("success", "failed", "approval_rejected")
NODE_STATUSES = ("success", "failed", "skipped", "rejected")


def parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _node(raw: dict[str, Any]) -> Node:
    return Node(
        node_id=raw["node_id"],
        name=raw["name"],
        status=raw["status"],
        parent_id=raw.get("parent_id"),
        children=[_node(c) for c in raw.get("children", [])],
        error_summary=raw.get("error_summary", ""),
    )


def _execution(raw: dict[str, Any]) -> Execution:
    return Execution(
        execution_id=raw["execution_id"],
        pipeline=raw["pipeline"],
        status=raw["status"],
        started_at=raw["started_at"],
        finished_at=raw.get("finished_at"),
        root=_node(raw["root"]) if raw.get("root") else None,
        refs=dict(raw.get("refs", {})),
    )


class SyntheticSource:
    """ExecutionSource over `executions.json`, `logs/`, `repos/` and `changes.json` of a dataset directory.

    It opens nothing else: the allowlist is enforced in `_resolve`, and tests spy on every file access.
    """

    def __init__(self, data_dir: Path | str) -> None:
        self._root = Path(data_dir).resolve()
        raw = json.loads(self._read_text("executions.json"))
        self._exec_raw: dict[str, dict[str, Any]] = {e["execution_id"]: e for e in raw}
        self._order = sorted(raw, key=lambda e: (e["started_at"], e["execution_id"]))
        self._starts = {e["execution_id"]: parse_time(e["started_at"]) for e in raw}
        self._changes: list[dict[str, Any]] | None = None

    def _resolve(self, relative: str) -> Path:
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] not in AGENT_VISIBLE_ENTRIES:
            raise SourceError(f"path not available: {relative}")
        path = (self._root / rel).resolve()
        # Re-apply the allowlist to the real path, so a symlink into any hidden directory is refused.
        try:
            real_parts = path.relative_to(self._root).parts
        except ValueError:
            raise SourceError(f"path not available: {relative}") from None
        if not real_parts or real_parts[0] not in AGENT_VISIBLE_ENTRIES:
            raise SourceError(f"path not available: {relative}")
        return path

    def _read_text(self, relative: str) -> str:
        return self._resolve(relative).read_text(encoding="utf-8")

    def _get(self, execution_id: str) -> dict[str, Any]:
        try:
            return self._exec_raw[execution_id]
        except KeyError:
            raise SourceError(f"unknown execution: {execution_id}") from None

    def get_execution(self, execution_id: str) -> Execution:
        return _execution(self._get(execution_id))

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
        self._get(execution_id)
        if "/" in node_id or "\\" in node_id or ".." in node_id:
            raise SourceError(f"bad node id: {node_id}")
        try:
            text = self._read_text(f"logs/{execution_id}/{node_id}.log")
        except FileNotFoundError:
            raise SourceError(f"no log for {execution_id}/{node_id}") from None
        truncated = len(text) > budget
        return LogChunk(execution_id, node_id, text[:budget], truncated)

    def list_executions(self, window: Any, filter: Any) -> list[ExecutionSummary]:
        """`window` is None or (start, end) (ISO strings or datetimes, inclusive); `filter` is None or a dict.

        Filter keys: `status`, `pipeline`, or any `refs` key (connector, template, runner_pool, infra_def,
        commit) compared for equality. Results are ordered by start time.
        """
        lo, hi = (parse_time(window[0]), parse_time(window[1])) if window else (None, None)
        flt: dict[str, Any] = dict(filter or {})
        out: list[ExecutionSummary] = []
        for e in self._order:
            t = self._starts[e["execution_id"]]
            if (lo and t < lo) or (hi and t > hi):
                continue
            if any((e[k] if k in ("status", "pipeline") else e["refs"].get(k)) != v for k, v in flt.items()):
                continue
            out.append(
                ExecutionSummary(e["execution_id"], e["pipeline"], e["status"], e["started_at"], dict(e["refs"]))
            )
        return out

    def read_file(self, repo: str, path: str, ref: str, max_bytes: int) -> FileContent:
        for part in (repo, ref):
            if not part or "/" in part or "\\" in part or part in (".", ".."):
                raise SourceError(f"bad repo or ref: {part!r}")
        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise SourceError(f"bad path: {path}")
        try:
            data = self._resolve(f"repos/{repo}/{ref}/{path}").read_bytes()
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            raise SourceError(f"no such file: {repo}@{ref}:{path}") from None
        truncated = len(data) > max_bytes
        return FileContent(repo, path, ref, data[:max_bytes].decode("utf-8", errors="replace"), truncated)

    def list_changes(self, window: Any = None) -> list[dict[str, Any]]:
        """Change timeline entries (commits, releases, config changes), optionally within (start, end)."""
        if self._changes is None:
            self._changes = json.loads(self._read_text("changes.json"))
        if not window:
            return list(self._changes)
        lo, hi = parse_time(window[0]), parse_time(window[1])
        return [c for c in self._changes if lo <= parse_time(c["at"]) <= hi]
