"""Redaction corpus: every fake secret is removed, structure is kept, benign text is untouched.

All secrets in redaction_corpus.json are FAKE (built from repeated patterns in real-world formats).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from driftgate.tools.logtext import clean_lines
from driftgate.tools.redaction import redact, redact_lines

DOC = json.loads((Path(__file__).parent / "redaction_corpus.json").read_text(encoding="utf-8"))


def _render(case: dict) -> str:
    text = case["template"]
    for i, s in enumerate(case["secrets"]):
        text = text.replace(f"{{S{i}}}", s)
    return text


@pytest.mark.parametrize("case", DOC["cases"], ids=lambda c: c["id"])
def test_secret_removed_and_context_kept(case: dict) -> None:
    out = "\n".join(redact_lines(_render(case).split("\n")))
    for s in case["secrets"]:
        for part in s.split("\n"):
            assert part not in out, (case["id"], part)
    assert "[REDACTED]" in out
    for k in case["keep"]:
        assert k in out, (case["id"], k)
    assert redact(out) == out  # idempotent


@pytest.mark.parametrize("case", DOC["cases"], ids=lambda c: c["id"])
def test_secret_removed_through_clean_lines(case: dict) -> None:
    out = "\n".join(clean_lines(_render(case) + "\n"))
    for s in case["secrets"]:
        for part in s.split("\n"):
            assert part not in out, (case["id"], part)


@pytest.mark.parametrize("line", DOC["no_false_positives"])
def test_benign_lines_unchanged(line: str) -> None:
    assert redact(line) == line
