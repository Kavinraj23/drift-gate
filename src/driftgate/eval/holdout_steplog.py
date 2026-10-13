"""Cut one step's section out of a GitHub Actions job log, and prepare it for storage. Pure, offline.

A job log is one text with a timestamp prefix on most lines (`2026-10-01T10:00:06.0000000Z `) and runner markers:
`##[group]TITLE` ... `##[endgroup]` around each step's header, `##[error]` lines for failures. The REST API says which
step failed and when it started and finished (second resolution). The section is chosen like this:

1. Every line gets a timestamp: its own, or that of the line before it (continuation lines carry none).
2. The window is `started_at` (floored to the second) up to and including the second of `completed_at`.
3. Trim bleed from neighbouring steps that share a boundary second: lines before the first runner step header
   (`##[group]Run ...`, `##[group]Post ...`) inside the window are dropped when such a header exists, and the section
   ends before the next runner step header that follows the first one.

Transformations applied to the stored text, and only these (the single list the design note repeats):
- the log is decoded as UTF-8 and one leading byte-order mark is removed;
- ANSI escape sequences are removed (`logtext.strip_ansi`);
- credential-shaped text is redacted (`redaction.redact_lines` per line, then `redact` over the whole text, which must
  then be a fixed point because `holdout-validate` requires it). A section holding a private-key block is refused,
  not stored with the block cut out.
Line endings, timestamps, `##[...]` markers, blank lines and every other character are kept exactly as received.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from driftgate.redaction import redact, redact_lines
from driftgate.tools.logtext import strip_ansi

_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?Z ?")
_STEP_HEADER = re.compile(r"^##\[group\](?:Run |Post )")
_GROUP = "##[group]"
_ENDGROUP = "##[endgroup]"


class StepLogError(Exception):
    """The failed step's section could not be determined. The message is safe to print (no log text)."""


@dataclass(frozen=True)
class FailedStep:
    name: str
    number: int
    started_at: datetime
    completed_at: datetime
    other_failed: int  # further steps that also concluded `failure` (continue-on-error, always())


def parse_api_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def first_failed_step(job: dict[str, Any]) -> FailedStep:
    steps = job.get("steps")
    if not isinstance(steps, list):
        raise StepLogError("the job has no step list")
    failed = [s for s in steps if isinstance(s, dict) and s.get("conclusion") == "failure"]
    if not failed:
        raise StepLogError("the job has no step that concluded 'failure' (cancelled, timed out or rejected?)")
    step = failed[0]
    started, completed = parse_api_time(step.get("started_at")), parse_api_time(step.get("completed_at"))
    name, number = step.get("name"), step.get("number")
    if started is None or completed is None or not isinstance(name, str) or type(number) is not int:
        raise StepLogError("the failed step lacks a name, number or timestamps")
    return FailedStep(name, number, started, completed, len(failed) - 1)


def _line_times(lines: list[str]) -> list[datetime | None]:
    out: list[datetime | None] = []
    last: datetime | None = None
    for line in lines:
        m = _TS.match(line)
        if m:
            last = datetime.fromisoformat(m.group(1)).replace(tzinfo=UTC)
        out.append(last)
    return out


def _body(line: str) -> str:
    return _TS.sub("", line, count=1)


def extract_step_section(log_text: str, step: FailedStep) -> str:
    """The failed step's lines, verbatim (line endings kept). Raises `StepLogError` if nothing can be selected."""
    lines = log_text.splitlines(keepends=True)
    times = _line_times(lines)
    lo = step.started_at.replace(microsecond=0)
    hi = step.completed_at.replace(microsecond=0) + timedelta(seconds=1)
    picked = [i for i, t in enumerate(times) if t is not None and lo <= t < hi]
    if not picked:
        raise StepLogError("no log line falls inside the failed step's time window")
    headers = [i for i in picked if _STEP_HEADER.match(_body(lines[i]).rstrip("\r\n"))]
    start, end = picked[0], picked[-1] + 1
    if headers:
        start = headers[0]
        later = [i for i in headers if i > start]
        if later:
            end = later[0]
    section = "".join(lines[start:end])
    if not section.strip():
        raise StepLogError("the failed step's section is empty")
    return section


def prepare_for_storage(section: str) -> str:
    """ANSI strip, then redact. Raises `StepLogError` if the result is not stable under `redact` (fail closed)."""
    text = strip_ansi(section)
    parts = text.splitlines(keepends=True)
    bodies = [p.rstrip("\r\n") for p in parts]
    endings = [p[len(b) :] for p, b in zip(parts, bodies, strict=True)]
    kept = redact_lines(bodies)
    if len(kept) != len(bodies):  # redact_lines drops private-key blocks; dropping lines is not "verbatim", so refuse
        raise StepLogError("the section contains a private key block; not stored (handle this run by hand)")
    out = "".join(b + e for b, e in zip(kept, endings, strict=True))
    out = redact(out)
    if redact(out) != out:
        raise StepLogError("redaction did not reach a fixed point; refusing to store this log")
    if not out.strip():
        raise StepLogError("nothing is left of the section after redaction")
    return out


def decode_log(raw: bytes) -> str:
    """UTF-8, one leading BOM removed. Undecodable bytes are an error, never silently replaced."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise StepLogError("the log is not valid UTF-8") from None
