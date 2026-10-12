"""N5 architecture: the report is offline, reads no secrets, imports no SDK, and nothing the agent runs imports it."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "driftgate"
REPORT = sorted((SRC / "report").glob("*.py"))
NETWORK = {"socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "anthropic", "dotenv"}
DOTENV = "." + "env"  # spelled this way so this file itself never names the credentials file


def imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * node.level) + (node.module or "")
            names += [mod] + [f"{mod}.{a.name}" for a in node.names]
    return names


def test_the_report_package_exists() -> None:
    assert {p.name for p in REPORT} >= {"__init__.py", "__main__.py", "render.py", "trace.py", "style.py"}


@pytest.mark.parametrize("path", REPORT, ids=lambda p: p.name)
def test_report_modules_import_no_network_or_sdk_module(path: Path) -> None:
    assert [n for n in imports(path) if n.split(".")[0] in NETWORK] == []


@pytest.mark.parametrize("path", REPORT, ids=lambda p: p.name)
def test_report_modules_never_touch_credentials_ground_truth_or_targets(path: Path) -> None:
    text = path.read_text(encoding="utf-8").lower()
    assert DOTENV not in text and "ground_truth" not in text and "environ" not in text and "getenv" not in text
    bad = [n for n in imports(path) if ".adapters" in n or n.startswith("driftgate.eval") or "SyntheticTarget" in n]
    assert bad == []


@pytest.mark.parametrize("path", REPORT, ids=lambda p: p.name)
def test_report_modules_contain_no_script_or_external_reference(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "<script" not in text.lower() and "http://" not in text and "https://" not in text


@pytest.mark.parametrize("sub", ["agents", "tools", "llm", "adapters"])
def test_nothing_the_agent_runs_imports_the_report(sub: str) -> None:
    offenders = [
        (p.relative_to(SRC).as_posix(), n)
        for p in (SRC / sub).rglob("*.py")
        for n in imports(p)
        if n.startswith("driftgate.report") or n.endswith(".report") or n == "report"
    ]
    assert offenders == []


def test_only_the_cli_writes_files() -> None:
    for p in REPORT:
        if p.name == "__main__.py":
            continue
        text = p.read_text(encoding="utf-8")
        assert "write_text" not in text and "open(" not in text and "mkdir" not in text, p.name
