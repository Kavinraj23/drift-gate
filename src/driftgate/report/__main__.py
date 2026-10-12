"""`python -m driftgate.report [--audit PATH] [--reports PATH] [--out DIR]`: write the static trace pages.

Offline and read-only on its inputs. With no arguments it builds from the committed sample in
`fixtures/trace_sample/` into `data/report/`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from driftgate.report.render import build_pages
from driftgate.report.trace import load_audit, load_reports

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_AUDIT = ROOT / "fixtures" / "trace_sample" / "audit.jsonl"
DEFAULT_OUT = ROOT / "data" / "report"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python tasks.py trace-report", allow_abbrev=False)
    ap.add_argument("--audit", type=Path, default=DEFAULT_AUDIT, help="audit log (JSONL)")
    ap.add_argument("--reports", type=Path, default=None, help="saved reports JSON; default: reports.json beside it")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    args = ap.parse_args(argv)

    if not args.audit.is_file():
        print(f"audit log not found: {args.audit}", file=sys.stderr)
        return 1
    entries, skipped = load_audit(args.audit)
    reports_path = args.reports or args.audit.with_name("reports.json")
    if args.reports is not None and not reports_path.is_file():
        print(f"reports file not found: {reports_path}", file=sys.stderr)
        return 1
    try:
        reports = load_reports(reports_path) if reports_path.is_file() else {}
    except ValueError as e:
        print(f"reports file is not valid JSON: {e}", file=sys.stderr)
        return 1

    pages = build_pages(entries, reports, skipped_lines=skipped)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in pages.items():
        (args.out / name).write_text(text, encoding="utf-8", newline="\n")
        print(args.out / name)
    if skipped:
        print(f"skipped {skipped} unreadable audit line(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
