"""Deterministic error signatures keyed on text patterns of the error-string catalog families.

Each pattern is a substring/regex of text that appears in the catalog entry of the same id; nothing
here is a free-text error message. `rank` orders blocks and matches: higher means more operative
(a terminal state-lock error outranks the throttling that preceded it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

GENERIC_EXIT_ID = "exit_code_nonzero"


@dataclass(frozen=True)
class Signature:
    id: str
    layer: str
    classification: str
    tier0_rule: bool  # an authored Tier 0 known-transient rule exists for this signature
    rank: int
    pattern: re.Pattern[str]

    def matches(self, text: str) -> bool:
        return self.pattern.search(text) is not None


def _sig(id_: str, layer: str, klass: str, rule: bool, rank: int, pattern: str) -> Signature:
    return Signature(id_, layer, klass, rule, rank, re.compile(pattern))


SIGNATURES: tuple[Signature, ...] = (
    _sig("tf_state_lock", "L4", "platform", False, 90, r"Error acquiring the state lock"),
    _sig("aws_expired_token", "L2", "platform", False, 80, r"\(ExpiredToken\) when calling"),
    _sig("k8s_oom_killed", "L1", "platform", False, 80, r"Reason:\s+OOMKilled"),
    _sig(
        "tf_provider_constraints",
        "L4",
        "user",
        False,
        70,
        r"Failed to query available provider packages|no available releases match the given constraints",
    ),
    _sig("tf_undeclared_variable", "L3", "user", False, 70, r"Reference to undeclared input variable"),
    _sig(
        "npm_ci_lock_out_of_sync",
        "L4",
        "user",
        False,
        70,
        r"`npm ci` can only install packages when your package\.json and package-lock\.json",
    ),
    _sig(
        "pip_no_matching_distribution",
        "L4",
        "user",
        False,
        70,
        r"No matching distribution found for|Could not find a version that satisfies the requirement",
    ),
    _sig(
        "docker_pull_rate_limit", "L2", "transient", True, 60, r"toomanyrequests: You have reached your pull rate limit"
    ),
    _sig("k8s_image_pull_backoff", "L1", "transient", False, 60, r"Reason:\s+ImagePullBackOff|Back-off pulling image"),
    _sig("aws_throttling", "L4", "transient", True, 50, r"\(ThrottlingException\) when calling"),
    # A bare non-zero exit says only that a step failed; it carries no cause.
    _sig(GENERIC_EXIT_ID, "L4", "unknown", False, 10, r"Process completed with exit code \d+"),
)

# No pattern matched: the failure has no deterministic signature.
UNKNOWN = Signature("unknown_signature", "L4", "unknown", False, 0, re.compile(r"(?!x)x"))

BY_ID: dict[str, Signature] = {s.id: s for s in SIGNATURES}


def match_all(text: str) -> list[Signature]:
    """Every signature present in `text`, most operative first (stable by catalog order within a rank)."""
    hits = [s for s in SIGNATURES if s.matches(text)]
    return sorted(hits, key=lambda s: -s.rank)


def line_signature(line: str) -> Signature | None:
    hits = match_all(line)
    return hits[0] if hits else None
