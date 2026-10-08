"""The only reader of ground-truth labels (invariant 8). Nothing outside eval/ and generator/ may import this."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

LABELS_RELPATH = Path("ground_truth") / "labels.json"
MANIFEST_RELPATH = Path("manifest.json")


@dataclass(frozen=True)
class FailureLabel:
    execution_id: str
    pipeline: str
    fault_id: str
    true_layer: str
    true_classification: str
    disposition: str  # remediate | escalate | close
    correct_tier: int | None
    correct_action: str | None
    tier0_path: str | None
    precedent_exists: bool
    fleet_wide: bool
    burst_id: str | None
    blast_executions_affected: int
    blast_shared_dimension: str
    causal_change_ids: tuple[str, ...]
    decoy_change_ids: tuple[str, ...]
    fix_diff: str | None
    fix_paths: tuple[str, ...]
    first_fix_fails: bool
    error_catalog_ids: tuple[str, ...]
    flaky_pipeline: bool
    scenario_id: str | None


@dataclass(frozen=True)
class GroundTruth:
    failures: dict[str, FailureLabel]
    flaky_pipelines: tuple[str, ...]
    bursts: tuple[dict[str, object], ...]
    held_out_scenarios: frozenset[str] = field(default_factory=frozenset)

    def for_execution(self, execution_id: str) -> FailureLabel | None:
        return self.failures.get(execution_id)

    def scenarios(self, held_out: bool | None = None) -> list[FailureLabel]:
        """Labelled scenarios ordered by scenario id; `held_out` filters to the held-out or dev partition."""
        rows = [f for f in self.failures.values() if f.scenario_id is not None]
        if held_out is not None:
            rows = [f for f in rows if (f.scenario_id in self.held_out_scenarios) == held_out]
        return sorted(rows, key=lambda f: f.scenario_id or "")


def _label(raw: dict) -> FailureLabel:
    return FailureLabel(
        execution_id=raw["execution_id"],
        pipeline=raw["pipeline"],
        fault_id=raw["fault_id"],
        true_layer=raw["true_layer"],
        true_classification=raw["true_classification"],
        disposition=raw["disposition"],
        correct_tier=raw["correct_tier"],
        correct_action=raw["correct_action"],
        tier0_path=raw["tier0_path"],
        precedent_exists=raw["precedent_exists"],
        fleet_wide=raw["fleet_wide"],
        burst_id=raw["burst_id"],
        blast_executions_affected=raw["blast_radius"]["executions_affected"],
        blast_shared_dimension=raw["blast_radius"]["shared_dimension"],
        causal_change_ids=tuple(raw["causal_change_ids"]),
        decoy_change_ids=tuple(raw["decoy_change_ids"]),
        fix_diff=raw["fix_diff"],
        fix_paths=tuple(raw["fix_paths"]),
        first_fix_fails=raw["first_fix_fails"],
        error_catalog_ids=tuple(raw["error_catalog_ids"]),
        flaky_pipeline=raw["flaky_pipeline"],
        scenario_id=raw["scenario_id"],
    )


def load_ground_truth(data_dir: Path = Path("data")) -> GroundTruth:
    raw = json.loads((data_dir / LABELS_RELPATH).read_text(encoding="utf-8"))
    manifest = json.loads((data_dir / MANIFEST_RELPATH).read_text(encoding="utf-8"))
    return GroundTruth(
        failures={k: _label(v) for k, v in raw["failures"].items()},
        flaky_pipelines=tuple(raw["flaky_pipelines"]),
        bursts=tuple(raw["bursts"]),
        held_out_scenarios=frozenset(manifest["held_out_scenarios"]),
    )
