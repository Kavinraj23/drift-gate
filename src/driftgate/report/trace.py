"""Group an audit log into per-investigation traces and derive the facts a report shows.

Nothing here renders HTML. Inputs are untrusted (log text, hypotheses, identities): this module only reads and
groups them; `render` escapes and redacts everything it prints.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from driftgate.audit import AuditEntry

NOT_RECORDED = "not recorded"


@dataclass(frozen=True)
class Trace:
    """Everything recorded about one execution id: its audit entries in seq order, plus the saved report JSON
    (`Report.to_dict()`) when one is available. The audit log stores counts, not evidence items, so the report
    is where evidence text comes from."""

    execution_id: str
    entries: tuple[AuditEntry, ...]
    report: Mapping[str, Any] | None = None

    @property
    def fingerprint(self) -> str:
        for e in self.entries:
            if e.fingerprint:
                return e.fingerprint
        fp = (self.report or {}).get("fingerprint")
        return fp if isinstance(fp, str) else ""


@dataclass(frozen=True)
class ToolCallView:
    round: int
    index: int
    call_id: str
    summary: str  # the audit's one-line trace for that call, or "" when the log has none

    @property
    def anchor(self) -> str:
        return f"tc-{self.round}-{self.index}"


@dataclass(frozen=True)
class RoundView:
    number: int
    entry: AuditEntry
    tool_calls: tuple[ToolCallView, ...]


@dataclass(frozen=True)
class OutcomeView:
    kind: str
    label: str
    detail: str = ""


OUTCOME_LABELS = {
    "closed": "Closed by the pre-filter",
    "executed": "Executed (no verification recorded)",
    "verified": "Executed and verified",
    "verification_failed": "Executed, verification failed",
    "pr_proposed": "Pull request proposed (a human reviews and merges)",
    "pr_ci_failed": "Pull request proposed, CI failed (awaiting a human)",
    "pr_verified": "Pull request proposed, verified by CI (a human still reviews and merges)",
    "awaiting_approval": "Awaiting human approval",
    "approved": "Approved, awaiting execution",
    "denied": "Denied by an approver",
    "expired": "Approval window expired: escalated to a human",
    "escalated": "Escalated to a human",
    "failed": "Failed",
    "unknown": NOT_RECORDED,
}


def _int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value and abs(value) < 1e15:
        return int(value)
    return default


def _entry_from(raw: object) -> AuditEntry | None:
    if not isinstance(raw, dict):
        return None
    payload = raw.get("payload")
    ts = raw.get("ts")
    return AuditEntry(
        seq=_int(raw.get("seq")),
        ts=float(ts) if isinstance(ts, int | float) and not isinstance(ts, bool) else 0.0,
        kind=str(raw.get("kind", "")),
        fingerprint=str(raw.get("fingerprint", "")),
        execution_id=str(raw.get("execution_id", "")),
        payload=payload if isinstance(payload, dict) else {"_raw": payload},
    )


def parse_audit(text: str) -> tuple[list[AuditEntry], int]:
    """Entries from JSONL text, tolerant of damage: a line that is not a JSON object is skipped and counted."""
    entries: list[AuditEntry] = []
    skipped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = _entry_from(json.loads(line))
        except (ValueError, RecursionError):
            entry = None
        if entry is None:
            skipped += 1
        else:
            entries.append(entry)
    return entries, skipped


def load_audit(path: Path | str) -> tuple[list[AuditEntry], int]:
    """Read an audit JSONL file (read-only). Returns the entries and the number of unreadable lines."""
    return parse_audit(Path(path).read_text(encoding="utf-8", errors="replace"))


def load_reports(path: Path | str) -> dict[str, Mapping[str, Any]]:
    """Saved reports as `{execution_id: Report.to_dict()}` (a JSON object). Anything else yields no reports."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def group_traces(entries: Iterable[AuditEntry], reports: Mapping[str, Mapping[str, Any]] | None = None) -> list[Trace]:
    """One trace per execution id, ordered by execution id. A report with no audit entries still gets a trace."""
    by_exec: dict[str, list[AuditEntry]] = defaultdict(list)
    for e in entries:
        by_exec[e.execution_id].append(e)
    for eid in reports or {}:
        by_exec.setdefault(eid, [])
    return [
        Trace(eid, tuple(sorted(rows, key=lambda e: (e.seq, e.ts))), (reports or {}).get(eid))
        for eid, rows in sorted(by_exec.items())
    ]


def stage_of(e: AuditEntry) -> str:
    s = e.payload.get("stage")
    return s if isinstance(s, str) else ""


def with_stage(trace: Trace, kind: str, *stages: str) -> list[AuditEntry]:
    return [e for e in trace.entries if e.kind == kind and (not stages or stage_of(e) in stages)]


def rounds(trace: Trace) -> list[RoundView]:
    """Investigation rounds: one per `proposal`/`agent` entry (round 2 exists after a re-investigation)."""
    out: list[RoundView] = []
    for n, e in enumerate(with_stage(trace, "proposal", "agent"), start=1):
        ids = e.payload.get("tool_call_ids")
        lines = e.payload.get("tools")
        ids = ids if isinstance(ids, list) else []
        lines = lines if isinstance(lines, list) else []
        calls = tuple(
            ToolCallView(n, i, str(cid), str(lines[i - 1]) if i - 1 < len(lines) else "")
            for i, cid in enumerate(ids, start=1)
        )
        out.append(RoundView(n, e, calls))
    return out


def evidence_items(trace: Trace) -> list[dict[str, Any]]:
    ev = (trace.report or {}).get("evidence")
    return [x for x in ev if isinstance(x, dict)] if isinstance(ev, list) else []


# Every (kind, stage) the code base writes, classified once. "terminal" entries can set the displayed outcome (when
# `_apply` recognises their payload); everything else, and ANYTHING NOT LISTED, is informational: it appears in the
# audit trail but never changes the outcome. Refused, no-op and duplicate events are informational by decision: a
# refused approve/deny, an execute attempt on a request that is not approved (or already executed), a re-run of the
# gate, a skipped history read must not rewrite what the case actually did. Source of each: orchestrator.py,
# approval_flow.py, verify.py.
TERMINAL = "terminal"
INFORMATIONAL = "informational"
STAGE_EFFECT: dict[tuple[str, str], str] = {
    # terminal: the case's state really changed
    ("rejected", "prefilter"): TERMINAL,  # closed by the pre-filter
    ("rejected", "approval_denied"): TERMINAL,  # the request was denied and closed
    ("rejected", "approval_request"): TERMINAL,  # no request opened: escalated
    ("rejected", "reinvestigation"): TERMINAL,  # orchestrator._escalate stages
    ("rejected", "facts"): TERMINAL,
    ("rejected", "validation"): TERMINAL,
    ("rejected", "gate"): TERMINAL,
    ("rejected", "review"): TERMINAL,
    ("rejected", "diff_check"): TERMINAL,
    ("rejected", "agent"): TERMINAL,
    ("rejected", "verification_escalation"): TERMINAL,  # verify._hard_escalate
    ("expired", "approval"): TERMINAL,
    ("failed", "dry_run"): TERMINAL,
    ("failed", "execute"): TERMINAL,
    ("failed", "pr_proposed"): TERMINAL,
    ("execution", "pr_proposed"): TERMINAL,
    ("execution", "execute"): TERMINAL,  # only when ok is true
    ("verification", "closed"): TERMINAL,
    ("verification", "result"): TERMINAL,  # only when verified is false (true is followed by `closed`)
    ("proposal", "approval_requested"): TERMINAL,
    ("proposal", "approval_decision"): TERMINAL,  # only when the request became approved
    # informational: written without changing the case's state
    ("rejected", "approval_refused"): INFORMATIONAL,  # a refused approve/deny; the request stays as it was
    (
        "rejected",
        "approval_execute_refused",
    ): INFORMATIONAL,  # execute on a pending/executed/cancelled request, or a refusal
    ("gate_decision", "approval_precheck"): INFORMATIONAL,
    ("gate_decision", "approval_recheck"): INFORMATIONAL,
    ("gate_decision", ""): INFORMATIONAL,
    ("proposal", "agent"): INFORMATIONAL,
    ("proposal", "gate_facts"): INFORMATIONAL,
    ("proposal", "holder_proof"): INFORMATIONAL,
    ("proposal", "history_skipped"): INFORMATIONAL,
    ("proposal", "tier3_diff"): INFORMATIONAL,
    ("proposal", "tier3_review"): INFORMATIONAL,
    ("proposal", "tier3_revision"): INFORMATIONAL,
    ("proposal", "reinvestigation_start"): INFORMATIONAL,
    ("execution", "dry_run"): INFORMATIONAL,
    ("verification", "rollback"): INFORMATIONAL,
    ("budget_truncated", ""): INFORMATIONAL,
}


def changes_outcome(e: AuditEntry) -> bool:
    """True only for a (kind, stage) the table lists as terminal. Unknown stages are informational."""
    return STAGE_EFFECT.get((e.kind, stage_of(e)), INFORMATIONAL) == TERMINAL


def derive_outcome(trace: Trace) -> OutcomeView:
    """The final state, from the audit entries alone (the log has no single 'outcome' entry): the last terminal event
    wins, which is right for the two-round case where a failed first attempt is followed by a second one. Only
    entries `STAGE_EFFECT` lists as terminal count; refused, no-op, duplicate and unknown entries never change it."""
    kind, detail = "unknown", ""
    for e in trace.entries:
        if not changes_outcome(e):
            continue
        st, p = stage_of(e), e.payload
        reason = str(p.get("reason", p.get("description", "")))
        if e.kind == "rejected":
            if st == "prefilter":
                kind, detail = "closed", reason
            elif st == "approval_denied":
                kind, detail = "denied", reason
            else:
                kind, detail = "escalated", f"{st}: {reason}"
        elif e.kind == "expired":
            kind, detail = "expired", reason
        elif e.kind == "failed":
            kind, detail = "failed", f"{st}: {reason}"
        elif e.kind == "execution" and st == "pr_proposed":
            kind, detail = "pr_proposed", f"branch {p.get('branch', NOT_RECORDED)}"
        elif e.kind == "execution" and st == "execute":
            if p.get("ok") is True:
                kind, detail = "executed", reason
        elif e.kind == "verification" and st == "closed":
            kind, detail = ("pr_verified" if kind == "pr_proposed" else "verified"), ""
        elif e.kind == "verification" and st == "result":
            if p.get("verified") is False:
                # A Tier 3 PR opens nothing on a target: a failed check is its CI failing, awaiting a human.
                kind, detail = ("pr_ci_failed" if kind == "pr_proposed" else "verification_failed"), reason
        elif e.kind == "proposal" and st == "approval_requested":
            kind, detail = "awaiting_approval", f"request {p.get('request_id', '?')}"
        elif e.kind == "proposal" and st == "approval_decision":
            if p.get("status") == "approved":
                kind, detail = "approved", f"request {p.get('request_id', '?')}, not yet executed"
    return OutcomeView(kind, OUTCOME_LABELS[kind], detail)
