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
