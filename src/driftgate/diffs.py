"""Structured unified diffs: parse, apply, render. Pure code, no I/O.

A Tier 3 proposal carries its change as a unified diff. The model emits text; this module turns it into a
`StructuredDiff` (files, hunks) that deterministic code can check and a reviewer can read, and applies it to file
contents so the checks see the exact result.

Parsing is lenient where models are sloppy and strict where it matters for safety:
- hunk header line counts are recomputed from the hunk body (only the start lines are taken from the header);
- a blank line inside a hunk is a blank context line;
- `diff --git`, `index`, mode and similar header lines are ignored;
- anything else that is not a recognisable diff is a `DiffError`.
Applying is strict: every context and removed line must match, at the stated line or at one unique offset.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEV_NULL = "/dev/null"
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_IGNORED_HEADERS = (
    "diff ",
    "index ",
    "old mode",
    "new mode",
    "similarity ",
    "rename ",
    "new file mode",
    "deleted file",
)


class DiffError(ValueError):
    """The text is not a usable unified diff, or it does not apply to the given contents."""


@dataclass(frozen=True)
class Hunk:
    old_start: int
    new_start: int
    lines: tuple[str, ...]  # each line keeps its one-character prefix: ' ' context, '-' removed, '+' added

    @property
    def added(self) -> int:
        return sum(1 for line in self.lines if line.startswith("+"))

    @property
    def removed(self) -> int:
        return sum(1 for line in self.lines if line.startswith("-"))

    @property
    def old_count(self) -> int:
        return sum(1 for line in self.lines if line[:1] in (" ", "-"))

    @property
    def new_count(self) -> int:
        return sum(1 for line in self.lines if line[:1] in (" ", "+"))


@dataclass(frozen=True)
class FileDiff:
    old_path: str | None  # None for a new file
    new_path: str | None  # None for a deleted file
    hunks: tuple[Hunk, ...]

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def is_new(self) -> bool:
        return self.old_path is None

    @property
    def is_deleted(self) -> bool:
        return self.new_path is None

    @property
    def is_rename(self) -> bool:
        return self.old_path is not None and self.new_path is not None and self.old_path != self.new_path

    @property
    def added(self) -> int:
        return sum(h.added for h in self.hunks)

    @property
    def removed(self) -> int:
        return sum(h.removed for h in self.hunks)

    @property
    def net(self) -> int:
        """Added minus removed lines; negative means the file shrinks."""
        return self.added - self.removed


@dataclass(frozen=True)
class StructuredDiff:
    files: tuple[FileDiff, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files)

    @property
    def added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def removed(self) -> int:
        return sum(f.removed for f in self.files)

    def to_dict(self) -> dict[str, Any]:
        """JSON-able form stored in `Remediation.dry_run["diff"]` (the contract allows any object there)."""
        return {
            "files": [
                {
                    "path": f.path,
                    "old_path": f.old_path,
                    "new_path": f.new_path,
                    "added": f.added,
                    "removed": f.removed,
                    "hunks": [
                        {
                            "old_start": h.old_start,
                            "old_count": h.old_count,
                            "new_start": h.new_start,
                            "new_count": h.new_count,
                            "lines": list(h.lines),
                        }
                        for h in f.hunks
                    ],
                }
                for f in self.files
            ],
            "added": self.added,
            "removed": self.removed,
        }


def _strip_prefix(raw: str) -> str | None:
    name = raw.split("\t")[0].strip()
    if name == DEV_NULL:
        return None
    if name[:2] in ("a/", "b/"):
        name = name[2:]
    return name


def parse_diff(text: str) -> StructuredDiff:
    if not text or not text.strip():
        raise DiffError("the diff is empty")
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    files: list[FileDiff] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                raise DiffError(f"line {i + 1}: '---' header is not followed by '+++'")
            old, new = _strip_prefix(line[4:]), _strip_prefix(lines[i + 1][4:])
            if old is None and new is None:
                raise DiffError(f"line {i + 1}: both sides of the file header are /dev/null")
            i += 2
            hunks: list[Hunk] = []
            while i < len(lines) and lines[i].startswith("@@"):
                m = _HUNK.match(lines[i])
                if not m:
                    raise DiffError(f"line {i + 1}: malformed hunk header")
                i += 1
                body: list[str] = []
                while i < len(lines) and not lines[i].startswith(("@@", "--- ", "diff ")):
                    raw = lines[i]
                    if raw.startswith("\\"):  # "\ No newline at end of file"
                        body.append(raw)
                    elif raw == "":
                        body.append(" ")
                    elif raw[0] in " +-":
                        body.append(raw)
                    else:
                        raise DiffError(f"line {i + 1}: not a diff line")
                    i += 1
                hunks.append(Hunk(int(m.group(1)), int(m.group(3)), tuple(body)))
            if not hunks:
                raise DiffError(f"{new or old}: file header without any hunk")
            files.append(FileDiff(old, new, tuple(hunks)))
        elif line.startswith(_IGNORED_HEADERS) or not line.strip():
            i += 1
        else:
            raise DiffError(f"line {i + 1}: not a unified diff")
    if not files:
        raise DiffError("no file diffs found")
    return StructuredDiff(tuple(files))


def _split(content: str) -> list[str]:
    return content.splitlines(keepends=True)


def _match_at(src: list[str], at: int, old: list[str]) -> bool:
    return at >= 0 and src[at : at + len(old)] == old if old else at >= 0


def _hunk_lines(hunk: Hunk) -> tuple[list[str], list[str]]:
    """Old and new line lists (with newlines) for a hunk; a `\\` marker strips the previous line's newline."""
    old: list[str] = []
    new: list[str] = []
    last_kind = " "
    for raw in hunk.lines:
        if raw.startswith("\\"):
            targets = {" ": (old, new), "-": (old,), "+": (new,)}[last_kind]
            for t in targets:
                if t:
                    t[-1] = t[-1].rstrip("\n")
            continue
        kind, body = raw[0], raw[1:] + "\n"
        last_kind = kind
        if kind in " -":
            old.append(body)
        if kind in " +":
            new.append(body)
    return old, new


def apply_file_diff(old_content: str | None, fd: FileDiff) -> str | None:
    """New contents of one file (None when the diff deletes it). Raises DiffError if it does not apply."""
    if fd.is_new:
        if old_content is not None:
            raise DiffError(f"{fd.path}: marked as a new file but it already exists")
        src: list[str] = []
    else:
        if old_content is None:
            raise DiffError(f"{fd.path}: file does not exist")
        src = _split(old_content)
    out: list[str] = []
    cursor = 0
    for n, hunk in enumerate(fd.hunks, start=1):
        old, new = _hunk_lines(hunk)
        want = max(hunk.old_start - 1, 0) if old else hunk.old_start
        at = want if _match_at(src, want, old) and want >= cursor else -1
        if at < 0:
            hits = [k for k in range(cursor, len(src) - len(old) + 1) if old and src[k : k + len(old)] == old]
            if len(hits) != 1:
                reason = "does not match" if not hits else "matches in several places"
                raise DiffError(f"{fd.path}: hunk {n} {reason} the file contents")
            at = hits[0]
        out.extend(src[cursor:at])
        out.extend(new)
        cursor = at + len(old)
    out.extend(src[cursor:])
    if fd.is_deleted:
        if "".join(out):
            raise DiffError(f"{fd.path}: deletion diff does not remove the whole file")
        return None
    return "".join(out)


def apply_diff(diff: StructuredDiff, read: Callable[[str], str | None]) -> dict[str, str | None]:
    """Apply every file diff against `read(path)` (None when missing). Returns path -> new contents (None = deleted)."""
    result: dict[str, str | None] = {}
    for fd in diff.files:
        if fd.path in result:
            raise DiffError(f"{fd.path}: appears in the diff more than once")
        result[fd.path] = apply_file_diff(None if fd.is_new else read(fd.old_path or ""), fd)
    return result


def make_unified_diff(before: dict[str, str], after: dict[str, str]) -> str:
    """Unified diff between two path -> contents maps (paths sorted). Used by scripts and tests, not by gates."""
    import difflib

    chunks: list[str] = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        chunks.extend(
            difflib.unified_diff(
                (old or "").splitlines(keepends=True),
                (new or "").splitlines(keepends=True),
                fromfile=f"a/{path}" if old is not None else DEV_NULL,
                tofile=f"b/{path}" if new is not None else DEV_NULL,
            )
        )
    return "".join(chunks)
