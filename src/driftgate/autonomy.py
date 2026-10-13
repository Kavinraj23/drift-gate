"""Is an autonomous run in progress? One shared check for the human-only targets.

`.autonomous` counts as present when it exists at `root`, or at the main checkout when `root` is a git worktree (read
from the worktree's `.git` FILE, or from the layout `<main>/.claude/worktrees/<name>`). "Present" is `os.path.lexists`,
so a dangling symlink or an odd entry counts. This mirrors the fetcher's check (which is fixed to the
repository root of that module and cannot be pointed elsewhere); a test pins the two to the same answers.
No `git` subprocess is run, so there is nothing to hang or hijack.
"""

from __future__ import annotations

import os
from pathlib import Path

MARKER = ".autonomous"


def worktree_main_checkout(root: Path) -> Path | None:
    try:
        text = (root / ".git").read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    rest = text[len("gitdir:") :].strip()
    gitdir = Path(rest.splitlines()[0] if rest else "")
    if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
        return gitdir.parent.parent.parent
    return None


def autonomous_present_at(root: Path) -> bool:
    roots = [root]
    if root.parent.name == "worktrees" and root.parent.parent.name == ".claude":
        roots.append(root.parent.parent.parent)
    main = worktree_main_checkout(root)
    if main is not None:
        roots.append(main)
    return any(os.path.lexists(r / MARKER) for r in roots)
