"""Static boundaries for N4: agents stay read-only, nothing reaches GitHub, nothing sleeps or touches the network."""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "driftgate"
APPROVAL_MODULES = ("approvals", "approval_flow", "approvals_cli", "lock_proof")
NEW_MODULES = ("approvals.py", "approval_flow.py", "approvals_cli.py", "lock_proof.py", "eval/tier12_cases.py")


def imports_of_source(source: str, package: list[str]) -> list[str]:
    """Absolute module names imported by `source`, which lives in `package` (e.g. ["driftgate", "agents"]).
    Relative imports are resolved against the package, so `from ..approvals import X` is `driftgate.approvals.X`."""
    out: list[str] = []
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                keep = len(package) - (n.level - 1)
                if keep < 1:
                    out.append("<beyond top-level package>")
                    continue
                base = package[:keep] + (n.module.split(".") if n.module else [])
            else:
                base = n.module.split(".") if n.module else []
            mod = ".".join(base)
            out += [mod] + [f"{mod}.{a.name}" for a in n.names]
    return out


def imports(path: Path) -> list[str]:
    package = ["driftgate", *path.relative_to(SRC).parts[:-1]]
    return imports_of_source(path.read_text(encoding="utf-8"), package)


_MENTION = re.compile(r"^\s*(?:from|import)\s+[.\w]*\bapprovals\b|^\s*from\s+[.\w]+\s+import\s+.*\bapprovals\b", re.M)


def _is_approval_module(name: str) -> bool:
    return any(name == f"driftgate.{m}" or name.startswith(f"driftgate.{m}.") for m in APPROVAL_MODULES)


@pytest.mark.parametrize(
    "line",
    [
        "from ..approvals import ApprovalStore",
        "from .. import approval_flow",
        "from .. import approvals_cli as c",
        "from ... import lock_proof",
        "from driftgate.approvals import X",
        "from driftgate import approvals",
        "import driftgate.lock_proof",
    ],
)
def test_the_detector_flags_every_import_form(line: str) -> None:
    pkg = ["driftgate", "agents", "deep"] if line.startswith("from ...") else ["driftgate", "agents"]
    names = imports_of_source(line + "\n", pkg)
    assert any(_is_approval_module(n) for n in names), names


def test_the_detector_does_not_flag_unrelated_relative_imports() -> None:
    names = imports_of_source("from ..domain import X\nfrom . import investigator\n", ["driftgate", "agents"])
    assert not any(_is_approval_module(n) for n in names), names


@pytest.mark.parametrize("sub", ["agents", "tools", "llm"])
def test_agents_tools_and_llm_never_import_approvals_or_the_lock_proof(sub: str) -> None:
    offenders = []
    for p in (SRC / sub).rglob("*.py"):
        for name in imports(p):
            if _is_approval_module(name) or ("approval" in name.lower() and name.startswith("driftgate")):
                offenders.append((p.relative_to(SRC).as_posix(), name))
    assert offenders == []


def test_agents_and_tools_do_not_even_mention_the_approval_store() -> None:
    hits = []
    words = ("ApprovalStore", "execute_approved", "claim_for_execution", "approval_flow", "approvals_cli", "lock_proof")
    for sub in ("agents", "tools", "llm"):
        for p in (SRC / sub).rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            if any(w in text for w in words) or _MENTION.search(text):
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
