"""HUMAN-RUN admin for the `drift-gate-playground` repo (M8b). Not imported by the package; never run by agents.

    python scripts/playground.py setup            # dry run: prints what it would push
    python scripts/playground.py setup --yes      # create main, six scenario branches, baseline tags; push them
    python scripts/playground.py sync-main --yes  # copy the six workflow files onto main so Actions lists them
    python scripts/playground.py reset            # dry run
    python scripts/playground.py reset --yes      # force scenario branches back to their baseline tag,
                                                  # delete remote driftgate/* branches, close open PRs (needs `gh`)

Uses your own git login (credential manager); it never reads `.env` or the PAT. The remote is hard-coded and every
command refuses any other repository (invariant 12).

Layout deviation to confirm against the PRD ("a tagged `baseline`"): the workflows must sit at the root of their own
branch, so there is one tag per scenario, `baseline/<name>`, rather than a single tag.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAYGROUND_DIR = ROOT / "playground"
REPO = "Kavinraj23/drift-gate-playground"
REMOTE = f"https://github.com/{REPO}.git"
SCENARIOS = ("flaky", "throttle", "lockfile", "provider-pin", "undefined-var", "approval-rejected")
MAIN_README = (
    "# drift-gate-playground\n\n"
    "Scratch repo for DriftGate live scenarios. Each `scenario/*` branch holds one deliberately broken workflow.\n"
    "Nothing here is real code. Do not merge agent PRs from `driftgate/*` without reading them.\n"
)


def require_repo(remote: str) -> None:
    if remote != REMOTE:
        raise SystemExit(f"refusing remote {remote!r}: only {REMOTE} is allowed")


def branch(name: str) -> str:
    return f"scenario/{name}"


def tag(name: str) -> str:
    return f"baseline/{name}"


def plan_setup() -> list[str]:
    lines = ["main (README + the six workflow files)"]
    for n in SCENARIOS:
        lines.append(f"{branch(n)}  <- playground/{n}/   tag {tag(n)}")
    return lines


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=check, text=True, capture_output=True)


def setup(yes: bool) -> int:
    require_repo(REMOTE)
    for line in plan_setup():
        print(" ", line)
    for n in SCENARIOS:
        if not (PLAYGROUND_DIR / n).is_dir():
            raise SystemExit(f"missing playground/{n}")
    if not yes:
        print(f"dry run. Create the empty repo {REPO} first, then re-run with --yes.")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        _git(work, "init", "-b", "main")
        (work / "README.md").write_text(MAIN_README, encoding="utf-8")
        _copy_workflows(work)
        _git(work, "add", "-A")
        _git(work, "commit", "-m", "playground: main")
        for n in SCENARIOS:
            _git(work, "checkout", "-B", branch(n), "main")
            shutil.copytree(PLAYGROUND_DIR / n, work, dirs_exist_ok=True)
            _git(work, "add", "-A", "-f")
            _git(work, "commit", "-m", f"playground: {n} scenario")
            _git(work, "tag", "-f", tag(n))
        _git(work, "remote", "add", "origin", REMOTE)
        refs = ["main", *(branch(n) for n in SCENARIOS), *(f"refs/tags/{tag(n)}" for n in SCENARIOS)]
        out = _git(work, "push", "origin", *refs, check=False)
        print(out.stdout + out.stderr)
        return out.returncode
    return 0


def _copy_workflows(work: Path) -> None:
    for n in SCENARIOS:
        for f in (PLAYGROUND_DIR / n / ".github" / "workflows").glob("*.yml"):
            dest = work / ".github" / "workflows" / f.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)


def sync_main(yes: bool) -> int:
    """GitHub lists (and lets you dispatch) a workflow only once its file is on the default branch."""
    require_repo(REMOTE)
    print(f"copy the {len(SCENARIOS)} workflow files onto main of {REPO}")
    if not yes:
        print("dry run. Re-run with --yes.")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        _git(work, "init", "-b", "main")
        _git(work, "remote", "add", "origin", REMOTE)
        _git(work, "pull", "origin", "main")
        _copy_workflows(work)
        _git(work, "add", "-A", "-f")
        if not _git(work, "status", "--porcelain").stdout.strip():
            print("main already has the workflows")
            return 0
        _git(work, "commit", "-m", "playground: workflows on main so Actions lists them")
        out = _git(work, "push", "origin", "main", check=False)
        print(out.stdout + out.stderr)
        return out.returncode


def reset(yes: bool) -> int:
    require_repo(REMOTE)
    print(f"reset {REPO}: force each scenario branch to its baseline tag; delete driftgate/*; close open PRs")
    if not yes:
        print("dry run. Re-run with --yes.")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        _git(work, "init", "-b", "main")
        _git(work, "remote", "add", "origin", REMOTE)
        _git(work, "fetch", "origin", "--tags", "+refs/heads/*:refs/remotes/origin/*")
        code = 0
        for n in SCENARIOS:
            out = _git(work, "push", "--force", "origin", f"refs/tags/{tag(n)}:refs/heads/{branch(n)}", check=False)
            print(out.stdout + out.stderr)
            code = code or out.returncode
        heads = _git(work, "ls-remote", "--heads", "origin", "refs/heads/driftgate/*").stdout.split()
        for ref in (h for h in heads if h.startswith("refs/heads/driftgate/")):
            out = _git(work, "push", "origin", "--delete", ref, check=False)
            print(out.stdout + out.stderr)
            code = code or out.returncode
    if shutil.which("gh"):
        listed = subprocess.run(
            ["gh", "pr", "list", "--repo", REPO, "--state", "open", "--json", "number", "--jq", ".[].number"],
            text=True,
            capture_output=True,
        )
        for num in listed.stdout.split():
            subprocess.run(["gh", "pr", "close", num, "--repo", REPO], check=False)
    else:
        print("`gh` not found: close any open PRs by hand.")
    return code


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=("setup", "sync-main", "reset"))
    p.add_argument("--yes", action="store_true")
    a = p.parse_args(argv)
    return {"setup": setup, "sync-main": sync_main, "reset": reset}[a.command](a.yes)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
