"""The playground scenario files: parseable, dispatch-only, hosted runners, no secrets; and architecture guards."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from miniyaml import YamlError, load

ROOT = Path(__file__).resolve().parents[2]
PLAYGROUND = ROOT / "playground"
SCENARIOS = {
    "flaky": "scenario/flaky",
    "throttle": "scenario/throttle",
    "lockfile": "scenario/lockfile",
    "provider-pin": "scenario/provider-pin",
    "undefined-var": "scenario/undefined-var",
    "approval-rejected": "scenario/approval-rejected",
}
WORKFLOWS = sorted(PLAYGROUND.glob("*/.github/workflows/*.yml"))
HOSTED = re.compile(r"ubuntu-(latest|\d{2}\.\d{2})|windows-latest|macos-latest")


def test_all_six_scenarios_are_present_with_exactly_one_workflow_each() -> None:
    assert {p.parent.parent.parent.name for p in WORKFLOWS} == set(SCENARIOS)
    assert len(WORKFLOWS) == len(SCENARIOS)
    assert (PLAYGROUND / "README.md").exists()
    readme = (PLAYGROUND / "README.md").read_text(encoding="utf-8")
    assert "baseline" in readme and "M8b" in readme and "human step" in readme
    for name, branch in SCENARIOS.items():
        assert f"`{branch}`" in readme and f"`{name}/`" in readme


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.parent.parent.parent.name)
def test_workflow_parses_dispatch_only_hosted_runners_and_no_secrets(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    doc = load(text)
    trigger = doc.get("on", doc.get(True))
    assert isinstance(trigger, dict) and "workflow_dispatch" in trigger
    assert set(trigger) == {"workflow_dispatch"}  # no push / pull_request / schedule triggers
    assert doc.get("permissions") == {"contents": "read"}
    assert "secrets." not in text and "secrets[" not in text and "GITHUB_TOKEN" not in text
    jobs = doc["jobs"]
    assert jobs
    for job in jobs.values():
        assert HOSTED.fullmatch(str(job["runs-on"])), job["runs-on"]
        assert "self-hosted" not in str(job)
        assert job["steps"]
        for step in job["steps"]:
            assert "run" in step or "uses" in step
            if "uses" in step:  # only well-known actions
                assert re.match(r"(actions|opentofu)/[\w.-]+@v\d+$", step["uses"]), step["uses"]


def test_the_scenario_faults_are_present() -> None:
    def text(rel: str) -> str:
        return (PLAYGROUND / rel).read_text(encoding="utf-8")

    assert 'github.run_attempt }}" = "1"' in text("flaky/.github/workflows/flaky.yml")
    throttle = text("throttle/.github/workflows/throttle.yml")
    assert "ThrottlingException" in throttle and "UNVERIFIED" in throttle
    assert 'version = "3.2.1"' in text("lockfile/main.tf") and 'version     = "3.1.0"' in text(
        "lockfile/.terraform.lock.hcl"
    )
    assert 'version = "~> 9.0"' in text("provider-pin/main.tf")
    undefined = text("undefined-var/.github/workflows/undefined-var.yml")
    assert "inputs.relase_tag" in undefined and "release_tag:" in undefined
    assert "environment: needs-approval" in text("approval-rejected/.github/workflows/approval-rejected.yml")


def test_no_credentials_or_cloud_config_in_the_playground_files() -> None:
    for path in PLAYGROUND.rglob("*"):
        if path.is_file():
            body = path.read_text(encoding="utf-8")
            assert not re.search(r"gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY", body), path


def test_the_mini_parser_rejects_what_it_cannot_read() -> None:
    for bad in ("on: [push]\n", "a:\n\tb: 1\n", "a: 1\na: 2\n", "a:\n    b: 1\n  c: 2\n"):
        with pytest.raises(YamlError):
            load(bad)
    assert load("a:\n  - x: 1\n    y: |\n      line\n  - z\n") == {"a": [{"x": 1, "y": "line\n"}, "z"]}


# -- architecture -----------------------------------------------------------------------------------------------
SRC = ROOT / "src" / "driftgate"


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
            names += [f"{node.module}.{a.name}" for a in node.names]
    return names


def test_agents_and_tools_do_not_import_the_github_adapter() -> None:
    for sub in ("agents", "tools", "llm"):
        for p in (SRC / sub).rglob("*.py"):
            bad = [n for n in _imports(p) if "github_actions" in n or "github_client" in n or "adapters" in n]
            assert bad == [], (p.name, bad)


def test_only_the_github_client_imports_requests_nothing_else_does_network_io() -> None:
    """The adapter takes an injected session and never imports an HTTP library at module level."""
    for p in (SRC / "adapters").glob("github_*.py"):
        assert not [n for n in _imports(p) if n.split(".")[0] in ("requests", "urllib3", "httpx", "socket")], p.name
    for p in SRC.rglob("*.py"):
        if "adapters/github" in p.as_posix():
            continue
        assert not [n for n in _imports(p) if "github_client" in n or "github_actions" in n], p.name
