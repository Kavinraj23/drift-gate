"""Redaction edge cases (placeholder collisions) and the boundaries where redaction is wired in."""

from __future__ import annotations

import json
from pathlib import Path

from driftgate.audit import AuditLog
from driftgate.tools.redaction import redact, redact_value

ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:prod/db-creds-AbCdEf"


def test_arn_shield_stops_at_query_delimiters() -> None:
    sig = "0123456789abcdef" * 4
    out = redact(f"arn:aws:s3:::b/k?X-Amz-Signature={sig}&token=abc")
    assert sig not in out and "token=abc" not in out
    assert out.startswith("arn:aws:s3:::b/k?")


def test_arn_itself_is_still_preserved() -> None:
    assert redact(f"on resource: {ARN}") == f"on resource: {ARN}"


def test_input_with_literal_nul_and_digits_does_not_break_restore() -> None:
    text = f"\x000\x00 {ARN} \x001\x00 \x00dg0:0\x00"
    assert redact(text) == text


def test_nul_digits_without_any_arn_is_unchanged() -> None:
    assert redact("a\x007\x00b") == "a\x007\x00b"


def test_secret_still_redacted_next_to_nuls() -> None:
    out = redact(f"\x000\x00 {ARN} password=hunter2hunter2")
    assert "hunter2hunter2" not in out and ARN in out


def test_redact_value_recurses_and_passes_other_types() -> None:
    out = redact_value({"a": ["password=hunter2hunter2", 3], "b": {"c": None}})
    assert out == {"a": ["password=[REDACTED]", 3], "b": {"c": None}}


def test_audit_entries_are_redacted(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "a.jsonl", lambda: 1.0)
    entry = log.append("failed", "fp", "e1", description="boom token=ghp_" + "a" * 36, n=2)
    raw = (tmp_path / "a.jsonl").read_text(encoding="utf-8")
    assert "ghp_" not in raw and "ghp_" not in json.dumps(entry.payload)
    assert entry.payload["n"] == 2


def test_redact_pull_request_covers_title_body_diff_and_files() -> None:
    from driftgate.tier3 import PullRequest, redact_pull_request

    secret = "ghp_" + "a1B2c3D4e5" * 4
    pr = PullRequest(
        "driftgate/x",
        "main",
        f"fix token={secret}",
        f"body password=hunter2hunter2 {secret}",
        f"+ API_TOKEN={secret}\n",
        ("a.txt",),
        files=(("a.txt", f"API_TOKEN={secret}\n"),),
    )
    out = redact_pull_request(pr)
    blob = "|".join([out.title, out.body, out.diff_text, *(c for _, c in out.files)])
    assert secret not in blob and "hunter2hunter2" not in blob
    assert out.branch == pr.branch and out.paths == pr.paths and out.files[0][0] == "a.txt"
