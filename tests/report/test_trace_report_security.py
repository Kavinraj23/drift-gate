"""N5 security: every untrusted value is redacted and escaped; the page has no script and loads nothing."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import pytest
from report_helpers import inspect

from driftgate.audit import AuditEntry
from driftgate.report import build_pages, group_traces, render_trace
from driftgate.report.__main__ import main
from driftgate.report.render import esc, file_name

TOKEN_BODY = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
FAKE_TOKEN = "ghp_" + TOKEN_BODY  # shaped like a credential; not a real one
BIDI_AND_CONTROLS = "‮‬⁦⁩​﻿\x00\x1b"
HOSTILE = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "javascript:alert(document.cookie)",
    '<a href="javascript:alert(1)">click</a>',
    "'><svg/onload=alert(1)>",
    "</style><script>x</script>",
    "&lt;script&gt; &#x3c;script&#x3e;",
    f"{BIDI_AND_CONTROLS}evil",
    "x" * 100_000,
    f"token={FAKE_TOKEN}",
    FAKE_TOKEN,
]
ALLOWED_TAGS = {
    "html", "head", "meta", "title", "style", "body", "header", "h1", "h2", "h3", "nav", "a", "section",
    "p", "dl", "dt", "dd", "div", "table", "thead", "tbody", "tr", "th", "td", "blockquote", "span", "footer",
}  # fmt: skip
Make = Callable[..., AuditEntry]


def hostile_trace(make: Make, text: str) -> tuple[list[AuditEntry], dict[str, object]]:
    entries = [
        make(
            "proposal",
            "agent",
            execution_id=text,
            tool_call_ids=[text, "c1"],
            tools=[text, f"read_repo_file: {text}"],
            hypothesis=text,
            action=text,
            tier=text,
            escalation_reason=text,
            stop_reason=text,
            evidence_kept=text,
        ),
        make("proposal", "gate_facts", execution_id=text, facts={text: text}),
        make("gate_decision", execution_id=text, decision=text, reason=text, action=text, request_id=text),
        make("proposal", "approval_requested", execution_id=text, requested_by=text, request_id=1, expires_at=text),
        make("proposal", "approval_decision", execution_id=text, approver=text, decision=text, status=text),
        make("rejected", "approval_refused", execution_id=text, approver=text, error=text, reason=text),
        make("execution", "execute", execution_id=text, description=text, ok=text),
        make("verification", "result", execution_id=text, description=text, verified=text),
        make("rejected", "gate", execution_id=text, reason=text),
        make("proposal", "reinvestigation_start", execution_id=text, action=text),
    ]
    report = {
        "execution_id": text,
        "fingerprint": text,
        "classification": text,
        "layer": text,
        "confidence": text,
        "hypothesis": text,
        "escalation_reason": text,
        "evidence": [{"source": text, "finding": text, "supports": text}, {"source": "c1", "finding": text}],
        "remediation": {"tier": text, "action": text, "rationale": text, "gate_decision": text},
        "suspected_change": {"kind": text, "ref": text},
        "run": {text: text},
        "prior_attempts": [{"x": text}],
    }
    return entries, {text: report}


@pytest.mark.parametrize("text", HOSTILE, ids=[f"hostile{i}" for i in range(len(HOSTILE))])
def test_hostile_strings_never_reach_the_page_unescaped(make: Make, text: str) -> None:
    entries, reports = hostile_trace(make, text)
    pages = build_pages(entries, reports)  # type: ignore[arg-type]
    assert len(pages) == 2
    for name, out in pages.items():
        insp = inspect(out)
        assert set(insp.tags) <= ALLOWED_TAGS, (name, set(insp.tags) - ALLOWED_TAGS)
        for tag, attr, value in insp.attrs:
            assert not attr.startswith("on"), (name, tag, attr)
            assert "javascript:" not in value.lower() and "<" not in value
            if attr == "href":
                assert re.fullmatch(r"#[a-z0-9-]+|trace-[A-Za-z0-9._-]+\.html", value), value
        lowered = out.lower()
        assert "<script" not in lowered and "<img" not in lowered and "<svg" not in lowered
        assert FAKE_TOKEN not in out and TOKEN_BODY not in out
        for ch in BIDI_AND_CONTROLS:
            assert ch not in out, (name, hex(ord(ch)))
        assert len(out) < 400_000  # very long values are capped per field, not copied whole


def test_script_text_is_shown_as_inert_text(make: Make) -> None:
    text = "<script>alert(1)</script>"
    entries, reports = hostile_trace(make, text)
    out = build_pages(entries, reports)[file_name(text)]  # type: ignore[arg-type]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out and "<script>" not in out


def test_long_values_are_truncated_with_a_visible_marker() -> None:
    assert str(esc("y" * 5000)).endswith("...[3000 more characters not shown]")


def test_bidi_and_control_characters_are_shown_as_escapes() -> None:
    assert str(esc("a‮b\x00c")) == "a\\u202eb\\u0000c"


def test_credential_shapes_are_redacted_before_rendering_even_if_the_log_kept_them(make: Make) -> None:
    # The audit writer redacts, but a log edited or produced elsewhere may not have been.
    e = make("proposal", "agent", hypothesis=f"password=hunter2hunter2 and {FAKE_TOKEN}", tools=[], tool_call_ids=[])
    out = render_trace(group_traces([e])[0])
    assert FAKE_TOKEN not in out and "hunter2hunter2" not in out and "[REDACTED]" in out


@pytest.mark.parametrize("eid", ["../../etc/passwd", "a/b\\c", "x" * 500, "", "ex 1;rm -rf", "e‮.html", "CON"])
def test_file_names_are_safe_and_distinct(eid: str) -> None:
    name = file_name(eid)
    assert "/" not in name and "\\" not in name and ".." not in name
    assert name.startswith("trace-") and name.endswith(".html") and len(name) < 100
    assert set(name) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    assert file_name(eid + "x") != name


def test_hostile_execution_ids_cannot_escape_the_output_directory(make: Make, tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    entry = make("rejected", "prefilter", execution_id="../../evil", reason="r")
    audit.write_text(json.dumps(asdict(entry)) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    assert main(["--audit", str(audit), "--out", str(out)]) == 0
    assert sorted(p.parent for p in tmp_path.rglob("*.html")) == [out, out]
    assert not (tmp_path.parent / "evil").exists()
