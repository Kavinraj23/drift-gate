"""Fixtures for the N4 approval tests: a store on a manual clock and a world per tier (orchestrator + lock table)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from driftgate.approvals import ApprovalStore
from driftgate.eval.ground_truth import FailureLabel, GroundTruth
from driftgate.eval.tier12_cases import Holder, ManualClock, World, build_world, pick_label


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def store(tmp_path: Path, clock: ManualClock) -> ApprovalStore:
    s = ApprovalStore(tmp_path / "state" / "approvals.sqlite", clock)
    yield s  # type: ignore[misc]
    s.close()


@pytest.fixture(scope="session")
def tier1_label(truth: GroundTruth) -> FailureLabel:
    return pick_label(truth, 1)


@pytest.fixture(scope="session")
def tier2_label(truth: GroundTruth) -> FailureLabel:
    return pick_label(truth, 2)


@pytest.fixture
def make_world(dataset_dir: Path, tmp_path: Path) -> Callable[..., World]:
    worlds: list[World] = []

    def _make(label: FailureLabel, holder: Holder | None = None, confidence: float = 0.9) -> World:
        work = tmp_path / f"world{len(worlds)}"
        work.mkdir()
        w = build_world(label, dataset_dir, work, holder=holder, confidence=confidence)
        worlds.append(w)
        return w

    yield _make  # type: ignore[misc]
    for w in worlds:
        w.close()
