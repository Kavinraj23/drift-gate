"""`python tasks.py holdout-fetch`: fetch failed-step logs of human-picked public runs (HUMAN ONLY).

This is NOT the playground adapter. It is a separate, GET-only reader (`holdout_gh`) for PUBLIC third-party
repositories, with its own credential: a fine-grained read-only token in `HOLDOUT_FETCH_TOKEN` (environment, or the
gitignored `.env`). It lives in `eval/` because only `eval/` may touch `holdout/` (invariants 8 and 15) and nothing in
`agents/`, `tools/`, `llm/` may import it.

Input: `holdout/candidates.json` (human-written; `contracts/holdout_candidates.schema.json`):
`{"schema_version": 1, "candidates": [{"repo": "owner/name", "run_id": 123, "job": "optional exact job name",
"step": "optional failed step name or number"}]}`. `step` is REQUIRED when more than one step of the job concluded
`failure` (the first one may be a `continue-on-error` step); otherwise the case is refused and nothing is stored.

Default is a PLAN: it validates the file, lists what would be fetched and how many requests that is, and makes none.
Real network needs `--yes-fetch`. For each new candidate it stores, and nothing else:
- `holdout/logs/<case_id>.log`: the failed step's section (see `holdout_steplog` for the exact transformations);
- `holdout/fetched/<case_id>.json`: source metadata (repo, run id, job, commit SHA, URL, fetch date) and the SHA-256 of
  the stored bytes, plus job id, run attempt, step name/number and the number of other failed steps.
It never writes `labels.json`, never assigns layer/classification/action/rationale/split/`labeled_at`, and never
overwrites anything. Labelling is the human's job and must happen before the agent runs (`holdout/README.md`).

`--skeleton` (offline, no token) prints a labels skeleton for fetched cases that have no label yet; see
`holdout_skeleton`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from jsonschema import Draft202012Validator

from driftgate.eval.holdout import sha256_bytes
from driftgate.eval.holdout_gh import (
    FetchError,
    RateLimited,
    ReadOnlyGitHub,
    RequestsTransport,
    Transport,
    split_repo,
    valid_id,
)
from driftgate.eval.holdout_steplog import (
    StepLogError,
    count_lines,
    decode_log,
    extract_step_section,
    extraction_inconsistency,
    first_failed_step,
    prepare_for_storage,
)

ROOT = Path(__file__).resolve().parents[3]
HOLDOUT_DIR = ROOT / "holdout"
CANDIDATES_SCHEMA = ROOT / "contracts" / "holdout_candidates.schema.json"
ENV_FILE = ROOT / ".env"
TOKEN_ENV = "HOLDOUT_FETCH_TOKEN"
REQUESTS_PER_RUN = 4  # run metadata + jobs list + job log (API) + log blob; more only for runs with over 100 jobs

Clock = Callable[[], datetime]
TokenProvider = Callable[[], str | None]
TransportFactory = Callable[[], Transport]

_ID_RE = re.compile(r"^hc-([0-9]{2,3})$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
Identity = tuple[str, int, str]  # (lower-cased repo, run id, job name)


class CandidateError(ValueError):
    """candidates.json is unusable. The message names the problem, never a value that could be a secret."""


@dataclass(frozen=True)
class Candidate:
    repo: str
    run_id: int
    job: str | None
    step: str | int | None = None  # which failed step to store; required when several steps failed


@dataclass(frozen=True)
class PlanRow:
    candidate: Candidate
    case_id: str | None  # None when the candidate is already fetched
    skipped_as: str | None  # the existing case id when already fetched


def _schema_doc() -> dict[str, Any]:
    return json.loads(CANDIDATES_SCHEMA.read_text(encoding="utf-8"))


def load_candidates(path: Path) -> list[Candidate]:
    """Schema-validated and strictly checked before any network call."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CandidateError(f"{path.name} not found") from None
    except (OSError, ValueError) as exc:
        raise CandidateError(f"{path.name} is unreadable ({type(exc).__name__})") from None
    errors = sorted(
        Draft202012Validator(_schema_doc()).iter_errors(doc), key=lambda e: [str(p) for p in e.absolute_path]
    )
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.absolute_path) or "(root)"
        raise CandidateError(f"{path.name}: schema violation at {where} ({first.validator})")
    out: list[Candidate] = []
    seen: set[tuple[str, int, str | None]] = set()
    for raw in doc["candidates"]:
        try:
            owner, name = split_repo(raw["repo"])
            run_id = valid_id(raw["run_id"], "run id")
        except FetchError as exc:
            raise CandidateError(f"{path.name}: {exc}") from None
        cand = Candidate(f"{owner}/{name}", run_id, raw.get("job"), raw.get("step"))
        key = (cand.repo.lower(), cand.run_id, cand.job)
        if key in seen:
            raise CandidateError(f"{path.name}: the same repo/run/job is listed twice (run {run_id})")
        seen.add(key)
        out.append(cand)
    return out


class Store:
    """The `holdout/` directory as the fetcher sees it: which ids and sources exist, and exclusive writes."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.logs = root / "logs"
        self.fetched = root / "fetched"

    def _read(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise FetchError(f"{path.name} is unreadable; fix or remove it before fetching") from None

    def scan(self) -> tuple[set[int], dict[Identity, str]]:
        """Numbers of every used case id (labels, logs, fetched records) and the sources already present."""
        numbers: set[int] = set()
        sources: dict[Identity, str] = {}

        def note(case_id: Any, source: Any) -> None:
            m = _ID_RE.match(case_id) if isinstance(case_id, str) else None
            if m:
                numbers.add(int(m.group(1)))
            if m and isinstance(source, dict):
                try:
                    sources[(str(source["repo"]).lower(), int(source["run_id"]), str(source["job"]))] = case_id
                except (KeyError, TypeError, ValueError):
                    pass

        labels = self._read(self.root / "labels.json")
        for case in (labels or {}).get("cases", []) if isinstance(labels, dict) else []:
            if isinstance(case, dict):
                note(case.get("id"), case.get("source"))
        if self.fetched.is_dir():
            for p in sorted(self.fetched.glob("*.json")):
                rec = self._read(p)
                if isinstance(rec, dict):
                    note(rec.get("case_id"), rec.get("source"))
                note(p.stem, None)
        if self.logs.is_dir():
            for p in sorted(self.logs.glob("*.log")):
                note(p.stem, None)
        return numbers, sources

    def write_new(self, path: Path, data: bytes) -> None:
        """Create-only. An existing file with identical bytes is left alone; different bytes are refused."""
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "xb") as fh:
                fh.write(data)
        except FileExistsError:
            if path.read_bytes() != data:
                raise FetchError(f"{path.name} already exists with different content; nothing overwritten") from None


def case_id_for(number: int) -> str:
    if number > 999:
        raise FetchError("case id space (hc-999) is exhausted")
    return f"hc-{number:02d}"


def make_plan(candidates: list[Candidate], store: Store) -> list[PlanRow]:
    """Deterministic: ids are allocated in input order starting after the highest id ever used."""
    numbers, sources = store.scan()
    nxt = max(numbers, default=0) + 1
    rows: list[PlanRow] = []
    for cand in candidates:
        # A named job can be checked now; an unnamed one is checked once its failed job is known (at fetch time).
        existing = sources.get((cand.repo.lower(), cand.run_id, cand.job)) if cand.job is not None else None
        if existing is not None:
            rows.append(PlanRow(cand, None, existing))
            continue
        rows.append(PlanRow(cand, case_id_for(nxt), None))
        nxt += 1
    return rows


def format_plan(rows: list[PlanRow], candidates_name: str) -> str:
    new = [r for r in rows if r.skipped_as is None]
    lines = [f"holdout-fetch plan for {candidates_name} (nothing was requested):"]
    for r in rows:
        job = repr(r.candidate.job) if r.candidate.job else "(the failed job)"
        if r.skipped_as:
            lines.append(
                f"  REFUSED  {r.candidate.repo} run {r.candidate.run_id} job {job}: already fetched as {r.skipped_as}"
            )
        else:
            lines.append(f"  {r.case_id}    {r.candidate.repo} run {r.candidate.run_id} job {job}")
    lines.append(
        f"{len(new)} run(s) to fetch, {len(new) * REQUESTS_PER_RUN} request(s): per run 3 GitHub API calls "
        "(run, jobs, job log) plus 1 log download; GET only; read-only token."
    )
    lines.append("Stores logs/<id>.log and fetched/<id>.json only. Labels are yours to write; this never touches them.")
    return "\n".join(lines)


def _pick_job(jobs: list[dict[str, Any]], wanted: str | None) -> dict[str, Any]:
    if wanted is not None:
        named = [j for j in jobs if j.get("name") == wanted]
        if len(named) != 1:
            raise FetchError(f"{len(named)} jobs are named {wanted!r} in this run; need exactly one")
        job = named[0]
        if job.get("conclusion") != "failure":
            raise FetchError(f"job {wanted!r} did not conclude 'failure' (it is {job.get('conclusion')!r})")
        return job
    failed = [j for j in jobs if j.get("conclusion") == "failure"]
    if not failed:
        raise FetchError("the run has no job that concluded 'failure'")
    if len(failed) > 1:
        names = ", ".join(repr(str(j.get("name"))[:60]) for j in failed[:8])
        raise FetchError(f'{len(failed)} jobs failed ({names}); name one with "job" in candidates.json')
    return failed[0]


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_one(
    cand: Candidate,
    case_id: str,
    gh: ReadOnlyGitHub,
    store: Store,
    today: str,
    known: dict[Identity, str],
    token: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Returns (fetched record, None) or ({}, existing case id) when the resolved job is already stored.
    Raises `StepLogError` (nothing written) when the failed step cannot be cut out safely."""
    run = gh.get_run(cand.repo, cand.run_id)
    if run.get("status") != "completed" or run.get("conclusion") != "failure":
        raise FetchError(f"run is {run.get('status')!r}/{run.get('conclusion')!r}, not completed/failure")
    sha, url = run.get("head_sha"), run.get("html_url")
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise FetchError("run metadata has no valid head_sha")
    expected = f"https://github.com/{cand.repo}/actions/runs/{cand.run_id}"
    if not isinstance(url, str) or url.lower() != expected.lower():
        raise FetchError("run metadata html_url does not match the requested repo and run")
    job = _pick_job(gh.list_jobs(cand.repo, cand.run_id), cand.job)
    job_name, job_id = str(job.get("name")), valid_id(job.get("id"), "job id")
    existing = known.get((cand.repo.lower(), cand.run_id, job_name))
    if existing:
        return {}, existing
    attempt = run.get("run_attempt")
    job_attempt = job.get("run_attempt")
    if job_attempt is not None and job_attempt != attempt:
        raise FetchError("the job's run_attempt differs from the run's; not the latest attempt, fetch by hand")
    step = first_failed_step(job, cand.step)
    raw = gh.get_job_log(cand.repo, job_id)
    ext = extract_step_section(decode_log(raw), step)
    data = prepare_for_storage(ext.text, token).encode("utf-8")
    record = {
        "schema_version": 2,
        "case_id": case_id,
        "source": {
            "repo": cand.repo,
            "run_id": cand.run_id,
            "job": job_name,
            "commit_sha": sha,
            "url": expected,
            "fetch_date": today,
        },
        "job_id": job_id,
        "run_attempt": attempt if type(attempt) is int and attempt > 0 else None,
        "step": {
            "name": step.name,
            "number": step.number,
            "started_at": _iso(step.started_at),
            "completed_at": _iso(step.completed_at),
        },
        "other_failed_steps": [{"name": o.name, "number": o.number} for o in step.other_failed],
        "extraction": {
            "method": ext.method,
            "job_log_lines": ext.job_log_lines,
            "start_line": ext.start_line,
            "end_line": ext.end_line,
            "error_line": ext.error_line,
            "error_timestamp": ext.error_timestamp,
            "inner_group_lines": ext.inner_group_lines,
            "stored_lines": count_lines(data.decode("utf-8")),
        },
        "log_sha256": sha256_bytes(data),
        "log_bytes": len(data),
    }
    bad = extraction_inconsistency(record)
    if bad:  # a bug in the extractor, not in the data: nothing is written
        raise StepLogError(f"internal inconsistency ({bad})")
    store.write_new(store.logs / f"{case_id}.log", data)
    store.write_new(store.fetched / f"{case_id}.json", (json.dumps(record, indent=2) + "\n").encode("utf-8"))
    return record, None


_ENV_LINE = re.compile(rf"^\s*(?:export\s+)?{TOKEN_ENV}\s*=\s*(.*?)\s*$", re.ASCII)


def env_token() -> str | None:
    """`HOLDOUT_FETCH_TOKEN` from the environment, else from the gitignored env file.

    The environment wins over the file. In the file the key must appear AT MOST ONCE: this is deliberately stricter
    than python-dotenv (last wins) and than a first-match reader (first wins); a duplicate is ambiguous, so it is an
    error rather than a guess. The file is read line by line (a leading BOM is ignored); every line is dropped as soon
    as it is examined and only the matching value is kept (no other key is parsed, retained or exported, and there is
    no `${VAR}` interpolation). A value may be wrapped in one pair of single or double quotes; a trailing ` # comment`
    is not stripped. A file that is not valid UTF-8, or unreadable, raises `FetchError` with a fixed message (no
    content); so does a duplicate key."""
    value = os.environ.get(TOKEN_ENV)
    if value:
        return value
    if not ENV_FILE.is_file():
        return None
    found: str | None = None
    count = 0
    try:
        with open(ENV_FILE, encoding="utf-8-sig") as fh:
            for line in fh:
                m = _ENV_LINE.match(line)
                if m:
                    count += 1
                    if count == 1:
                        found = m.group(1)
    except UnicodeDecodeError:
        raise FetchError(f"could not read the token: the env file is not valid UTF-8 ({TOKEN_ENV})") from None
    except OSError:
        raise FetchError(f"could not read the token: the env file is unreadable ({TOKEN_ENV})") from None
    if count > 1:
        raise FetchError(f"could not read the token: {TOKEN_ENV} appears {count} times in the env file (ambiguous)")
    if found and len(found) >= 2 and found[0] == found[-1] and found[0] in "\"'":
        found = found[1:-1]
    return found or None


def default_clock() -> datetime:
    return datetime.now(UTC)


def _worktree_main_checkout(root: Path) -> Path | None:
    """The main checkout of a git worktree, read from the `.git` FILE (`gitdir: <main>/.git/worktrees/<name>`).
    Only a file is read; no subprocess (nothing to hang or hijack). None when `root` is not such a worktree."""
    try:
        text = (root / ".git").read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:") :].strip().splitlines()[0] if text[len("gitdir:") :].strip() else "")
    if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
        return gitdir.parent.parent.parent
    return None


def autonomous_present() -> bool:
    """True when `.autonomous` is present at the repo root (same root as tasks.py) or at the main checkout when this
    code runs from a git worktree: found from the `.git` file of the worktree, or from the layout
    `<main>/.claude/worktrees/<name>`. "Present" is `os.path.lexists`, so a broken symlink or an odd entry counts.
    Limit: a worktree whose `.git` file is missing or unusual AND which lives outside `.claude/worktrees/` is only
    checked at its own root (no `git` subprocess is run to find more)."""
    roots = [ROOT]
    if ROOT.parent.name == "worktrees" and ROOT.parent.parent.name == ".claude":
        roots.append(ROOT.parent.parent.parent)
    main = _worktree_main_checkout(ROOT)
    if main is not None:
        roots.append(main)
    return any(os.path.lexists(r / ".autonomous") for r in roots)


def run_fetch(
    argv: list[str],
    *,
    root: Path = HOLDOUT_DIR,
    out: TextIO | None = None,
    err: TextIO | None = None,
    transport_factory: TransportFactory = RequestsTransport,
    token_provider: TokenProvider = env_token,
    clock: Clock = default_clock,
    autonomous: Callable[[], bool] = autonomous_present,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    ap = argparse.ArgumentParser(prog="python tasks.py holdout-fetch", allow_abbrev=False)
    ap.add_argument("--yes-fetch", action="store_true", help="make the real GitHub requests (default: plan only)")
    ap.add_argument(
        "--skeleton", action="store_true", help="offline: print a labels skeleton for unlabelled fetched cases"
    )
    ap.add_argument("--candidates", type=Path, default=None, help="default: holdout/candidates.json")
    args = ap.parse_args(argv)
    store = Store(root)

    if args.skeleton:
        from driftgate.eval.holdout_skeleton import print_skeleton

        return print_skeleton(root, out=out, err=err)

    cpath = args.candidates or (root / "candidates.json")
    try:
        candidates = load_candidates(cpath)
        rows = make_plan(candidates, store)
    except (CandidateError, FetchError) as exc:
        print(f"cannot plan: {exc}", file=out)
        if isinstance(exc, CandidateError) and "not found" in str(exc):
            print(
                'create it first: {"schema_version": 1, "candidates": [{"repo": "owner/name", "run_id": 123}]}',
                file=out,
            )
        return 2
    print(format_plan(rows, cpath.name), file=out)
    if not args.yes_fetch:
        print("\nnot fetching: pass --yes-fetch (human-run; uses the read-only token HOLDOUT_FETCH_TOKEN).", file=out)
        return 2

    if autonomous():
        print("refusing: holdout-fetch makes network requests and is human-only, and .autonomous exists.", file=out)
        return 1
    try:
        token = token_provider()
    except FetchError as exc:
        print(f"{exc}; nothing was requested.", file=out)
        return 2
    if not token:
        print(f"{TOKEN_ENV} is not set (environment or the gitignored env file); nothing was requested.", file=out)
        return 2

    def say(msg: str) -> None:
        print(msg.replace(token, "[token]"), file=out)

    try:
        gh = ReadOnlyGitHub(transport_factory(), token)
    except FetchError as exc:
        say(f"cannot start: {exc}")
        return 2
    today = clock().astimezone(UTC).date().isoformat()
    _, known = store.scan()
    failures = 0
    print("", file=out)
    for row in rows:
        c = row.candidate
        label = f"{c.repo} run {c.run_id}"
        if row.skipped_as:
            say(f"  REFUSED  {label}: already fetched as {row.skipped_as}; nothing written")
            continue
        assert row.case_id is not None
        try:
            record, existing = fetch_one(c, row.case_id, gh, store, today, known, token)
        except RateLimited as exc:
            say(f"  STOPPED  {label}: {exc}")
            say("stopped at the rate limit; rerun later (already stored cases are skipped, ids keep counting up).")
            return 1
        except StepLogError as exc:
            failures += 1
            say(f"  NOT STORED  {label}: {exc}. Nothing was written for this case; fetch it by hand.")
            continue
        except FetchError as exc:
            failures += 1
            say(f"  FAILED   {label}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - never print arbitrary exception text near a credential
            failures += 1
            say(f"  FAILED   {label}: unexpected {type(exc).__name__}")
            continue
        if existing:
            say(f"  REFUSED  {label}: the failed job is already fetched as {existing}; nothing written")
            continue
        src = record["source"]
        known[(src["repo"].lower(), src["run_id"], src["job"])] = record["case_id"]
        ex = record["extraction"]
        others = ", ".join(repr(o["name"]) for o in record["other_failed_steps"])
        extra = f"; other failed step(s) not stored: {others}" if others else ""
        nested = f"; {ex['inner_group_lines']} inner ##[group] line(s) kept" if ex["inner_group_lines"] else ""
        say(
            f"  {record['case_id']}  stored  {c.repo} run {c.run_id} job {src['job']!r} "
            f"step {record['step']['name']!r} (#{record['step']['number']}, attempt {record['run_attempt']})"
            f"\n      job log {ex['job_log_lines']} lines; stored lines {ex['start_line']}-{ex['end_line']} "
            f"({ex['stored_lines']} lines, {record['log_bytes']} bytes); error at line {ex['error_line']} "
            f"{ex['error_timestamp']}; step window {record['step']['started_at']}..{record['step']['completed_at']}; "
            f"method {ex['method']}{nested}{extra}"
        )
    say(f"\n{gh.requests_made} request(s) made. Next: label by hand, then `python tasks.py holdout-validate`.")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    return run_fetch(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
