"""PreToolUse(Bash) guard: exit 2 with a message for commands that must never run from an agent."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ROOT, read_payload  # noqa: E402

HUMAN_ONLY = {"record", "eval-live", "e2e-live", "playground-reset"}
READERS = {
    "cat", "type", "less", "more", "head", "tail", "grep", "rg", "sed", "awk", "sort", "source", ".",
    "get-content", "gc", "select-string", "sls", "cp", "copy", "copy-item", "xxd", "od", "strings", "findstr",
}  # fmt: skip
NETWORK = {"curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm", "gh"}
PYTHONS = {"python", "python3", "py", "pythonw"}
HOSTS = ("api.github.com", "api.anthropic.com")
SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n|&")
ENV_REASON = "Blocked: .env holds credentials and must never be read (invariant 11)."


def _tokens(segment: str) -> list[str]:
    segment = segment.replace("\\", "/")
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _exe(token: str) -> str:
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _is_env_file(token: str) -> bool:
    base = token.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
    return re.fullmatch(r"\.env(\..+)?", base) is not None and base != ".env.example"


def _git_subcommand(tokens: list[str]) -> str | None:
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 2 if tokens[i] in ("-C", "-c") else 1
    return tokens[i].lower() if i < len(tokens) else None


def check_command(command: str, autonomous: bool) -> str | None:
    """Return a block reason, or None when the command is allowed."""
    whole = command.lower().replace("\\", "/")
    if any(host in whole for host in HOSTS):
        exes = {_exe(t) for seg in SEGMENT_SPLIT.split(command) for t in _tokens(seg.strip())[:1]}
        if exes & (NETWORK | PYTHONS):
            return "Blocked: no direct calls to api.github.com or api.anthropic.com; use the adapters and the gateway."
    for segment in SEGMENT_SPLIT.split(command):
        tokens = _tokens(segment.strip())
        if not tokens:
            continue
        exe = _exe(tokens[0])
        lowered = segment.lower().replace("\\", "/")
        if exe == "git" and _git_subcommand(tokens) == "push":
            return "Blocked: git push is never run by the agent. Pushing is a human action."
        if exe == "gh" and [t.lower() for t in tokens[1:3]] == ["pr", "merge"]:
            return "Blocked: agents never merge pull requests (invariant 4)."
        if any(host in lowered for host in HOSTS) and (exe in NETWORK or exe in PYTHONS):
            return "Blocked: no direct calls to api.github.com or api.anthropic.com; use the adapters and the gateway."
        if exe == "gh" and len(tokens) > 1 and tokens[1].lower() == "api":
            return "Blocked: no direct GitHub API calls from the agent."
        if (exe in READERS or exe in PYTHONS) and any(_is_env_file(t) for t in tokens[1:]):
            return ENV_REASON
        if exe in PYTHONS and re.search(r"[\"'/]\.env(?!\.example)\b", lowered):
            return ENV_REASON
        if autonomous and "tasks.py" in lowered and any(t.lower() in HUMAN_ONLY for t in tokens[1:]):
            return "Blocked: this target is human-only and .autonomous exists."
    return None


def main() -> int:
    payload = read_payload()
    command = (payload.get("tool_input") or {}).get("command", "")
    reason = check_command(command, (ROOT / ".autonomous").exists())
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
