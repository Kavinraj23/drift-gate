"""Prompt/schema fingerprint: one hash over everything that shapes the model's requests.

Recorded replay fixtures are keyed on the full request, so changing the investigator or reviewer system prompt, a
tool description or schema, or `max_tokens` silently orphans them. The fingerprint is stored in every fixture and in
the fixture manifest so replay can say "stale: re-record" instead of just "missing".
"""

from __future__ import annotations

import hashlib
import json

from driftgate.agents import investigator, reviewer


def fingerprint_inputs() -> dict[str, object]:
    return {
        "investigator_system": investigator.SYSTEM_PROMPT,
        "reviewer_system": reviewer.SYSTEM_PROMPT,
        "investigator_tools": investigator.agent_tool_definitions(),
        "reviewer_tools": reviewer.reviewer_tool_definitions(),
        "investigator_max_tokens": investigator.DEFAULT_RESPONSE_TOKENS,
        "reviewer_max_tokens": reviewer.DEFAULT_RESPONSE_TOKENS,
    }


def prompt_fingerprint() -> str:
    canonical = json.dumps(fingerprint_inputs(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
