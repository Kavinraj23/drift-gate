"""Test configuration: every test runs with real network access blocked."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest


class NetworkBlockedError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(*args: object, **kwargs: object) -> None:
        raise NetworkBlockedError("network access is blocked in tests")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


@pytest.fixture(scope="session")
def dataset_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The default seeded synthetic dataset, written once per session to a temp directory."""
    from driftgate.generator.population import generate
    from driftgate.generator.writer import write_dataset

    out = tmp_path_factory.mktemp("dataset") / "data"
    write_dataset(generate(), out)
    return out


# --- M5 helpers: scenarios, orchestrator factory, report schema -------------------------------------------------
import json  # noqa: E402
from collections.abc import Callable  # noqa: E402

from jsonschema import Draft202012Validator  # noqa: E402
from referencing import Registry, Resource  # noqa: E402

from driftgate.adapters.synthetic import SyntheticSource  # noqa: E402
from driftgate.eval.e2e import StepClock  # noqa: E402
from driftgate.eval.ground_truth import FailureLabel, GroundTruth, load_ground_truth  # noqa: E402
from driftgate.eval.scripted_agent import scripted_model  # noqa: E402
from driftgate.orchestrator import Orchestrator, build_synthetic_orchestrator  # noqa: E402

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"
KILL_ON = {"global": True, "tiers": {"0": True, "1": True, "2": True, "3": True}}


@pytest.fixture(scope="session")
def truth(dataset_dir: Path) -> GroundTruth:
    return load_ground_truth(dataset_dir)


@pytest.fixture(scope="session")
def pick(truth: GroundTruth) -> Callable[..., FailureLabel]:
    """pick(fault_id, fleet=None, nth=0): a labelled scenario by fault family."""

    def _pick(fault_id: str, fleet: bool | None = None, nth: int = 0) -> FailureLabel:
        rows = [s for s in truth.scenarios() if s.fault_id == fault_id and (fleet is None or s.fleet_wide == fleet)]
        return rows[nth]

    return _pick


@pytest.fixture
def kill_file(tmp_path: Path) -> Path:
    path = tmp_path / "kill_switch.json"
    path.write_text(json.dumps(KILL_ON), encoding="utf-8")
    return path


@pytest.fixture
def make_orch(dataset_dir: Path, tmp_path: Path, kill_file: Path) -> Callable[..., Orchestrator]:
    source = SyntheticSource(dataset_dir)

    def _make(label: FailureLabel, variant: str = "correct", *, model_factory=None, **kw) -> Orchestrator:  # type: ignore[no-untyped-def]
        factory = model_factory or (lambda _e, _b: scripted_model(label, source, variant))
        kw.setdefault("kill_switch_path", kill_file)
        kw.setdefault("clock", StepClock())
        return build_synthetic_orchestrator(dataset_dir, factory, audit_path=tmp_path / "audit.jsonl", **kw)

    return _make


@pytest.fixture(scope="session")
def report_validator() -> Draft202012Validator:
    remediation = json.loads((CONTRACTS / "remediation.schema.json").read_text(encoding="utf-8"))
    report = json.loads((CONTRACTS / "report.schema.json").read_text(encoding="utf-8"))
    registry = Registry().with_resource("remediation.schema.json", Resource.from_contents(remediation))
    return Draft202012Validator(report, registry=registry)
