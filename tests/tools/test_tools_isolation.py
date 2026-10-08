"""tools/ stay read-only and provider-agnostic: no adapters, no targets, no eval/generator, no model SDK."""

from __future__ import annotations

import ast
import re
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "src" / "driftgate" / "tools"
FORBIDDEN = (
    "driftgate.adapters",
    "driftgate.eval",
    "driftgate.generator",
    "driftgate.llm",
    "driftgate.gates",
    "anthropic",
)


def _imports(path: Path) -> list[str]:
    out: list[str] = []
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            out.append(n.module or "")
    return out


def test_tools_import_none_of_the_forbidden_packages() -> None:
    files = sorted(TOOLS.glob("*.py"))
    assert len(files) >= 10
    offenders = [(f.name, m) for f in files for m in _imports(f) if m.startswith(FORBIDDEN)]
    assert offenders == []


def test_tools_have_no_write_path() -> None:
    src = "\n".join(f.read_text(encoding="utf-8") for f in TOOLS.glob("*.py"))
    for needle in (
        "write_text",
        "write_bytes",
        ".execute(",
        ".dry_run(",
        "RemediationTarget",
        "subprocess",
        "requests",
    ):
        assert not re.search(rf"(?<![A-Za-z]){re.escape(needle)}", src), needle


SRC = TOOLS.parent
SCANNED = (
    *sorted(TOOLS.glob("*.py")),
    SRC / "baseline.py",
    SRC / "prefilter.py",
    *sorted((SRC / "agents").glob("**/*.py")),
)
WORLD_WORDS = ("adapters", "ground_truth", "world", "followups", "eval")


def _ast_tokens(path: Path) -> list[str]:
    """Imported module names, imported-from names, and every string literal of a file."""
    out: list[str] = []
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            out.append(n.module or "")
            out += [f"{n.module or ''}.{a.name}" for a in n.names]
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
    return out


def test_no_ast_token_in_agent_facing_code_names_hidden_data_or_adapters() -> None:
    assert len(SCANNED) >= 12
    offenders = []
    for f in SCANNED:
        for tok in _ast_tokens(f):
            if f.parent == TOOLS and tok.startswith("driftgate.tools"):
                continue
            words = set(re.split(r"[^A-Za-z0-9_]+", tok))
            if words & set(WORLD_WORDS):
                offenders.append((f.name, tok[:60]))
    assert offenders == []
