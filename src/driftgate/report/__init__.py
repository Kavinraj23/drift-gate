"""Static HTML trace report: one self-contained page per investigation, built offline from the audit log.

Pure functions over data already on disk. No web server, no network, no JavaScript, no model call, and nothing from
`eval/` or the ground truth: the page shows only what the agent and the audit log saw.
"""

from __future__ import annotations

from driftgate.report.render import build_pages, render_index, render_trace
from driftgate.report.trace import Trace, group_traces, load_audit, load_reports, parse_audit

__all__ = [
    "Trace",
    "build_pages",
    "group_traces",
    "load_audit",
    "load_reports",
    "parse_audit",
    "render_index",
    "render_trace",
]
