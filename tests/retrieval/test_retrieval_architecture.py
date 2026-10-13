"""Static checks for the retrieval modules: no target implementations, no labelled data, no SDK, no network."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "driftgate"
MODULES = ["retrieval.py", "tools/similar_history.py"]
FORBIDDEN_TEXT = ("ground_truth", "holdout", "labels.json", "scenarios.json")
FORBIDDEN_IMPORT_PREFIXES = (
    "driftgate.eval",
    "driftgate.generator",
    "driftgate.adapters",
    "anthropic",
    "requests",
    "socket",
    "urllib",
    "http",
)


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


@pytest.mark.parametrize("rel", MODULES)
def test_module_does_not_name_or_open_labelled_data(rel: str) -> None:
    text = (SRC / rel).read_text(encoding="utf-8").lower()
    assert [w for w in FORBIDDEN_TEXT if w in text] == []


@pytest.mark.parametrize("rel", MODULES)
def test_module_imports_no_eval_adapter_sdk_or_network_code(rel: str) -> None:
    bad = [n for n in _imports(SRC / rel) if n.startswith(FORBIDDEN_IMPORT_PREFIXES)]
    assert bad == []


def test_only_the_orchestrator_writes_the_index() -> None:
    writers = []
    for p in sorted(SRC.rglob("*.py")):
        rel = p.relative_to(SRC).as_posix()
        if rel == "retrieval.py":
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("add", "add_all") and _on_history(node.value):
                writers.append(rel)
    assert sorted(set(writers)) == ["orchestrator.py"]


def _on_history(node: ast.expr) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr in ("_history", "history")) or (
        isinstance(node, ast.Name) and node.id in ("history", "index", "idx")
    )


def test_agents_and_tools_never_name_the_record_type() -> None:
    for sub in ("agents", "tools"):
        for p in (SRC / sub).rglob("*.py"):
            assert "IncidentRecord" not in p.read_text(encoding="utf-8"), p
