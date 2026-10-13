"""Cut one step's section out of a GitHub Actions job log, and prepare it for storage. Pure, offline.

A job log is one text with a timestamp prefix on most lines (`2026-10-01T10:00:06.0000000Z `) and runner markers:
`##[group]TITLE` ... `##[endgroup]` around each step's header and `##[error]` lines for failures. The REST API says
which step failed and when it started and finished (second resolution) but the log carries no step names.

EXTRACTION (method label `error-anchor-v1`). The cut is anchored on the log's own structure and CROSS-CHECKED against
the API times; it never falls back to a raw time window. Anything unclear is REFUSED (`StepLogError`, nothing stored,
the human fetches that case by hand):

1. Window: from the failed step's `started_at` (floored to the second) to `completed_at` + 1 second, inclusive.
2. Anchor: the `##[error]` lines whose OWN timestamp falls in the window ("window errors"). None -> refuse. An error
   line without its own timestamp is not counted (it cannot be cross-checked).
3. Start: the step-opening headers (`##[group]Run `, `##[group]Post `, and the runner phases `Set up job`,
   `Initialize containers`, `Stop containers`, `Complete job`) whose own timestamp falls in the window and which come
   before the last window error. EXACTLY ONE is accepted. None -> refuse; more than one -> refuse (a neighbour that
   starts in the same second, a composite action's inner `Run` header, an earlier step's header). A header that is a
   runner phase is refused too, as is a failed step whose API name is a runner phase (their logs differ in shape).
4. Between the start header and the last window error there must be NO other step-opening header at all (timestamped
   or not): a second step, or a nested header, means the anchor cannot be proven to belong to this step -> refuse.
   Ordinary `##[group]TITLE` lines (the tool's own groups) are allowed and counted in the record.
5. Every window error must come after the start header (an earlier one belongs to another step -> refuse).
6. No OTHER failed step may have a window containing the anchor's timestamp (two failing steps in the same second).
7. End: the last window error line, plus one immediately following `##[endgroup]` if present. What follows must be the
   end of the log or a step-opening header; other output after the error line means the step continues -> refuse.

Line numbers are 1-based and count only `\\n` (a CRLF file counts the same; other Unicode line separators do not split).

Transformations applied to the stored text, and only these (the single list the design note repeats):
- the log is decoded as UTF-8 and one leading byte-order mark is removed;
- ANSI escape sequences are removed (`logtext.strip_ansi`);
- credential-shaped text is redacted (`redaction.redact_lines` per line, then `redact` over the whole text, which must
  then be a fixed point because `holdout-validate` requires it); the exact fetch token, if present, is replaced too.
  A section holding a private-key block is refused, not stored with the block cut out.
Line endings, timestamps, `##[...]` markers, blank lines and every other character are kept exactly as received.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from driftgate.redaction import redact, redact_lines
from driftgate.tools.logtext import strip_ansi

METHOD = "error-anchor-v1"
_TS = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(\.[0-9]+)?Z ?", re.ASCII)
_STEP_HEADER = re.compile(r"^##\[group\](?:Run |Post |Set up job|Initialize containers|Stop containers|Complete job)")
_PHASE_HEADER = re.compile(r"^##\[group\](?:Set up job|Initialize containers|Stop containers|Complete job)")
_PHASE_NAMES = frozenset({"Set up job", "Initialize containers", "Stop containers", "Complete job"})
_ERROR = "##[error]"
_GROUP = "##[group]"
_ENDGROUP = "##[endgroup]"
_LINE = re.compile(r"[^\n]*\n|[^\n]+")


class StepLogError(Exception):
    """The failed step's section could not be determined. The message is safe to print (no log text)."""


@dataclass(frozen=True)
class OtherStep:
    name: str
    number: int
    started_at: datetime
    completed_at: datetime


@dataclass(frozen=True)
class FailedStep:
    name: str
    number: int
    started_at: datetime
    completed_at: datetime
    other_failed: tuple[OtherStep, ...] = ()  # further steps that also concluded `failure`


@dataclass(frozen=True)
class Extraction:
    """The cut section (before redaction) and where it came from. Positions are 1-based lines of the job log."""

    text: str
    job_log_lines: int
    start_line: int
    end_line: int
    error_line: int
    error_timestamp: str
    inner_group_lines: int
    method: str = METHOD


def split_lines(text: str) -> list[str]:
    """Lines split on `\\n` only, endings kept (so line numbers match any editor, unlike `str.splitlines`)."""
    return _LINE.findall(text)


def count_lines(text: str) -> int:
    return len(split_lines(text))


def parse_api_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def _step_fields(step: dict[str, Any]) -> tuple[str, int, datetime, datetime]:
    started, completed = parse_api_time(step.get("started_at")), parse_api_time(step.get("completed_at"))
    name, number = step.get("name"), step.get("number")
    if started is None or completed is None or not isinstance(name, str) or type(number) is not int:
        raise StepLogError("a failed step lacks a name, number or timestamps")
    return name, number, started, completed


def first_failed_step(job: dict[str, Any]) -> FailedStep:
    steps = job.get("steps")
    if not isinstance(steps, list):
        raise StepLogError("the job has no step list")
    failed = [s for s in steps if isinstance(s, dict) and s.get("conclusion") == "failure"]
    if not failed:
        raise StepLogError("the job has no step that concluded 'failure' (cancelled, timed out or rejected?)")
    parsed = sorted((_step_fields(s) for s in failed), key=lambda f: f[1])
    name, number, started, completed = parsed[0]
    return FailedStep(name, number, started, completed, tuple(OtherStep(*f) for f in parsed[1:]))


def _line_ts(line: str) -> tuple[datetime, str] | None:
    """The line's OWN timestamp as (UTC datetime, the text as written), or None for a line without one."""
    m = _TS.match(line)
    if not m:
        return None
    frac = m.group(2) or ""
    when = datetime.fromisoformat(m.group(1)).replace(tzinfo=UTC) + timedelta(seconds=float("0" + (frac or ".0")))
    return when, m.group(1) + frac + "Z"


def _body(line: str) -> str:
    return _TS.sub("", line, count=1).rstrip("\r\n")


def _in_window(t: datetime, started: datetime, completed: datetime) -> bool:
    return started.replace(microsecond=0) <= t <= completed + timedelta(seconds=1)


def extract_step_section(log_text: str, step: FailedStep) -> Extraction:
    """The failed step's lines, verbatim (line endings kept) with their position, or `StepLogError` (module doc)."""
    if step.name in _PHASE_NAMES:
        raise StepLogError(f"step {step.name!r} is a runner phase, whose log has no error anchor; fetch it by hand")
    lines = split_lines(log_text)
    stamps = [_line_ts(ln) for ln in lines]
    bodies = [_body(ln) for ln in lines]
    started, completed = step.started_at, step.completed_at

    def stamped_in_window(i: int) -> bool:
        s = stamps[i]
        return s is not None and _in_window(s[0], started, completed)

    errors = [i for i, b in enumerate(bodies) if b.startswith(_ERROR) and stamped_in_window(i)]
    if not errors:
        raise StepLogError("no '##[error]' line with its own timestamp inside the failed step's window")
    first_err, last_err = errors[0], errors[-1]

    headers = [i for i in range(last_err) if _STEP_HEADER.match(bodies[i]) and stamped_in_window(i)]
    if not headers:
        raise StepLogError("no step header inside the window before the error line")
    if len(headers) > 1:
        raise StepLogError(
            f"{len(headers)} step headers fall inside the window before the error line "
            "(a neighbouring step in the same second, or a nested composite header)"
        )
    start = headers[0]
    if _PHASE_HEADER.match(bodies[start]):
        raise StepLogError("the step header is a runner phase header; fetch it by hand")
    if first_err < start:
        raise StepLogError("an '##[error]' line in the window comes before the step header (another step's)")
    if any(_STEP_HEADER.match(bodies[i]) for i in range(start + 1, last_err)):
        raise StepLogError("another step header sits between the step header and the error line")
    last_stamp = stamps[last_err]
    assert last_stamp is not None  # guaranteed by stamped_in_window
    for other in step.other_failed:
        if _in_window(last_stamp[0], other.started_at, other.completed_at):
            raise StepLogError(f"failed step {other.name!r} has a window that also contains the error line")

    end = last_err + 1
    if end < len(lines) and bodies[end] == _ENDGROUP:
        end += 1
    rest = next((b for b in bodies[end:] if b.strip()), None)
    if rest is not None and not _STEP_HEADER.match(rest):
        raise StepLogError("output continues after the error line and is not a new step; fetch it by hand")
    return Extraction(
        text="".join(lines[start:end]),
        job_log_lines=len(lines),
        start_line=start + 1,
        end_line=end,
        error_line=last_err + 1,
        error_timestamp=last_stamp[1],
        inner_group_lines=sum(1 for i in range(start + 1, end) if bodies[i].startswith(_GROUP)),
    )


def prepare_for_storage(section: str, token: str | None = None) -> str:
    """ANSI strip, mask the exact token, then redact. Raises `StepLogError` if not stable under `redact`."""
    text = strip_ansi(section)
    if token:
        text = text.replace(token, "[REDACTED]")
    parts = split_lines(text)
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
