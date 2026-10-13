"""`python -m driftgate.report [--audit PATH] [--reports PATH] [--out DIR]`: write the static trace pages.

Offline and read-only on its inputs. With no arguments it builds from the committed sample in
`fixtures/trace_sample/` into `data/report/`.

Writes only `index.html` and `trace-*.html` inside the output directory. It refuses an output path that is a file, and
a target name that is a symlink or directory. It removes nothing except stale `trace-*.html` files that carry this
tool's marker comment and are no longer part of the output (pages from an earlier run); any other file is left alone.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from driftgate.report.render import MARKER, build_pages
from driftgate.report.trace import load_audit, load_reports

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_AUDIT = ROOT / "fixtures" / "trace_sample" / "audit.jsonl"
DEFAULT_OUT = ROOT / "data" / "report"
_TRACE_FILE = re.compile(r"trace-[A-Za-z0-9._-]+\.html")


def _ours(path: Path) -> bool:
    """A regular file (not a link) named like a trace page whose head carries this tool's marker comment."""
    if path.is_symlink() or not path.is_file() or not _TRACE_FILE.fullmatch(path.name):
        return False
    try:
        with path.open("rb") as f:
            return f"<!-- {MARKER} -->".encode() in f.read(256)
    except OSError:
        return False


def _write(out: Path, pages: dict[str, str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, text in pages.items():
        target = out / name
        if target.is_symlink() or target.is_dir():
            raise OSError(f"{target} is a link or a directory; not overwriting it")
        target.write_text(text, encoding="utf-8", newline="\n")
        print(target)
    for old in sorted(out.glob("trace-*.html")):
        if old.name not in pages and _ours(old):
            old.unlink()
            print(f"removed stale page {old}", file=sys.stderr)


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

    if args.out.exists() and not args.out.is_dir():
        print(f"refused: --out {args.out} exists and is not a directory; nothing was written", file=sys.stderr)
        return 1
    pages = build_pages(entries, reports, skipped_lines=skipped)
    try:
        _write(args.out, pages)
    except OSError as e:  # FileExistsError, NotADirectoryError, PermissionError, a path component that is a file
        print(f"refused: could not write the report to {args.out}: {e}", file=sys.stderr)
        return 1
    if skipped:
        print(f"skipped {skipped} unreadable audit line(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
