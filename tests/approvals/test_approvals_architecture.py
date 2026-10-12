"""Static boundaries for N4: agents stay read-only, nothing reaches GitHub, nothing sleeps or touches the network."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "driftgate"
APPROVAL_MODULES = ("approvals", "approval_flow", "approvals_cli", "lock_proof")
NEW_MODULES = ("approvals.py", "approval_flow.py", "approvals_cli.py", "lock_proof.py", "eval/tier12_cases.py")


def imports(path: Path) -> list[str]:
    out: list[str] = []
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            out += [n.module or ""] + [f"{n.module or ''}.{a.name}" for a in n.names]
    return out


@pytest.mark.parametrize("sub", ["agents", "tools"])
def test_agents_and_tools_never_import_approvals_or_the_lock_proof(sub: str) -> None:
    offenders = []
    for p in (SRC / sub).rglob("*.py"):
        for name in imports(p):
            if any(name == f"driftgate.{m}" or name.startswith(f"driftgate.{m}.") for m in APPROVAL_MODULES):
                offenders.append((p.relative_to(SRC).as_posix(), name))
            if "approval" in name.lower() and name.startswith("driftgate"):
                offenders.append((p.relative_to(SRC).as_posix(), name))
    assert offenders == []


def test_agents_and_tools_do_not_even_mention_the_approval_store() -> None:
    hits = []
    for sub in ("agents", "tools"):
        for p in (SRC / sub).rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            if "ApprovalStore" in text or "execute_approved" in text or "claim_for_execution" in text:
                hits.append(p.relative_to(SRC).as_posix())
    assert hits == []


def test_only_the_wiring_modules_import_the_approval_store() -> None:
    importers = set()
    for p in SRC.rglob("*.py"):
        rel = p.relative_to(SRC).as_posix()
        if any(n == "driftgate.approvals" or n.startswith("driftgate.approvals.") for n in imports(p)):
            importers.add(rel)
    assert importers <= {"orchestrator.py", "approval_flow.py", "approvals_cli.py", "eval/tier12_cases.py"}


@pytest.mark.parametrize("name", ["github_actions.py", "github_client.py", "github_live.py", "http_session.py"])
def test_the_github_adapter_has_no_tier1_or_tier2_capability(name: str) -> None:
    text = (SRC / "adapters" / name).read_text(encoding="utf-8")
    names = imports(SRC / "adapters" / name)
    assert not any(any(m in n for m in APPROVAL_MODULES) for n in names)
    for forbidden in ("LockTable", "force_unlock", "ApprovalStore", "lock_proof"):
        assert forbidden not in text, forbidden


def test_the_github_orchestrator_cannot_be_given_an_approval_store() -> None:
    from driftgate.orchestrator import build_github_orchestrator

    params = inspect.signature(build_github_orchestrator).parameters
    assert not {"approvals", "locks", "triggered_by"} & set(params)


def test_approvals_store_is_stdlib_only() -> None:
    for name in imports(SRC / "approvals.py"):
        assert not name.startswith("driftgate"), name


@pytest.mark.parametrize("rel", NEW_MODULES)
def test_new_modules_have_no_sleep_network_or_env_access(rel: str) -> None:
    tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not used & {"sleep", "getenv", "environ", "load_dotenv", "dotenv_values", "urlopen", "socket"}
    assert not any(
        n.split(".")[0] in {"requests", "urllib", "http", "socket", "anthropic", "ssl"} for n in imports(SRC / rel)
    )
