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


def _secrets(case: dict) -> list[str]:
    """A secret is a string, or a list of fragments joined here. The newer vendor-shaped fakes are stored split so no
    contiguous token-shaped string is committed: repository push protection cannot tell a fake from a real one."""
    return ["".join(s) if isinstance(s, list) else s for s in case["secrets"]]


def _render(case: dict) -> str:
    text = case["template"]
    for i, s in enumerate(_secrets(case)):
        text = text.replace(f"{{S{i}}}", s)
    return text


VENDOR_PREFIXES = (
    "key-", "SK", "dop_v1_", "shpat_", "dapi", "hvs.", "sntrys_", "lin_api_", "PMAK-", "dp.pt.", "fo1_", "123456789:",
)  # fmt: skip


def test_vendor_shaped_fakes_are_never_stored_contiguously() -> None:
    raw = (Path(__file__).parent / "redaction_corpus.json").read_text(encoding="utf-8")
    split = [c for c in DOC["cases"] if any(isinstance(s, list) for s in c["secrets"])]
    assert len(split) >= 21
    vendor_checked = 0
    for case in split:
        for stored, joined in zip(case["secrets"], _secrets(case), strict=True):
            assert isinstance(stored, list) and len(stored) >= 2 and all(stored), case["id"]
            if joined.startswith(VENDOR_PREFIXES):
                vendor_checked += 1
                assert joined not in raw, case["id"]
    assert vendor_checked >= 10


@pytest.mark.parametrize("case", DOC["cases"], ids=lambda c: c["id"])
def test_secret_removed_and_context_kept(case: dict) -> None:
    out = "\n".join(redact_lines(_render(case).split("\n")))
    for s in _secrets(case):
        for part in s.split("\n"):
            assert part not in out, (case["id"], part)
    assert "[REDACTED]" in out
    for k in case["keep"]:
        assert k in out, (case["id"], k)
    assert redact(out) == out  # idempotent


@pytest.mark.parametrize("case", DOC["cases"], ids=lambda c: c["id"])
def test_secret_removed_through_clean_lines(case: dict) -> None:
    out = "\n".join(clean_lines(_render(case) + "\n"))
    for s in _secrets(case):
        for part in s.split("\n"):
            assert part not in out, (case["id"], part)


@pytest.mark.parametrize("line", DOC["no_false_positives"])
def test_benign_lines_unchanged(line: str) -> None:
    assert redact(line) == line
