"""Shared Tier 3 test helpers: the labelled Tier 3 scenarios and a runner that wires the reviewer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from driftgate.adapters.synthetic import SyntheticSource, SyntheticTarget
from driftgate.audit import AuditEntry, AuditLog
from driftgate.eval.e2e import _scripted_factory
from driftgate.eval.ground_truth import FailureLabel, GroundTruth
from driftgate.eval.tier3_scripts import scripted_reviewer
from driftgate.llm.types import ModelClient, ModelRequest, ModelResponse
from driftgate.orchestrator import Orchestrator, Outcome
from driftgate.tier3 import ReviewerHook


class Spy:
    """Wraps a model and records every request it receives."""

    def __init__(self, inner: ModelClient) -> None:
        self.inner = inner
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.inner.complete(request)


@dataclass
class Run:
    orch: Orchestrator
    outcome: Outcome
    target: SyntheticTarget
    hook: ReviewerHook
    reviewer_spies: list[Spy] = field(default_factory=list)
    investigator_calls: int = 0
    offset: int = 0  # audit entries that belong to earlier runs in the same test

    @property
    def audit(self) -> list[AuditEntry]:
        return self.orch.audit.read()[self.offset :]

    def stage(self, stage: str) -> list[AuditEntry]:
        return [e for e in self.audit if e.payload.get("stage") == stage]


RunT3 = Callable[..., Run]


@pytest.fixture(scope="session")
def t3_labels(truth: GroundTruth) -> list[FailureLabel]:
    """Every scenario whose correct answer is a Tier 3 PR."""
    rows = [s for s in truth.scenarios() if s.correct_tier == 3 and s.disposition == "remediate"]
    assert len(rows) == 9
    return rows


@pytest.fixture
def run_t3(make_orch: Callable[..., Orchestrator], dataset_dir: Path, tmp_path: Path) -> RunT3:
    """run_t3(label, variant="correct", reviewer="strict", reviewer_model=None, hook=True, **orchestrator kwargs)."""
    source = SyntheticSource(dataset_dir)

    def _run(
        label: FailureLabel,
        variant: str = "correct",
        reviewer: str = "strict",
        *,
        reviewer_model: Callable[[], ModelClient] | None = None,
        **kw: Any,
    ) -> Run:
        target = kw.pop("target", None) or SyntheticTarget()
        offset = len(AuditLog(tmp_path / "audit.jsonl", lambda: 0.0).read())
        spies: list[Spy] = []
        counter = {"n": 0}
        inner = _scripted_factory(label, source, variant)

        def investigators(eid: str, budget: Any) -> ModelClient:
            counter["n"] += 1
            return inner(eid, budget)

        def reviewers(_eid: str, _budget: Any) -> ModelClient:
            spy = Spy(reviewer_model() if reviewer_model else scripted_reviewer(label, source, reviewer))
            spies.append(spy)
            return spy

        hook = ReviewerHook(source, reviewers)
        orch = make_orch(label, model_factory=investigators, target=target, tier3_review=hook, **kw)
        outcome = orch.handle(label.execution_id)
        return Run(orch, outcome, target, hook, spies, counter["n"], offset)

    return _run
