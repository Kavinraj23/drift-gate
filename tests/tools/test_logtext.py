"""Log extraction: ANSI, spinner/retry collapse, redaction, ranking, budget."""

from __future__ import annotations

from driftgate.generator.catalog import load_catalog
from driftgate.tools.logtext import (
    clean_lines,
    collapse_repeats,
    error_blocks,
    redact,
    render_budgeted,
    strip_ansi,
)

CATALOG = load_catalog()
THROTTLE = CATALOG["aws_throttling"].render({"operation": "X", "retries": 2})[0]
TS = "2026-09-08T04:08:44.0390750Z "
BOX_OPEN, BOX_CLOSE = "╷", "╵"


def test_strip_ansi_removes_color_codes() -> None:
    assert strip_ansi("\x1b[31m╷\x1b[0m boom") == "╷ boom"


def test_clean_lines_drops_timestamps_and_keeps_last_carriage_return_frame() -> None:
    raw = f"{TS}downloading 10%\rdownloading 99%\n{TS}done\n"
    assert clean_lines(raw) == ["downloading 99%", "done"]


def test_spinner_frames_collapse() -> None:
    frames = [f"⠋ installing step {i}" for i in range(6)]
    out = collapse_repeats(["before", *frames, "after"])
    assert out[0] == "before" and out[-1] == "after"
    assert len(out) == 4  # before, first frame, summary, after
    assert "repeated 5 more times" in out[2]


def test_repeated_retry_lines_collapse_ignoring_digits() -> None:
    lines = [f"Retrying in {n} seconds ({n}/5)" for n in range(1, 6)]
    out = collapse_repeats(lines)
    assert out[0] == lines[0] and len(out) == 2
    assert "repeated 4 more times" in out[1]


def test_two_repeats_are_not_collapsed() -> None:
    assert collapse_repeats(["a 1", "a 2"]) == ["a 1", "a 2"]


def test_redacts_fake_credential_patterns() -> None:
    secrets = {
        "aws access key": "AKIA" + "IOSFODNN7EXAMPLE",
        "github token": "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
        "github pat": "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnop",
        "slack": "xoxb-" + "123456789012-abcdefABCDEF",
        "jwt": "eyJhbGciOiJI" + "." + "eyJzdWIiOiIxMjM0" + "." + "SflKxwRJSMeKKF2QT4",
        "anthropic": "sk-ant-" + "api03-abcdefghijklmnop1234",
    }
    for name, secret in secrets.items():
        out = redact(f"value is {secret} here")
        assert secret not in out, name
        assert "[REDACTED]" in out, name


def test_redacts_key_value_forms_url_credentials_and_bearer() -> None:
    cases = [
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "password: hunter2hunter2",
        "API_TOKEN=abc123def456",
        "client_secret: s3cr3t-value",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        "https://deploy:p4ssw0rdvalue@registry.example.com/v2/",
    ]
    for line in cases:
        out = redact(line)
        assert "[REDACTED]" in out, line
        for leaked in (
            "hunter2hunter2",
            "abc123def456",
            "s3cr3t-value",
            "p4ssw0rdvalue",
            "wJalrXUtn",
            "abcdefghijklmnopqr",
        ):
            assert leaked not in out, line


def test_redacts_private_key_block() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    assert redact(pem) == "[REDACTED]"


def test_redaction_leaves_ordinary_error_text_alone() -> None:
    line = CATALOG["aws_expired_token"].lines[0]
    assert redact(line) == line


def test_ranking_puts_operative_error_before_the_cascade_and_exit_line() -> None:
    log = "\n".join(
        [
            TS + THROTTLE,
            TS + "starting terraform plan",
            TS + BOX_OPEN,
            TS + "│ Error: Error acquiring the state lock",
            TS + "│ ",
            TS + "│ Error message: ConditionalCheckFailedException: The conditional request failed",
            TS + BOX_CLOSE,
            TS + "##[error]Process completed with exit code 1.",
        ]
    )
    blocks = error_blocks(log)
    assert [b.signature for b in blocks] == ["tf_state_lock", "aws_throttling", "exit_code_nonzero"]
    assert blocks[0].text.startswith(BOX_OPEN) and blocks[0].text.endswith(BOX_CLOSE)
    assert len({b.text for b in blocks}) == 3  # all blocks returned, not just the first


def test_identical_blocks_merge_with_a_count() -> None:
    line = THROTTLE
    blocks = error_blocks("\n".join([line, "ok", "ok2", line]))
    assert len(blocks) == 1 and blocks[0].count == 2


def test_budget_is_enforced_and_marks_truncation() -> None:
    log = "\n".join(f"error number {i}: " + "x" * 80 for i in range(100) if True for _ in [0]).replace(
        "\n", "\nok\nok\n"
    )
    blocks = error_blocks(log)
    assert len(blocks) > 20
    text, truncated = render_budgeted(blocks, 500)
    assert len(text) <= 500 and truncated
    full, not_truncated = render_budgeted(blocks[:2], 10_000)
    assert not not_truncated and len(full) < 10_000


def test_no_error_blocks_for_clean_log() -> None:
    assert error_blocks("all good\nnothing wrong\n") == []
