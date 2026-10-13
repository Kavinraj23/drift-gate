"""The holdout fetcher is isolated: unreachable from agents/tools/llm, no playground adapter, no write verbs."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "driftgate"
FETCH_MODULES = ("holdout_fetch.py", "holdout_gh.py", "holdout_steplog.py", "holdout_skeleton.py")
FETCH_NAMES = ("holdout_fetch", "holdout_gh", "holdout_steplog", "holdout_skeleton")
FORBIDDEN_IN_FETCHER = (
    "adapters",
    "github_client",
    "github_actions",
    "agents",
    "llm",
    "gates",
    "orchestrator",
    "anthropic",
)
TARGETS = ("RemediationTarget", "SyntheticTarget", "GitHubActionsTarget")


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * node.level) + (node.module or "")
            names.append(mod)
            names += [f"{mod}.{a.name}" for a in node.names]
    return names


@pytest.mark.parametrize("sub", ["agents", "tools", "llm"])
def test_agents_tools_and_llm_cannot_reach_the_fetcher(sub: str) -> None:
    files = sorted((SRC / sub).rglob("*.py"))
    assert files
    for p in files:
        text = p.read_text(encoding="utf-8")
        assert not any(n in text for n in FETCH_NAMES), p
        assert not any(re.search(r"(^|\.)eval(\.|$)", i.replace("driftgate.", "")) for i in _imports(p)), p


def test_nothing_outside_eval_names_the_fetcher() -> None:
    offenders = [
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if not p.relative_to(SRC).as_posix().startswith("eval/")
        and any(n in p.read_text(encoding="utf-8") for n in FETCH_NAMES)
    ]
    assert offenders == []


@pytest.mark.parametrize("name", FETCH_MODULES)
def test_the_fetcher_imports_no_adapter_agent_gate_or_remediation_target(name: str) -> None:
    path = SRC / "eval" / name
    assert path.is_file()
    for imp in _imports(path):
        assert not any(re.search(rf"(^|\.){bad}(\.|$)", imp) for bad in FORBIDDEN_IN_FETCHER), (name, imp)
        assert not any(t in imp for t in TARGETS), (name, imp)
    assert not any(t in path.read_text(encoding="utf-8") for t in TARGETS), name


def test_the_fetcher_has_no_write_verbs_and_no_sleeping() -> None:
    for name in FETCH_MODULES:
        text = (SRC / "eval" / name).read_text(encoding="utf-8")
        assert not re.search(r"\.(post|put|patch|delete|request|head)\(", text), name
        assert "import time" not in text and "sleep(" not in text, name
    gh = (SRC / "eval" / "holdout_gh.py").read_text(encoding="utf-8")
    assert gh.count("session.get(") == 1 and "allow_redirects=False" in gh


def test_only_the_cli_module_reads_the_environment() -> None:
    for name in ("holdout_gh.py", "holdout_steplog.py", "holdout_skeleton.py"):
        text = (SRC / "eval" / name).read_text(encoding="utf-8")
        assert "environ" not in text and "getenv" not in text and "dotenv" not in text, name


def test_the_fetch_step_assigns_no_label_fields() -> None:
    text = (SRC / "eval" / "holdout_fetch.py").read_text(encoding="utf-8")
    body = text.split("def fetch_one")[1].split("def env_token")[0]
    for field in ("classification", "rationale", "labeled_at", "correct_action", '"split"', '"layer"', "labels.json"):
        assert field not in body, field
