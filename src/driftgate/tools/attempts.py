"""In-memory store of remediation attempts, injected into tools for re-investigation. No persistence here."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class RemediationAttempt:
    attempt_id: str
    fingerprint: str
    execution_id: str
    tier: int
    action: str
    outcome: str  # e.g. executed | refused | failed
    verification: dict[str, Any] = field(default_factory=dict)  # verified, description, details

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AttemptStore:
    def __init__(self, attempts: list[RemediationAttempt] | None = None) -> None:
        self._attempts: list[RemediationAttempt] = list(attempts or [])

    def add(self, attempt: RemediationAttempt) -> None:
        self._attempts.append(attempt)

    def by_fingerprint(self, fingerprint: str) -> list[RemediationAttempt]:
        return [a for a in self._attempts if a.fingerprint == fingerprint]

    def get(self, attempt_id: str) -> RemediationAttempt | None:
        return next((a for a in self._attempts if a.attempt_id == attempt_id), None)

    def update(self, attempt_id: str, **changes: Any) -> RemediationAttempt:
        """Replace an attempt with a copy carrying `changes` (outcome, verification). The attempt must exist."""
        for i, a in enumerate(self._attempts):
            if a.attempt_id == attempt_id:
                self._attempts[i] = replace(a, **changes)
                return self._attempts[i]
        raise KeyError(attempt_id)

    def all(self) -> list[RemediationAttempt]:
        return list(self._attempts)
