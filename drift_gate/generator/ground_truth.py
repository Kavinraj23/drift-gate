"""The GroundTruth type - deliberately kept out of drift_gate/domain.py.

Nothing in drift_gate.protocols or drift_gate.domain ever returns a GroundTruth. It is
only reachable by loading ground_truth.json directly, which only the Phase 6 eval
harness is allowed to do - see docs in synthetic/source.py.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from drift_gate.domain import Classification, Remediation


@dataclass(frozen=True)
class GroundTruth:
    execution_id: str
    injected_fault_id: str
    correct_classification: Classification
    correct_remediation: Remediation | None
    is_duplicate_of: str | None
    is_flake: bool

    def to_json(self) -> dict:
        d = {
            "execution_id": self.execution_id,
            "injected_fault_id": self.injected_fault_id,
            "correct_classification": self.correct_classification.value,
            "correct_remediation": (
                None
                if self.correct_remediation is None
                else {
                    "tier": self.correct_remediation.tier.value,
                    "action": self.correct_remediation.action,
                    "rationale": self.correct_remediation.rationale,
                    "reversible": self.correct_remediation.reversible,
                    "gate": self.correct_remediation.gate.value,
                    "context": self.correct_remediation.context,
                }
            ),
            "is_duplicate_of": self.is_duplicate_of,
            "is_flake": self.is_flake,
        }
        return d
