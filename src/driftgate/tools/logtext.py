"""Log extraction: ANSI stripping, spinner/retry collapse, credential redaction, error-block ranking, budget."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .redaction import REDACTED as REDACTED  # re-exported
from .redaction import redact as redact  # re-exported
from .redaction import redact_lines
from .signatures import GENERIC_EXIT_ID, line_signature

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s?")
SPINNER_RE = re.compile(r"^\s*[\u2800-\u28ff]\s*\S")
BOX_OPEN = "\u2577"  # terraform diagnostic box top
BOX_CLOSE = "\u2575"  # terraform diagnostic box bottom
ERRORISH_RE = re.compile(
    r"(?i)(##\[error\]|\berror\b|\bERR!|\bfailed\b|\bexception\b|\bdenied\b|exit code|"
    r"\breason:|oomkilled|back-?off|toomanyrequests|\bstate:\s+(?:waiting|terminated))"
)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _normalize_for_repeat(line: str) -> str:
    return re.sub(r"\d+", "#", line.strip())


def clean_lines(raw: str) -> list[str]:
    """ANSI stripped, carriage-return progress reduced to its last frame, timestamps removed, redacted."""
    lines: list[str] = []
    for line in strip_ansi(raw).replace("\r\n", "\n").split("\n"):
        line = line.split("\r")[-1]
        line = TIMESTAMP_RE.sub("", line).rstrip()
        lines.append(line)
    lines = redact_lines(lines)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def collapse_repeats(lines: list[str]) -> list[str]:
    """Collapse runs of spinner frames and consecutive near-identical lines (digits ignored)."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        j = i + 1
        key = _normalize_for_repeat(lines[i])
        spinner = bool(SPINNER_RE.match(lines[i]))
        while (
            j < len(lines)
            and lines[j].strip()
            and (SPINNER_RE.match(lines[j]) if spinner else _normalize_for_repeat(lines[j]) == key)
        ):
            j += 1
        run = j - i
        if run > 2 and lines[i].strip():
            out.append(lines[i])
            out.append(f"[... previous line repeated {run - 1} more times, last: {lines[j - 1].strip()}]")
        else:
            out.extend(lines[i:j])
        i = j
    return out


@dataclass(frozen=True)
class ErrorBlock:
    rank: int
    signature: str | None
    text: str
    line_start: int  # 1-based line in the cleaned, collapsed log
    count: int = 1  # identical blocks merged into this one


def _segments(lines: list[str]) -> list[tuple[int, list[str]]]:
    """Box blocks are one segment each; elsewhere, runs of error-ish lines (one non-error gap allowed)."""
    segs: list[tuple[int, list[str]]] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith(BOX_OPEN):
            j = i
            while j < n and not lines[j].startswith(BOX_CLOSE):
                j += 1
            segs.append((i + 1, lines[i : min(j + 1, n)]))
            i = j + 1
        elif ERRORISH_RE.search(line):
            j = i + 1
            while j < n and not lines[j].startswith(BOX_OPEN):
                if ERRORISH_RE.search(lines[j]):
                    j += 1
                elif j + 1 < n and ERRORISH_RE.search(lines[j + 1]) and not lines[j + 1].startswith(BOX_OPEN):
                    j += 2
                else:
                    break
            segs.append((i + 1, lines[i:j]))
            i = j
        else:
            i += 1
    return segs


def error_blocks(raw: str) -> list[ErrorBlock]:
    """All error blocks of a log, merged when identical, ranked most operative first."""
    lines = collapse_repeats(clean_lines(raw))
    merged: dict[str, ErrorBlock] = {}
    order: list[str] = []
    for start, seg in _segments(lines):
        text = "\n".join(seg)
        if text in merged:
            old = merged[text]
            merged[text] = ErrorBlock(old.rank, old.signature, text, old.line_start, old.count + 1)
            continue
        sig = None
        rank = 1
        for line in seg:
            s = line_signature(line)
            if s is not None and s.rank > rank:
                sig, rank = s, s.rank
        merged[text] = ErrorBlock(rank, sig.id if sig else None, text, start)
        order.append(text)
    blocks = [merged[t] for t in order]
    # Most operative first; the generic exit-code line is a consequence, so it sorts last among equals.
    return sorted(blocks, key=lambda b: (-b.rank, b.signature == GENERIC_EXIT_ID, b.line_start))


def render_budgeted(blocks: list[ErrorBlock], budget: int) -> tuple[str, bool]:
    """Join ranked blocks into at most `budget` characters; returns (text, truncated)."""
    parts: list[str] = []
    used = 0
    truncated = False
    for b in blocks:
        head = f"[block rank={b.rank}" + (f" signature={b.signature}" if b.signature else "")
        head += f" line={b.line_start}" + (f" repeated={b.count}" if b.count > 1 else "") + "]\n"
        chunk = head + b.text + "\n"
        room = budget - used
        if len(chunk) <= room:
            parts.append(chunk)
            used += len(chunk)
            continue
        truncated = True
        marker = "\n[...truncated]"
        if room > len(head) + len(marker) + 20:
            parts.append(chunk[: room - len(marker)] + marker)
        break
    return "".join(parts), truncated
