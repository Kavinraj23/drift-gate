"""Render traces as self-contained static HTML.

Security model: every value that reaches a page is untrusted (log text, hypotheses, identities, error excerpts). Each
one goes through `esc`: credential redaction, control and bidi characters made visible, a length cap, then
`html.escape`. The page has no JavaScript, no external resource, no URL taken from data (the only links are
`#fragment` anchors built from integers, and a relative file name built from a sanitised id), and a CSP meta that
forbids everything except inline styles. Output is deterministic: ordered by audit seq, timestamps formatted from the
logged epoch in UTC, no clock read.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from driftgate.audit import AuditEntry
from driftgate.redaction import redact
from driftgate.report.style import CSS
from driftgate.report.trace import (
    NOT_RECORDED,
    OutcomeView,
    RoundView,
    Trace,
    derive_outcome,
    evidence_items,
    group_traces,
    rounds,
    stage_of,
    with_stage,
)

CSP = "default-src 'none'; style-src 'unsafe-inline'"
MAX_VALUE_CHARS = 2000
_PRE_REDACT_CAP = 50_000  # bound the regex work on absurdly long values; the visible cap is MAX_VALUE_CHARS

_INVISIBLE = re.compile("[­؜᠎​-‏ -‮⁠-⁯﻿￹-￻]")
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
_DOTS = re.compile(r"\.{2,}")


class Html(str):
    """Markup this module built from already-escaped parts. Everything else is escaped when it is placed in a page."""


def _visible(match: re.Match[str]) -> str:
    return f"\\u{ord(match.group(0)):04x}"


def esc(value: object, limit: int = MAX_VALUE_CHARS) -> Html:
    """Untrusted value -> safe HTML text: redact, show control and bidi characters as escapes, cap, escape."""
    if isinstance(value, Html):
        return value
    text = value if isinstance(value, str) else str(value)
    text = redact(text[:_PRE_REDACT_CAP])
    text = _CONTROL.sub(_visible, _INVISIBLE.sub(_visible, text))
    cut = len(text) - limit
    if cut > 0:
        text = text[:limit] + f" ...[{cut} more characters not shown]"
    return Html(html.escape(text, quote=True))


def _json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
    except RecursionError:
        return "[value nested too deeply to show]"
    except (TypeError, ValueError):
        return "[value not representable]"


def scalar(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    return _json(value)


def fmt_ts(ts: object) -> str:
    if isinstance(ts, bool) or not isinstance(ts, int | float):
        return NOT_RECORDED
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return NOT_RECORDED


def file_name(execution_id: str) -> str:
    """A safe, unique file name for an execution id: redacted, sanitised, with a hash suffix when it changed."""
    safe = _DOTS.sub("_", _UNSAFE_NAME.sub("_", redact(execution_id[:_PRE_REDACT_CAP])))[:64].strip(".") or "unknown"
    if safe != execution_id:
        safe += "-" + hashlib.sha256(execution_id.encode("utf-8", "replace")).hexdigest()[:8]
    return f"trace-{safe}.html"


# -- small builders ---------------------------------------------------------------------------------------------------
def tag(name: str, *parts: object, cls: str = "") -> Html:
    attr = f' class="{cls}"' if cls else ""  # `cls` is always a literal from this module
    return Html(f"<{name}{attr}>" + "".join(esc(p) for p in parts) + f"</{name}>")


def muted(text: str) -> Html:
    return tag("p", text, cls="muted")


def table(headers: list[str], rows: Iterable[list[object]]) -> Html:
    body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in rows)
    if not body:
        return muted("none")
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    return Html(f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')


def facts(pairs: Iterable[tuple[str, object]]) -> Html:
    rows = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in pairs)
    return Html(f"<dl>{rows}</dl>") if rows else muted("none")


def payload_pairs(p: Mapping[str, Any], skip: tuple[str, ...] = ("stage",)) -> list[tuple[str, object]]:
    return [(k, scalar(v)) for k, v in sorted(p.items()) if k not in skip]


def section(sid: str, title: str, *body: object) -> Html:
    return Html(f'<section id="{sid}"><h2>{esc(title)}</h2>' + "".join(esc(b) for b in body) + "</section>")


def _val(report: Mapping[str, Any] | None, key: str) -> object:
    if report is None or key not in report:
        return NOT_RECORDED
    v = report[key]
    return v if v not in (None, "") else NOT_RECORDED


# -- sections ---------------------------------------------------------------------------------------------------------
def _header(trace: Trace, outcome: OutcomeView) -> Html:
    r = trace.report
    times = [e.ts for e in trace.entries]
    conf = _val(r, "confidence")
    pairs: list[tuple[str, object]] = [
        ("Execution id", trace.execution_id or NOT_RECORDED),
        ("Fingerprint", trace.fingerprint or NOT_RECORDED),
        ("Classification", _val(r, "classification")),
        ("Layer", _val(r, "layer")),
        ("Agent confidence", f"{conf} (recorded only; it never qualifies an action)" if r else NOT_RECORDED),
        ("Audit entries", len(trace.entries)),
        ("First entry", fmt_ts(min(times)) if times else NOT_RECORDED),
        ("Last entry", fmt_ts(max(times)) if times else NOT_RECORDED),
    ]
    if r and r.get("duplicate_of"):
        pairs.append(("Duplicate of", r["duplicate_of"]))
    badge = Html(f'<p class="badge {outcome.kind}">{esc(outcome.label)}</p>')
    return Html(f"<header><h1>Trace {esc(trace.execution_id or NOT_RECORDED)}</h1>{badge}{facts(pairs)}</header>")


def _hypothesis(trace: Trace, rs: list[RoundView]) -> Html:
    parts: list[object] = []
    r = trace.report
    if r and r.get("hypothesis"):
        parts.append(tag("blockquote", r["hypothesis"]))
        change = r.get("suspected_change")
        if isinstance(change, dict):
            parts.append(tag("h3", "Suspected change"))
            parts.append(facts(sorted((k, scalar(v)) for k, v in change.items())))
    if rs and (len(rs) > 1 or not (r and r.get("hypothesis"))):
        for rv in rs:
            h = rv.entry.payload.get("hypothesis")
            parts.append(tag("h3", f"Round {rv.number} (from the audit log, truncated at 600 characters)"))
            parts.append(tag("blockquote", h) if h else muted(NOT_RECORDED))
    if not parts:
        parts.append(muted(NOT_RECORDED))
    return section("hypothesis", "Hypothesis", *parts)


def _tool_calls(trace: Trace, rs: list[RoundView], cited: Mapping[str, int]) -> Html:
    if not rs:
        return section("tool-calls", "Tool calls", muted(f"{NOT_RECORDED}: the log has no investigation entry"))
    parts: list[object] = []
    for rv in rs:
        parts.append(tag("h3", f"Round {rv.number}"))
        rows: list[list[object]] = []
        for c in rv.tool_calls:
            rows.append(
                [
                    Html(f'<span id="{c.anchor}">{esc(c.call_id)}</span>'),
                    c.summary or NOT_RECORDED,
                    f"cited by {cited[c.call_id]} evidence item(s)" if cited.get(c.call_id) else "not cited",
                ]
            )
        parts.append(table(["Call id", "Tool and result summary (as audited)", "Use"], rows))
        p = rv.entry.payload
        parts.append(facts([("Stop reason", p.get("stop_reason", NOT_RECORDED)), ("Tool calls", len(rv.tool_calls))]))
    parts.append(
        muted(
            "The result summary is the one line the audit log keeps per call. Deterministic gate-time calls "
            "(classification, fleet correlation, flake history) use ids prefixed gate: and are not recorded here."
        )
    )
    return section("tool-calls", "Tool calls", *parts)


def _id_index(rs: list[RoundView]) -> dict[str, list[tuple[int, str]]]:
    index: dict[str, list[tuple[int, str]]] = {}
    for rv in rs:
        for c in rv.tool_calls:
            index.setdefault(c.call_id, []).append((rv.number, c.anchor))
    return index


def _evidence(trace: Trace, rs: list[RoundView], ids: dict[str, list[tuple[int, str]]]) -> tuple[Html, dict[str, int]]:
    items = evidence_items(trace)
    cited: dict[str, int] = {}
    parts: list[object] = []
    dropped = sum(_count(rv.entry.payload.get("evidence_dropped")) for rv in rs)
    kept = sum(_count(rv.entry.payload.get("evidence_kept")) for rv in rs)
    if trace.report is None:
        parts.append(
            muted(
                f"Evidence text is {NOT_RECORDED}: the audit log keeps only counts (kept {kept}, dropped {dropped}). "
                "Supply the saved report JSON to see the items."
            )
        )
    rows: list[list[object]] = []
    for item in items:
        src = item.get("source")
        key = src if isinstance(src, str) else ""
        where = ids.get(key)
        if where:
            cited[key] = cited.get(key, 0) + 1
            extra = (
                f" (id reused in rounds {', '.join(str(n) for n, _ in where)}; first shown)" if len(where) > 1 else ""
            )
            link: object = Html(f'<a href="#{where[0][1]}">{esc(key)}</a>{esc(extra)}')
            status: object = Html('<span class="ok">cited</span>')
        else:
            link = key or NOT_RECORDED
            status = Html('<span class="bad">NOT CITED: source is not a tool call of this run; dropped in code</span>')
        rows.append([link, item.get("finding", NOT_RECORDED), item.get("supports", NOT_RECORDED), status])
    if trace.report is not None:
        parts.append(table(["Source (tool call)", "Finding", "Supports", "Citation"], rows))
    if rs:
        parts.append(
            facts(
                [
                    ("Evidence kept by the code (cited)", kept),
                    ("Evidence dropped in code (uncited or invalid)", dropped),
                    (
                        "Duplicate tool-call ids rejected",
                        sum(_count(rv.entry.payload.get("duplicate_ids_rejected")) for rv in rs),
                    ),
                ]
            )
        )
        if dropped:
            parts.append(muted("Dropped items are not stored anywhere, so their text cannot be shown."))
    return section("evidence", "Evidence with citations", *parts), cited


def _count(v: object) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _proposal(trace: Trace, rs: list[RoundView]) -> Html:
    parts: list[object] = []
    for rv in rs:
        p = rv.entry.payload
        parts.append(tag("h3", f"Round {rv.number}: the agent's proposal"))
        parts.append(
            facts(
                [
                    ("Action", p.get("action") or "none (no remediation proposed)"),
                    ("Proposed tier", p.get("tier", NOT_RECORDED)),
                    ("Gate required", p.get("gate", NOT_RECORDED)),
                    ("Paths", scalar(p.get("paths", []))),
                    ("Abstained", scalar(p.get("abstained", NOT_RECORDED))),
                    ("Agent confidence", p.get("confidence", NOT_RECORDED)),
                    ("Escalation reason given by the agent", p.get("escalation_reason") or "none"),
                ]
            )
        )
    rem = (trace.report or {}).get("remediation")
    if isinstance(rem, dict):
        parts.append(tag("h3", "Remediation in the final report (after the gate)"))
        parts.append(
            facts(
                [
                    ("Tier", rem.get("tier", NOT_RECORDED)),
                    ("Action", rem.get("action", NOT_RECORDED)),
                    ("Rationale", rem.get("rationale", NOT_RECORDED)),
                    ("Reversible", scalar(rem.get("reversible", NOT_RECORDED))),
                    ("Gate", rem.get("gate", NOT_RECORDED)),
                    ("Gate decision", rem.get("gate_decision", NOT_RECORDED)),
                ]
            )
        )
    if not parts:
        parts.append(muted(f"{NOT_RECORDED}: no proposal entry"))
    return section("proposal", "Proposal and tier", *parts)


def _gate(trace: Trace) -> Html:
    parts: list[object] = []
    decisions = with_stage(trace, "gate_decision")
    for e in decisions:
        p = e.payload
        label = stage_of(e) or "proposal"
        cls = {"allowed": "ok", "downgraded": "warn", "refused": "bad"}.get(str(p.get("decision")), "")
        parts.append(
            Html(
                f"<h3>Decision at #{e.seq}, {esc(fmt_ts(e.ts))} ({esc(label)}): "
                f'<span class="{cls}">{esc(p.get("decision", NOT_RECORDED))}</span></h3>'
            )
        )
        parts.append(
            facts(
                [
                    ("Reason", p.get("reason", NOT_RECORDED)),
                    ("Action", p.get("action", NOT_RECORDED)),
                    ("Proposed tier", p.get("proposed_tier", NOT_RECORDED)),
                    ("Agent confidence (recorded only)", p.get("agent_confidence", NOT_RECORDED)),
                    ("Abort ceiling", p.get("abort_ceiling", NOT_RECORDED)),
                    ("Approval request", p.get("request_id", "n/a")),
                ]
            )
        )
    for e in with_stage(trace, "proposal", "gate_facts"):
        f = e.payload.get("facts")
        parts.append(tag("h3", f"Facts the gate was given (#{e.seq}, derived by code, not by the agent)"))
        parts.append(
            facts(sorted((k, scalar(v)) for k, v in f.items())) if isinstance(f, dict) else muted(NOT_RECORDED)
        )
    for e in with_stage(trace, "proposal", "holder_proof"):
        parts.append(tag("h3", f"Tier 2 holder-death proof (#{e.seq})"))
        parts.append(facts(payload_pairs(e.payload)))
    if not decisions:
        parts.insert(0, muted(f"{NOT_RECORDED}: no gate decision in the log (for example a pre-filter close)"))
    return section("gate", "SafetyGate decisions", *parts)


def _tier3(trace: Trace) -> Html:
    parts: list[object] = []
    for e in with_stage(trace, "proposal", "tier3_diff"):
        p = e.payload
        parts.append(tag("h3", f"Deterministic diff check (#{e.seq})"))
        parts.append(
            facts(
                [
                    ("Passed", scalar(p.get("ok", NOT_RECORDED))),
                    ("Files", scalar(p.get("files", []))),
                    ("Added / removed lines", f"{p.get('added', '?')} / {p.get('removed', '?')}"),
                    ("Violations", scalar(p.get("violations", []))),
                ]
            )
        )
    for e in with_stage(trace, "proposal", "tier3_review", "tier3_revision"):
        parts.append(tag("h3", f"{stage_of(e)} (#{e.seq})"))
        parts.append(facts(payload_pairs(e.payload)))
    for e in with_stage(trace, "execution", "pr_proposed"):
        parts.append(tag("h3", f"Pull request (#{e.seq})"))
        parts.append(facts(payload_pairs(e.payload)))
    if not parts:
        parts.append(muted("none: not a Tier 3 case"))
    return section("tier3", "Tier 3 review and pull request", *parts)


def _approval_rows(trace: Trace) -> list[list[object]]:
    rows: list[list[object]] = []
    for e in trace.entries:
        p, st, when = e.payload, stage_of(e), fmt_ts(e.ts)
        if e.kind == "proposal" and st == "approval_requested":
            detail = (
                f"request {p.get('request_id')}, tier {p.get('tier')}, {p.get('action')}; needs "
                f"{p.get('required_approvals')} approval(s); expires {fmt_ts(p.get('expires_at'))}"
            )
            rows.append([when, "requested", p.get("requested_by", NOT_RECORDED), detail])
        elif e.kind == "proposal" and st == "approval_decision":
            detail = (
                f"request {p.get('request_id')}: {p.get('approvals')}/{p.get('required')} approvals, "
                f"status {p.get('status')}"
            )
            rows.append([when, str(p.get("decision", NOT_RECORDED)), p.get("approver", NOT_RECORDED), detail])
        elif e.kind == "rejected" and st == "approval_refused":
            detail = f"request {p.get('request_id')}: {p.get('error')}: {p.get('reason')}"
            rows.append([when, f"refused ({p.get('decision')})", p.get("approver", NOT_RECORDED), detail])
        elif e.kind == "rejected" and st == "approval_denied":
            rows.append([when, "request closed (denied)", "n/a", p.get("reason", NOT_RECORDED)])
        elif e.kind == "rejected" and st in ("approval_request", "approval_execute_refused"):
            rows.append([when, st, "n/a", p.get("reason", NOT_RECORDED)])
        elif e.kind == "expired":
            detail = f"request {p.get('request_id')}, window closed {fmt_ts(p.get('expires_at'))}: {p.get('reason')}"
            rows.append([when, "expired", "n/a", detail])
    return rows


def _approvals(trace: Trace) -> Html:
    rows = _approval_rows(trace)
    if not rows:
        return section("approvals", "Approvals", muted("none: no approval request was opened for this case"))
    return section("approvals", "Approvals", table(["When", "Event", "Who (as recorded)", "Detail"], rows))


def _execution(trace: Trace) -> Html:
    rows: list[list[object]] = []
    for e in trace.entries:
        if e.kind not in ("execution", "verification", "failed"):
            continue
        p = e.payload
        result = p.get("verified") if e.kind == "verification" else p.get("ok")
        ref = p.get("request_id", p.get("attempt_id", ""))
        rows.append(
            [
                fmt_ts(e.ts),
                e.kind,
                stage_of(e),
                scalar(result) if result is not None else "",
                p.get("description", ""),
                ref,
            ]
        )
    if not rows:
        return section("execution", "Execution and verification", muted("none: nothing was executed or verified"))
    return section(
        "execution",
        "Execution and verification",
        table(["When", "Kind", "Stage", "Ok / verified", "Description", "Request / attempt"], rows),
    )


def _reinvestigation(trace: Trace, rs: list[RoundView]) -> Html:
    starts = with_stage(trace, "proposal", "reinvestigation_start")
    if not starts and len(rs) < 2:
        return section("reinvestigation", "Re-investigation", muted("none: the first round was final"))
    parts: list[object] = []
    for e in starts:
        parts.append(tag("h3", f"Re-investigation started (#{e.seq}, {fmt_ts(e.ts)})"))
        parts.append(facts(payload_pairs(e.payload)))
    parts.append(tag("p", f"Investigation rounds recorded: {len(rs)}"))
    return section("reinvestigation", "Re-investigation", *parts)


def _outcome(trace: Trace, outcome: OutcomeView) -> Html:
    r = trace.report or {}
    pairs: list[tuple[str, object]] = [("Outcome", outcome.label), ("Detail", outcome.detail or "none")]
    pairs.append(("Escalation reason in the report", r.get("escalation_reason") or ("none" if r else NOT_RECORDED)))
    attempts = r.get("prior_attempts")
    if isinstance(attempts, list) and attempts:
        pairs.append(("Prior attempts", _json(attempts)))
    parts: list[object] = [facts(pairs)]
    stops = [
        e for e in trace.entries if e.kind in ("rejected", "expired", "failed") and stage_of(e) != "approval_refused"
    ]
    if stops:
        parts.append(tag("h3", "Why it stopped"))
        parts.append(
            table(
                ["When", "Kind", "Stage", "Reason"],
                [
                    [fmt_ts(e.ts), e.kind, stage_of(e), e.payload.get("reason", e.payload.get("description", ""))]
                    for e in stops
                ],
            )
        )
    return section("outcome", "Final outcome", *parts)


def _budget(trace: Trace, rs: list[RoundView]) -> Html:
    run = (trace.report or {}).get("run")
    parts: list[object] = []
    if isinstance(run, dict):
        parts.append(facts(sorted((k, scalar(v)) for k, v in run.items())))
    else:
        parts.append(muted(f"cost and token totals: {NOT_RECORDED} (they live in the saved report)"))
    for e in with_stage(trace, "budget_truncated"):
        parts.append(tag("h3", f"Budget truncated (#{e.seq})"))
        parts.append(facts(payload_pairs(e.payload)))
    for rv in rs:
        p = rv.entry.payload
        parts.append(
            tag(
                "p",
                f"Round {rv.number}: stop reason {p.get('stop_reason', NOT_RECORDED)}, "
                f"budget_truncated {scalar(p.get('budget_truncated', NOT_RECORDED))}",
            )
        )
    return section("budget", "Budget and cost", *parts)


def _trail(trace: Trace) -> Html:
    rows = [
        [
            e.seq,
            fmt_ts(e.ts),
            e.kind,
            stage_of(e),
            "; ".join(f"{k}={scalar(v)}" for k, v in sorted(e.payload.items()) if k != "stage"),
        ]
        for e in trace.entries
    ]
    return section(
        "audit-trail",
        "Audit trail (every entry, in seq order)",
        table(["Seq", "When", "Kind", "Stage", "Payload"], rows),
    )


# -- pages ------------------------------------------------------------------------------------------------------------
SECTIONS = (
    ("hypothesis", "Hypothesis"),
    ("tool-calls", "Tool calls"),
    ("evidence", "Evidence"),
    ("proposal", "Proposal"),
    ("gate", "Gate"),
    ("tier3", "Tier 3"),
    ("approvals", "Approvals"),
    ("execution", "Execution"),
    ("reinvestigation", "Re-investigation"),
    ("outcome", "Outcome"),
    ("budget", "Budget"),
    ("audit-trail", "Audit trail"),
)


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body>{body}"
        "<footer>Static report generated from the audit log. All values are redacted and escaped; "
        "this page contains no script and loads nothing.</footer></body></html>\n"
    )


def render_trace(trace: Trace) -> str:
    """One self-contained HTML page for an investigation."""
    rs = rounds(trace)
    outcome = derive_outcome(trace)
    ids = _id_index(rs)
    evidence, cited = _evidence(trace, rs, ids)
    nav = Html("<nav>" + " ".join(f'<a href="#{sid}">{esc(label)}</a>' for sid, label in SECTIONS) + "</nav>")
    body = "".join(
        str(x)
        for x in (
            _header(trace, outcome),
            nav,
            _hypothesis(trace, rs),
            _tool_calls(trace, rs, cited),
            evidence,
            _proposal(trace, rs),
            _gate(trace),
            _tier3(trace),
            _approvals(trace),
            _execution(trace),
            _reinvestigation(trace, rs),
            _outcome(trace, outcome),
            _budget(trace, rs),
            _trail(trace),
        )
    )
    return _page(f"Trace {trace.execution_id or NOT_RECORDED}", body)


def render_index(traces: list[Trace], skipped_lines: int = 0) -> str:
    rows: list[list[object]] = []
    for t in traces:
        o = derive_outcome(t)
        rows.append(
            [
                Html(f'<a href="{file_name(t.execution_id)}">{esc(t.execution_id or NOT_RECORDED)}</a>'),
                t.fingerprint or NOT_RECORDED,
                _val(t.report, "classification"),
                o.label,
                len(t.entries),
            ]
        )
    note = f"{skipped_lines} unreadable audit line(s) were skipped." if skipped_lines else ""
    summary = muted(f"{len(traces)} investigation(s) in the audit log. {note}")
    body = f"<header><h1>Investigation traces</h1>{summary}</header>" + str(
        table(["Execution", "Fingerprint", "Classification", "Outcome", "Audit entries"], rows)
    )
    return _page("Investigation traces", body)


def build_pages(
    entries: Iterable[AuditEntry],
    reports: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    skipped_lines: int = 0,
    only: Callable[[Trace], bool] | None = None,
) -> dict[str, str]:
    """`{file name: html}` for every investigation in the log, plus `index.html`. Pure: same input, same bytes."""
    traces = [t for t in group_traces(entries, reports) if only is None or only(t)]
    pages = {"index.html": render_index(traces, skipped_lines)}
    for t in traces:
        pages[file_name(t.execution_id)] = render_trace(t)
    return pages
