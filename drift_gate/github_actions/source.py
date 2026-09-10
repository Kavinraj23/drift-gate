"""Real ExecutionSource against the GitHub Actions REST API (PRD.md SS9, SS11 "what is
genuinely real").

GitHub Actions has no native concept of a "connector," "template," or "runner pool" the
way Harness/Terraform CI systems do - those fields on Execution are filled with the
closest real GitHub analog, documented per-field below rather than left null, since a
null would silently break fleet_correlate/fingerprint. None of these are invented data;
each is a real field on the run/job:
  - pipeline_id    -> the workflow's display name (real: run["name"])
  - connector_ref  -> the repo full name (there is one "connector" in this adapter: the
                      repo itself). Real, just coarse-grained.
  - template_ref   -> TemplateRef(workflow file path, head commit short-sha). GitHub
                      versions workflows via git, not a template registry, so the
                      commit sha is the honest analog of "version."
  - runner_pool    -> the `runs-on` label(s) of the run's jobs. Only available from
                      get_execution (which fetches jobs); list_executions leaves this
                      "unknown" rather than pay for a jobs call per row - documented
                      gap, matches the classifier.py style of stated-not-hidden gaps.
  - infra_ref      -> the head branch (real: run["head_branch"])
  - trigger        -> the triggering event (real: run["event"], e.g. "workflow_dispatch")

Node tree: each job is a parent Node (step_type="job"); each step within it is a leaf
Node, step_type set to the step's own name. Steps have no children, so every FAILED
step node is a leaf by construction - get_failed_leaf_nodes needs no extra logic beyond
that already used by SyntheticSource.

Log extraction: GitHub's job-logs endpoint returns the WHOLE job's plain-text log, not
one step's - there is no per-step log endpoint. GitHub's own per-step started_at/
completed_at are only second-granular, which collapses distinct steps together on any
job that runs in under ~1s per step (common - see _slice_step's docstring), so slicing
is instead done against the sub-second-precise `##[group]Run ...` markers the log text
itself carries for every author-defined step. ANSI color codes (GitHub syntax-highlights
the echoed command text) are stripped before matching. Char-budget truncation happens
at the tool boundary, as PRD.md SS11 requires.

Known gap, stated not hidden: PRD.md SS11 also calls for collapsing repeated lines,
credential redaction, and ranking multiple error blocks by likely relevance - none of
that is implemented yet (TASKS.md Phase 2). Fine for this project's demo workflow,
which has one small error block and nothing secret in it; would need doing before
pointing this at a real production repo's logs.
"""
from __future__ import annotations

import re
from datetime import datetime

from drift_gate.domain import (
    Execution,
    ExecutionStatus,
    ExecutionSummary,
    LogChunk,
    Node,
    NodeStatus,
    TemplateRef,
)
from drift_gate.github_actions.client import GitHubClient

_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s?(.*)$"
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _parse_gh_time(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _map_run_status(status: str, conclusion: str | None) -> ExecutionStatus:
    if status != "completed":
        return ExecutionStatus.RUNNING
    return {
        "success": ExecutionStatus.SUCCESS,
        "cancelled": ExecutionStatus.ABORTED,
        "action_required": ExecutionStatus.REJECTED,
    }.get(conclusion, ExecutionStatus.FAILED)


def _map_node_status(status: str, conclusion: str | None) -> NodeStatus:
    if status != "completed":
        return NodeStatus.RUNNING
    return {
        "success": NodeStatus.SUCCESS,
        "skipped": NodeStatus.SKIPPED,
        "neutral": NodeStatus.SKIPPED,
    }.get(conclusion, NodeStatus.FAILED)


class GitHubActionsSource:
    def __init__(self, owner: str, repo: str, client: GitHubClient | None = None):
        self.owner = owner
        self.repo = repo
        self.client = client or GitHubClient(owner, repo)

    def _template_ref(self, run: dict) -> TemplateRef:
        path = run.get("path") or run["name"]
        return TemplateRef(name=path, version=run["head_sha"][:7])

    def _jobs(self, run_id: str) -> list[dict]:
        return self.client.get_all_pages(f"/actions/runs/{run_id}/jobs", "jobs")

    def _nodes_for_run(self, jobs: list[dict]) -> tuple[Node, ...]:
        nodes: list[Node] = []
        for job in jobs:
            job_node_id = f"job-{job['id']}"
            nodes.append(Node(
                id=job_node_id, name=job["name"], step_type="job",
                status=_map_node_status(job["status"], job.get("conclusion")),
                parent_id=None,
                started_at=_parse_gh_time(job.get("started_at")),
                ended_at=_parse_gh_time(job.get("completed_at")),
            ))
            for step in job.get("steps") or []:
                nodes.append(Node(
                    id=f"{job_node_id}-step-{step['number']}", name=step["name"],
                    step_type=step["name"],
                    status=_map_node_status(step["status"], step.get("conclusion")),
                    parent_id=job_node_id,
                    started_at=_parse_gh_time(step.get("started_at")),
                    ended_at=_parse_gh_time(step.get("completed_at")),
                ))
        return tuple(nodes)

    def get_execution(self, execution_id: str) -> Execution:
        run = self.client.get(f"/actions/runs/{execution_id}")
        jobs = self._jobs(execution_id)
        runner_pool = ",".join(sorted({rl for j in jobs for rl in j.get("labels", [])})) \
            or "unknown"
        return Execution(
            id=execution_id, pipeline_id=run["name"],
            status=_map_run_status(run["status"], run.get("conclusion")),
            started_at=_parse_gh_time(run.get("run_started_at") or run["created_at"]),
            ended_at=_parse_gh_time(run["updated_at"]) if run["status"] == "completed" else None,
            connector_ref=f"{self.owner}/{self.repo}",
            template_ref=self._template_ref(run), runner_pool=runner_pool,
            infra_ref=run["head_branch"] or "unknown", trigger=run["event"],
            nodes=self._nodes_for_run(jobs),
        )

    def get_failed_leaf_nodes(self, execution_id: str) -> list[Node]:
        execution = self.get_execution(execution_id)
        parent_ids = {n.parent_id for n in execution.nodes if n.parent_id is not None}
        return [
            n for n in execution.nodes
            if n.status == NodeStatus.FAILED and n.id not in parent_ids
        ]

    _BOOKEND_STEP_NAMES = {"Set up job", "Complete job"}

    @classmethod
    def _slice_step(cls, content_lines: list[str], steps: list[dict], step: dict) -> str:
        """GitHub's per-step API timestamps are second-granularity, which collapses
        distinct steps in a sub-second-fast job into the same instant - useless for
        slicing. The log text itself is precise: every author-defined step (`run:` or
        `uses:`) opens with its own top-level `##[group]Run ...` line, in job order,
        which the synthetic bookend steps ("Set up job", "Complete job") and
        marker-less "Post *" steps do not emit. Real steps are matched to their
        marker by position; bookend/post steps fall back to returning nothing rather
        than guessing wrong - they essentially never carry the actual failure anyway.
        """
        run_marker_idxs = [
            i for i, line in enumerate(content_lines) if line.startswith("##[group]Run ")
        ]
        real_steps = [
            s for s in steps
            if s["name"] not in cls._BOOKEND_STEP_NAMES and not s["name"].startswith("Post ")
        ]
        if step not in real_steps or len(run_marker_idxs) < len(real_steps):
            return ""
        idx = real_steps.index(step)
        start = run_marker_idxs[idx]
        end = run_marker_idxs[idx + 1] if idx + 1 < len(run_marker_idxs) else len(content_lines)
        return "\n".join(content_lines[start:end])

    def get_step_logs(self, execution_id: str, node_id: str, budget: int) -> LogChunk:
        job_id = node_id.split("-step-")[0].removeprefix("job-")
        step_number = int(node_id.rsplit("-step-", 1)[1])
        jobs = self._jobs(execution_id)
        job = next(j for j in jobs if str(j["id"]) == job_id)
        step = next(s for s in job["steps"] if s["number"] == step_number)

        full_log = self.client.get_text(f"/actions/jobs/{job_id}/logs")
        content_lines = []
        for line in full_log.lstrip("﻿").splitlines():
            m = _TIMESTAMP_RE.match(line)
            content_lines.append(m.group(2) if m else line)

        text = _strip_ansi(self._slice_step(content_lines, job["steps"], step))

        truncated = len(text) > budget
        return LogChunk(
            text=text[:budget], truncated=truncated, char_budget=budget,
            source_node_id=node_id,
        )

    def list_executions(
        self, window: tuple[datetime, datetime], filter: dict | None = None
    ) -> list[ExecutionSummary]:
        start, end = window
        created = f"{start.isoformat()}..{end.isoformat()}"
        runs = self.client.get_all_pages(
            "/actions/runs", "workflow_runs", params={"created": created}
        )
        filter = filter or {}
        out = []
        for run in runs:
            summary = ExecutionSummary(
                id=str(run["id"]), pipeline_id=run["name"],
                status=_map_run_status(run["status"], run.get("conclusion")),
                started_at=_parse_gh_time(run.get("run_started_at") or run["created_at"]),
                connector_ref=f"{self.owner}/{self.repo}",
                template_ref=self._template_ref(run), runner_pool="unknown",
            )
            if any(getattr(summary, k, None) != v for k, v in filter.items()):
                continue
            out.append(summary)
        out.sort(key=lambda s: s.started_at)
        return out
