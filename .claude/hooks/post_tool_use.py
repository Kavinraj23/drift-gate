"""PostToolUse(Edit|Write): ruff format + ruff check on the changed .py file."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ROOT, python_exe, read_payload  # noqa: E402


def main() -> int:
    file_path = (read_payload().get("tool_input") or {}).get("file_path", "")
    path = Path(file_path)
    if path.suffix != ".py" or not path.exists():
        return 0
    py = python_exe()
    subprocess.run([py, "-m", "ruff", "format", str(path)], cwd=ROOT, capture_output=True)
    result = subprocess.run([py, "-m", "ruff", "check", str(path)], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout + result.stderr, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
