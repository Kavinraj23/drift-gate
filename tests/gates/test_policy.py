"""Abort ceiling N from config/policy.json: loading, fail-closed, fleet sizes around the ceiling, audit visibility."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.gates.test_gates import FakeClock, facts, prop, write_switch

from driftgate.audit import AuditLog, record_decision
from driftgate.domain import BlastRadius
from driftgate.gates import KillSwitch, RateLimiter, SafetyGate
from driftgate.orchestrator import FLEET_ABORT_CEILING, build_synthetic_orchestrator
from driftgate.policy import DEFAULT_POLICY_PATH, STRICTEST_ABORT_CEILING, load_policy


def write_policy(path: Path, value: object) -> Path:
    path.write_text(json.dumps({"abort_ceiling": value, "comment": "t"}), encoding="utf-8")
    return path


def test_shipped_policy_file_is_valid_and_matches_the_documented_value() -> None:
    p = load_policy(DEFAULT_POLICY_PATH)
    assert p.source == "file" and p.abort_ceiling == 2 == FLEET_ABORT_CEILING


@pytest.mark.parametrize("n", [0, 2, 10])
def test_valid_values_load(tmp_path: Path, n: int) -> None:
    assert load_policy(write_policy(tmp_path / "p.json", n)).abort_ceiling == n


@pytest.mark.parametrize("bad", ["2", 2.5, -1, True, None, 10**6, [2]])
def test_invalid_values_fail_closed(tmp_path: Path, bad: object) -> None:
    p = load_policy(write_policy(tmp_path / "p.json", bad))
    assert p.abort_ceiling == STRICTEST_ABORT_CEILING and p.source.startswith("fail_closed")


def test_missing_corrupt_and_non_object_files_fail_closed(tmp_path: Path) -> None:
    assert load_policy(tmp_path / "nope.json").abort_ceiling == STRICTEST_ABORT_CEILING
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert load_policy(tmp_path / "bad.json").abort_ceiling == STRICTEST_ABORT_CEILING
    (tmp_path / "list.json").write_text("[2]", encoding="utf-8")
    assert load_policy(tmp_path / "list.json").source.startswith("fail_closed")


def test_fail_closed_ceiling_is_strictest_possible() -> None:
    assert STRICTEST_ABORT_CEILING == 0


def make_gate(tmp_path: Path, ceiling: int) -> SafetyGate:
    sw = tmp_path / "ks.json"
    write_switch(sw)
    return SafetyGate(KillSwitch(sw), RateLimiter(FakeClock()), ceiling)


@pytest.mark.parametrize("ceiling", [2, 10])
@pytest.mark.parametrize("fleet", range(1, 13))
def test_fleet_sizes_around_the_ceiling(tmp_path: Path, ceiling: int, fleet: int) -> None:
    gate = make_gate(tmp_path, ceiling)
    d = gate.decide(prop(0), facts(blast_radius=BlastRadius(fleet, "runner_pool")))
    if fleet > ceiling:
        assert d.decision == "downgraded" and "exceeds ceiling" in d.reason
    else:
        assert d.allowed
    assert d.abort_ceiling == ceiling


def test_ceiling_is_recorded_in_the_audit_decision_record(tmp_path: Path) -> None:
    seen = []
    for ceiling in (2, 10):
        gate = make_gate(tmp_path, ceiling)
        audit = AuditLog(tmp_path / f"a{ceiling}.jsonl", FakeClock())
        d = gate.decide(prop(0), facts(blast_radius=BlastRadius(5, "runner_pool")))
        record_decision(audit, d, "fp1", "e1")
        seen.append(audit.read("gate_decision")[0].payload)
    assert [p["abort_ceiling"] for p in seen] == [2, 10]
    assert [p["decision"] for p in seen] == ["downgraded", "allowed"]  # raising N changes the outcome, visibly


def test_orchestrator_sources_n_from_policy_file(dataset_dir: Path, tmp_path: Path, kill_file: Path) -> None:
    def build(**kw: object):  # type: ignore[no-untyped-def]
        return build_synthetic_orchestrator(
            dataset_dir,
            lambda _e, _b: None,
            audit_path=tmp_path / "a.jsonl",
            clock=FakeClock(),
            kill_switch_path=kill_file,
            **kw,  # type: ignore[arg-type,return-value]
        )

    assert build()._gate._ceiling == 2
    assert build(policy_path=write_policy(tmp_path / "p10.json", 10))._gate._ceiling == 10
    assert build(policy_path=tmp_path / "missing.json")._gate._ceiling == STRICTEST_ABORT_CEILING
    assert build(policy_path=write_policy(tmp_path / "p10.json", 10), abort_ceiling=3)._gate._ceiling == 3
