"""Hook decision logic: PreToolUse blocklist/allowlist (incl. Windows paths) and Stop-hook rules."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / ".claude" / "hooks"
sys.path.insert(0, str(HOOKS))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HOOKS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pre = _load("pre_tool_use")
stop = _load("stop_hook")

BLOCKED = [
    "git push",
    "(git push)",
    "$(git push)",
    "env git push",
    "GIT_TRACE=1 git push",
    "bash -c 'git push'",
    "cmd /c git push",
    "cmd /c type .env",
    'powershell -c "gc .env"',
    "sh -c 'cat .env'",
    "tr a b < .env",
    "gh -R x/y pr merge 1",
    "git push origin main",
    "git push --force origin mvp1/m0-skeleton",
    "git -C C:\\Users\\Kavin\\projects\\drift-gate push",
    "cd C:\\Users\\Kavin\\projects\\drift-gate && git push origin main",
    "git status; git push",
    "gh pr merge 12 --squash",
    "gh api repos/x/y",
    "curl https://api.github.com/repos/x/y",
    'curl -H "x-api-key: k" https://api.anthropic.com/v1/messages',
    "Invoke-WebRequest https://api.github.com/user",
    "python -c \"import requests; requests.get('https://api.github.com')\"",
    "cat .env",
    "type C:\\Users\\Kavin\\projects\\drift-gate\\.env",
    "cat .\\.env",
    "Get-Content .env.local",
    "head -n 3 ./.env",
    "python -c \"print(open('.env').read())\"",
]
BLOCKED_AUTONOMOUS_ONLY = [
    "python tasks.py record",
    "python tasks.py eval-live",
    ".\\.venv\\Scripts\\python.exe .\\tasks.py e2e-live",
    "python tasks.py playground-reset && echo done",
]
ALLOWED = [
    "git status",
    "git add -A && git commit -m 'push the button'",
    "git log --oneline",
    "git switch -c mvp1/m1-generator",
    "git merge mvp1/m2-gateway",
    "python tasks.py lint",
    "python tasks.py test",
    "python tasks.py mvp-check",
    "pytest -q tests\\test_hooks.py",
    "ruff check src",
    "cat .env.example",
    "type C:\\Users\\Kavin\\projects\\drift-gate\\.env.example",
    "cat CLAUDE.md",
    "ls -la",
    "gh pr view 3",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_blocked(command: str) -> None:
    assert pre.check_command(command, autonomous=False), command


@pytest.mark.parametrize("command", BLOCKED_AUTONOMOUS_ONLY)
def test_human_only_blocked_only_while_autonomous(command: str) -> None:
    assert pre.check_command(command, autonomous=True), command
    assert pre.check_command(command, autonomous=False) is None, command


@pytest.mark.parametrize("command", ALLOWED)
@pytest.mark.parametrize("autonomous", [False, True])
def test_allowed(command: str, autonomous: bool) -> None:
    assert pre.check_command(command, autonomous=autonomous) is None, command


FAILING = [("seeded dataset generates", "M1")]


def _decide(**kw):
    args = dict(stop_hook_active=False, autonomous=True, failing=FAILING, blockers_text="", head="abc", state={})
    args.update(kw)
    return stop.decide(**args)


def test_stop_allows_when_not_autonomous() -> None:
    assert _decide(autonomous=False)[0] is False


def test_stop_allows_when_loop_prevention_field_set() -> None:
    assert _decide(stop_hook_active=True)[0] is False


def test_stop_allows_when_everything_passes() -> None:
    assert _decide(failing=[])[0] is False


def test_stop_allows_when_blockers_account_for_failures() -> None:
    assert _decide(blockers_text="## Needs human\n- M1 blocked: needs data")[0] is False


def test_stop_blocks_otherwise_and_counts() -> None:
    block, reason, state = _decide()
    assert block and "M1" in reason and state == {"head": "abc", "blocks": 1}


def test_stop_allows_after_three_blocks_with_no_new_commits() -> None:
    assert _decide(state={"head": "abc", "blocks": 3})[0] is False


def test_stop_resets_count_on_new_commit() -> None:
    block, _, state = _decide(head="def", state={"head": "abc", "blocks": 3})
    assert block and state == {"head": "def", "blocks": 1}


def test_failing_checks_parses_mvp_check_table() -> None:
    out = (
        "check                     milestone  status\n"
        "lint + tests              M0         pass\n"
        "seeded dataset generates  M1         pending\n"
    )
    assert stop.failing_checks(out) == [("seeded dataset generates", "M1")]
