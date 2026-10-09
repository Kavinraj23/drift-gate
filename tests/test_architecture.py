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


LIVE = "eval/live.py"
ENV_READERS = {"llm/config.py", "adapters/github_client.py", LIVE}
ENV_NAMES = {"load_dotenv", "dotenv_values", "getenv", "environ"}


def _rel(p: Path) -> str:
    return p.relative_to(SRC).as_posix()


def _names(path: Path) -> set[str]:
    """Every identifier or attribute name used in code (docstrings and comments excluded)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            found |= {a.name.split(".")[-1] for a in node.names}
            if isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
    return found


def test_only_live_builds_a_live_or_record_gateway() -> None:
    """Every `Gateway(...)` outside the gateway module itself passes the literal mode "replay", except in live.py."""
    offenders = []
    for p in _py_files():
        if _rel(p) in ("llm/gateway.py", LIVE):
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if (isinstance(f, ast.Name) and f.id == "Gateway") or (
                isinstance(f, ast.Attribute) and f.attr == "Gateway"
            ):
                modes = [k.value for k in node.keywords if k.arg == "mode"]
                ok = len(modes) == 1 and isinstance(modes[0], ast.Constant) and modes[0].value == "replay"
                if not ok:
                    offenders.append((_rel(p), node.lineno))
    assert offenders == []


def test_only_tasks_py_references_eval_live() -> None:
    offenders = []
    for p in _py_files():
        if _rel(p) == LIVE:
            continue
        text = p.read_text(encoding="utf-8")
        if "eval.live" in text or "eval import live" in text or any(n.endswith("eval.live") for n in _imports(p)):
            offenders.append(_rel(p))
    assert offenders == []
    tasks = SRC.parent.parent / "tasks.py"
    assert "driftgate.eval.live" in tasks.read_text(encoding="utf-8")  # the one allowed caller really exists


def test_credential_and_env_loading_is_confined() -> None:
    env_offenders = [_rel(p) for p in _py_files() if _rel(p) not in ENV_READERS and _names(p) & ENV_NAMES]
    assert env_offenders == []
    config_offenders = [
        _rel(p) for p in _py_files() if _rel(p) not in ("llm/config.py", LIVE) and "load_config" in _names(p)
    ]
    assert config_offenders == []


def test_audit_does_not_import_tools_or_agents() -> None:
    bad = [n for n in _imports(SRC / "audit.py") if n.startswith(("driftgate.tools", "driftgate.agents"))]
    assert bad == []


CREDS_FILE = "." + "env"


def _creds_file_literals(path: Path) -> list[int]:
    """Line numbers of string literals naming the credentials file (docstrings excluded)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and n.body
        and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
    }
    return sorted(
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in docstrings
        and n.value.replace("\\", "/").rsplit("/", 1)[-1] == CREDS_FILE
    )


def test_credentials_file_is_not_opened_outside_the_confined_modules() -> None:
    offenders = [(_rel(p), _creds_file_literals(p)) for p in _py_files() if _rel(p) not in ENV_READERS]
    assert [o for o in offenders if o[1]] == []


def test_credentials_file_detector_flags_direct_reads(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text(
        f'open("{CREDS_FILE}")\nPath("{CREDS_FILE}").read_text()\nPath("a") / "{CREDS_FILE}"\n"""doc"""\n',
        encoding="utf-8",
    )
    assert _creds_file_literals(f) == [1, 2, 3]


def test_network_is_blocked_in_tests() -> None:
    with pytest.raises(RuntimeError, match="network access is blocked"):
        socket.create_connection(("127.0.0.1", 9))
