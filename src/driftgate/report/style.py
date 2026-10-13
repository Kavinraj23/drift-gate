"""Inline stylesheet for the trace pages: light and dark via prefers-color-scheme, no external resources."""

from __future__ import annotations

RULES = (
    ":root{--bg:#fff;--fg:#1a1d21;--muted:#5b6470;--line:#d5d9df;--card:#f6f7f9;"
    "--ok:#16794a;--warn:#9a5b00;--bad:#b3261e;--link:#0b57d0}",
    "@media (prefers-color-scheme:dark){:root{--bg:#14171b;--fg:#e6e8eb;--muted:#9aa4b0;--line:#323941;"
    "--card:#1c2025;--ok:#4cc38a;--warn:#e3a33b;--bad:#ff7b72;--link:#8ab4f8}}",
    "*{box-sizing:border-box}",
    "body{margin:0 auto;max-width:72rem;padding:1rem 16px 3rem;background:var(--bg);color:var(--fg);"
    "font:15px/1.5 system-ui,sans-serif;overflow-wrap:anywhere}",
    "h1{font-size:1.4rem;margin:.2rem 0}",
    "h2{font-size:1.1rem;border-bottom:1px solid var(--line);padding-bottom:.2rem;margin-top:2rem}",
    "h3{font-size:.95rem;margin:1rem 0 .3rem}",
    "a{color:var(--link)}",
    "nav{display:flex;flex-wrap:wrap;gap:.3rem 1rem;margin:.5rem 0 1rem;font-size:.85rem}",
    "dl{display:grid;grid-template-columns:minmax(8rem,16rem) 1fr;gap:.2rem 1rem;margin:.4rem 0}",
    "dt{color:var(--muted)}",
    "dd{margin:0}",
    ".scroll{overflow-x:auto}",
    "table{border-collapse:collapse;width:100%;font-size:.85rem}",
    "th,td{border:1px solid var(--line);padding:.25rem .5rem;text-align:left;vertical-align:top}",
    "th{background:var(--card)}",
    "blockquote{margin:.4rem 0;padding:.4rem .8rem;border-left:3px solid var(--line);"
    "background:var(--card);white-space:pre-wrap}",
    ".muted{color:var(--muted)}",
    ".ok{color:var(--ok);font-weight:600}",
    ".warn{color:var(--warn);font-weight:600}",
    ".bad{color:var(--bad);font-weight:600}",
    ".badge{display:inline-block;padding:.15rem .6rem;border-radius:1rem;border:1px solid var(--line);"
    "background:var(--card);font-weight:600}",
    ".badge.verified,.badge.executed,.badge.pr_proposed,.badge.pr_verified,.badge.closed{color:var(--ok)}",
    ".badge.escalated,.badge.pr_ci_failed,.badge.awaiting_approval,.badge.approved,.badge.expired,.badge.denied{color:var(--warn)}",
    ".badge.approval_cancelled{color:var(--bad);border-style:dashed}",
    ".badge.failed,.badge.verification_failed{color:var(--bad)}",
    "footer{margin-top:2rem;color:var(--muted);font-size:.8rem}",
)

CSS = "\n".join(RULES)
