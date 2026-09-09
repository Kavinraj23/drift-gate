"""Change timeline: a real backdated git repo plus an events table with decoys.

PRD.md SS11: "A change timeline of real backdated git commits plus an events table for
template bumps and credential rotations, including decoy changes with no causal link -
so recency heuristics don't get credit they haven't earned."

Causal events are placed shortly before the burst they explain; decoys are placed at
random elsewhere in the window and touch unrelated refs, so a naive "most recent change
before the failure" heuristic gets fooled as often as it should.
"""
from __future__ import annotations

import random
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class ChangeEvent:
    timestamp: datetime
    kind: str  # template_bump | credential_rotation | routine_change
    ref: str
    description: str
    causal: bool
    linked_burst_id: str | None
    commit_sha: str | None = None

    def to_json(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


def _git(repo_dir: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init", "-q", "-b", "main")
    _git(repo_dir, "config", "user.email", "ci-bot@drift-gate.local")
    _git(repo_dir, "config", "user.name", "drift-gate ci-bot")


def _commit_at(repo_dir: Path, path: Path, content: str, message: str, at: datetime) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    rel = path.relative_to(repo_dir)
    _git(repo_dir, "add", str(rel))
    date_str = at.strftime("%Y-%m-%dT%H:%M:%S")
    import os

    env = {**os.environ, "GIT_AUTHOR_DATE": date_str, "GIT_COMMITTER_DATE": date_str}
    _git(repo_dir, "commit", "-q", "-m", message, env=env)
    return _git(repo_dir, "rev-parse", "HEAD")


def build_change_timeline(
    rng: random.Random,
    window_start: datetime,
    window_end: datetime,
    bursts: list,
    out_dir: Path,
    n_decoys: int = 6,
) -> list[ChangeEvent]:
    """Creates the fixture git repo under out_dir/fixture_repo and returns the full
    events list (causal + decoy)."""
    repo_dir = out_dir / "fixture_repo"
    _init_repo(repo_dir)
    _commit_at(
        repo_dir, repo_dir / "README.md", "# fixture infra repo\n",
        "initial commit", window_start,
    )

    events: list[ChangeEvent] = []

    for burst in bursts:
        lead = timedelta(minutes=rng.randint(5, 45))
        at = burst.started_at - lead
        if burst.kind == "template_bump":
            ref = burst.template_ref
            path = repo_dir / "templates" / f"{ref.name}.yaml"
            content = f"name: {ref.name}\nversion: {ref.version}\n"
            message = f"bump {ref.name} to {ref.version}"
            kind = "template_bump"
        else:
            ref = burst.connector_ref
            path = repo_dir / "connectors" / f"{ref}.yaml"
            content = f"connector: {ref}\nrotated_at: {at.isoformat()}\n"
            message = f"rotate credentials for {ref}"
            kind = "credential_rotation"
        sha = _commit_at(repo_dir, path, content, message, at)
        events.append(ChangeEvent(at, kind, str(ref), message, True, burst.id, sha))

    decoy_refs = [
        "runner-pool-config", "ci-notification-webhook", "log-retention-policy",
        "artifact-storage-bucket", "docs-site", "internal-style-guide",
    ]
    for i in range(n_decoys):
        at = window_start + timedelta(
            seconds=rng.randint(0, int((window_end - window_start).total_seconds()))
        )
        ref = decoy_refs[i % len(decoy_refs)]
        path = repo_dir / "misc" / f"{ref}.txt"
        content = f"touched at {at.isoformat()} (decoy #{i})\n"
        message = f"update {ref}"
        sha = _commit_at(repo_dir, path, content, message, at)
        events.append(ChangeEvent(at, "routine_change", ref, message, False, None, sha))

    events.sort(key=lambda e: e.timestamp)
    return events
