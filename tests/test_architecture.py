"""Architecture invariants enforced by static import scans (pass trivially now, enforce later)."""

from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "driftgate"
TARGET_IMPLS = ("adapters", "SyntheticTarget", "GitHubActionsTarget")


def _py_files(sub: str = "") -> list[Path]:
    return sorted((SRC / sub).rglob("*.py"))


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names.append(mod)
            names += [f"{mod}.{a.name}" for a in node.names]
    return names


def test_only_gateway_imports_anthropic() -> None:
    offenders = [
        p.relative_to(SRC).as_posix()
        for p in _py_files()
        if p.relative_to(SRC).as_posix() != "llm/gateway.py"
        and any(n.split(".")[0] == "anthropic" for n in _imports(p))
    ]
    assert offenders == []


def test_ground_truth_only_referenced_in_eval_and_generator() -> None:
    offenders = []
    for p in _py_files():
        rel = p.relative_to(SRC).as_posix()
        if rel.startswith(("eval/", "generator/")):
            continue
        if "ground_truth" in p.read_text(encoding="utf-8").lower():
            offenders.append(rel)
    assert offenders == []


@pytest.mark.parametrize("sub", ["agents", "tools"])
def test_agents_and_tools_do_not_import_remediation_targets(sub: str) -> None:
    offenders = []
    for p in _py_files(sub):
        for name in _imports(p):
            if any(name.startswith(f"driftgate.{t}") or name.endswith(t) for t in TARGET_IMPLS):
                offenders.append((p.relative_to(SRC).as_posix(), name))
    assert offenders == []


def test_network_is_blocked_in_tests() -> None:
    with pytest.raises(RuntimeError, match="network access is blocked"):
        socket.create_connection(("127.0.0.1", 9))
