# N1 (fetch half): `holdout-fetch`

Branch `mvp2/n1-fetch`. N1 is NOT ticked in TASKS.md: the data still has to be chosen, fetched and labelled by a human.

## What was built

A human-run target that stores, for each human-chosen public failed run, the failed step's log and its source
metadata. Nothing else.

| File | Responsibility |
| --- | --- |
| `src/driftgate/eval/holdout_gh.py` | Read-only GitHub client over an injected GET-only transport |
| `src/driftgate/eval/holdout_steplog.py` | Cut the failed step out of a job log; ANSI strip and redaction (pure) |
| `src/driftgate/eval/holdout_fetch.py` | Candidates file, plan, ids, storage, CLI (`--yes-fetch`, `--skeleton`, `--candidates`) |
| `src/driftgate/eval/holdout_skeleton.py` | Offline labels skeleton from `holdout/fetched/*.json` (metadata only) |
| `contracts/holdout_candidates.schema.json` | NEW contract: the candidates document and the fetched record. `holdout.schema.json` is untouched |

### Placement (a deviation from the brief, on purpose)
The brief said `src/driftgate/holdout_fetch.py`. The existing test `test_holdout_is_referenced_only_in_eval` fails any
file outside `eval/` that mentions "holdout", and weakening it was not allowed. `eval/` is also the only place that may
touch `holdout/` (invariants 8 and 15). So the modules are in `eval/`. Nothing in `agents/`, `tools/`, `llm/` can import
them (existing "no `eval` import" tests plus `tests/holdout/test_holdout_fetch_architecture.py`).

### Security shape
- **Not the playground adapter.** No import of `adapters/`, `github_client`, any `RemediationTarget`, the gateway or
  the SDK (architecture test). The playground adapter and invariant 12 are untouched.
- **GET only, by construction**: the `Transport` protocol has one method, `get`. The single real call is
  `session.get(..., allow_redirects=False)` (source scan: no `post/put/patch/delete/request`). The real
  `RequestsTransport` is also exercised offline with an injected fake session: `allow_redirects=False`,
  `trust_env=False`, the timeout is passed, the body is streamed and the size cap stops reading at the first chunk past
  the limit, `requests` exceptions are re-raised as `FetchError` from None (no URL, no token), exactly one attempt, and
  the transport itself refuses an `Authorization` header for any host other than the GitHub API host.
- **Three paths only**, built from strictly validated parts and re-checked against an allow-list before every send:
  `/repos/{o}/{r}/actions/runs/{id}`, `.../runs/{id}/jobs` (fixed query `filter=latest&per_page=100[&page=N]`),
  `.../actions/jobs/{id}/logs`. Host must be exactly `api.github.com` over https (no userinfo, no port, no fragment).
  Every host/path pattern is ASCII-only (`[0-9]`, `re.ASCII`; tested with Arabic-Indic digits). Owner `[A-Za-z0-9-]{1,39}` not starting with `-`; repo `[A-Za-z0-9_.-]{1,100}` and not `.`/`..`; ids are `int` > 0
  (bool, str, float rejected).
- **Redirects**: only the logs endpoint, one hop, to https on an allow-listed host, sent WITHOUT `Authorization`.
  Allow-list (`BLOB_HOST_PATTERNS`): `pipelines.actions.githubusercontent.com`,
  `pipelinesghub<xx><N>.actions.githubusercontent.com`, `productionresultssa<N>.blob.core.windows.net`. Anything else,
  a second redirect, or a redirect from another path is refused. Error messages name only the offending host, never
  the signed URL.
- **Bounds**: 30 s timeout; 2 MiB cap for JSON, 32 MiB for a log; jobs paged up to 5 x 100.
- **Rate limit** (429, or 403 with `x-ratelimit-remaining: 0`, `retry-after`, or "rate limit" in the body): the whole
  batch stops with the reset time. No retry, no sleep. Cases already stored stay; ids keep counting up on the next run.
- **Token**: `HOLDOUT_FETCH_TOKEN` from the process environment, else from the gitignored `.env`. The file is opened and
  read line by line; each line is matched against one anchored pattern for that key and dropped, only the matching
  value is kept (no `dotenv` parsing, no `${VAR}` interpolation, nothing exported; a test shows other keys never land in
  anything the module holds). The OS still reads the whole file; the code just does not parse or keep it.
  **Duplicates:** the environment variable wins over the file. In the file `HOLDOUT_FETCH_TOKEN` may appear at most once:
  python-dotenv takes the LAST occurrence, a first-match reader the FIRST, so a duplicate is ambiguous and is an explicit
  error (`could not read the token: ... appears N times`; the values are never printed). A file that is not valid
  UTF-8 (or unreadable) gives `could not read the token` with exit 2 and no traceback. A leading BOM is ignored so the
  first key still matches.
  Used only in the `Authorization` header. Every exception message is built from fixed text plus a status code or host;
  unexpected exceptions print their type name only; the CLI masks the token in anything it prints, and the exact token
  string is also replaced by `[REDACTED]` in the log text before storage (on top of the pattern redaction). `eval/holdout_fetch.py` was added to the architecture test's `ENV_READERS` for this.

### Behaviour
- **`.autonomous`**: `tasks.py` refuses the target, AND `holdout_fetch` itself refuses any `--yes-fetch` (exit 1, before the
  token is looked up or a transport is built) when `.autonomous` exists at the repo root (or at the main checkout when
  run from a git worktree: found from the worktree's `.git` FILE (`gitdir: <main>/.git/worktrees/<name>`, read as a file,
  with no `git` subprocess that could hang or be hijacked) or from the `<main>/.claude/worktrees/<name>` layout;
  presence is `os.path.lexists`, so a dangling symlink named `.autonomous` counts. Limit: a worktree with an unusual
  `.git` file outside `.claude/worktrees/` is checked at its own root only), so `python -m driftgate.eval.holdout_fetch --yes-fetch` is covered too.
- **Plan by default** (no token needed, zero requests): validates `holdout/candidates.json` against the schema and
  strict repo/id checks, prints the ids it would allocate and the request count (4 per run), exits 2 like `e2e-live`.
  `--yes-fetch` is the only way to touch the network.
- **Per run**: run metadata (must be `completed`/`failure`; `head_sha` is 40 hex; `html_url` must equal the requested
  repo/run), jobs list, pick the job (named, or the single failed one; several failed and none named is refused, naming
  them), the failed step (the only one that concluded `failure`, or the one the candidate names with `step`), job log, extract, store.
- **Ids**: `hc-NN`, max used + 1 over `labels.json` ids, `logs/*.log` and `fetched/*.json`, in input order; gaps are never
  refilled. Files are created with `xb`; identical bytes are tolerated, different bytes are refused.
- **Duplicates**: the same repo (case-insensitive) + run + job as an existing label or fetched record is REFUSED with the
  existing id and nothing is written (exit 0, so a re-run after a partial batch is clean). An unnamed job is checked after the
  jobs list, before the log download.
- **Never labels**: `labels.json` is only read (ids and sources). The code path that stores a case has no label field.
  `--skeleton` prints placeholders (`FILL_ME`, empty rationale) that fail `holdout-validate`.

## Extraction (`holdout_steplog.py`, method label `error-anchor-v1`)
The section is chosen from the log's own structure and cross-checked against the REST `steps[]` data; there is NO
fallback to a raw time window. Each rule refuses the case rather than guessing; nothing is written and the output says
`NOT STORED <repo> run <id>: <reason>. ... fetch it by hand` (exit 1):
1. Window = `started_at` floored to the second .. `completed_at` + 1 s. Anchor = the `##[error]` line(s) whose OWN
   timestamp is in the window. None (or only un-timestamped or out-of-window ones) -> refuse.
2. Start = the single step-opening header (`##[group]Run `, `Post `, or a runner phase) with its own timestamp in the
   window before the last window error. Zero -> refuse. More than one -> refuse (the same-second neighbour and the
   composite-action cases: an inner `##[group]Run` is a second header in the window).
3. No other step-opening header, timestamped or not, may sit between the start and the last error (a nested header
   outside the window is still caught). A window error before the start header belongs to an earlier step -> refuse.
4. A runner phase step (`Set up job`, `Initialize containers`, `Stop containers`, `Complete job`), or a phase header as
   the start, is refused: their logs have no `##[error]` framing I can source.
5. Another failed step whose window contains the anchor's timestamp -> refuse.
6. End = the last window error (+ one directly following `##[endgroup]`). After it there must be the end of the log or a
   step-opening header; other output means the step continues -> refuse.
7. A job whose `run_attempt` differs from the run's is refused.
8. Several failed steps and no `step` in the candidate -> refuse (see Multi-failure).
9. A step-opening header WITHOUT a timestamp anywhere in the region before the chosen start header (walking back until a
   line stamped before the window) -> refuse: the step's own header may be the untimestamped one, and the chosen
   header an inner one, which would store a truncated section.
10. The first non-blank thing after the error being `Post job cleanup` or `Cleaning up orphan processes` -> refuse with
   the "unrecognised runner framing" message instead of the generic "output continues" one. Nothing new is accepted.
Ordinary `##[group]TITLE` lines inside the step are kept and counted (`inner_group_lines`).

### Supplying a real sample (how to replace the assumptions)
Because the header and timestamp framing is assumed, the extractor can only be made trustworthy from a real log. The
human can supply one: open a failed run's job page, download the raw log, REDACT it by hand (secrets, tokens, internal
names), and put it under `tests/github/fixtures/real/<name>.log` with a sibling `<name>.source.txt` recording the run
URL, the date, what was redacted and that the framing lines were not altered. The implementer then derives the
header/boundary forms from it, adds tests that read that file, and may replace the scaffolding in
`tests/holdout/fetch_helpers.py`. No agent creates this file. One sample containing a `Post` step and the final cleanup
lines is the most useful.

Fetched record (`holdout/fetched/<id>.json`, schema version 3, no log text): source block, `job_id`, `run_attempt`,
`step` {name, number, started_at, completed_at}, `other_failed_steps` [{name, number}], `extraction` {method,
`job_log_lines`, `start_line`, `end_line` (1-based, counting newline characters only), `error_line`, `error_timestamp`,
`inner_group_lines`, `stored_lines`}, `log_sha256`, `log_bytes`, `jobs` (see below). The loader (`holdout_steplog.extraction_inconsistency`,
used when a record is written and by `--skeleton`) enforces what JSON Schema cannot: `start_line <= error_line <=
end_line <= job_log_lines` and `stored_lines == end_line - start_line + 1`. The start header's title and timestamp are
NOT recorded (that would put log text in the record); its line number is `start_line`. The stdout line per case prints the same numbers, so a
human can open the run page and check lines `start_line`..`end_line` against the job log without trusting the tool.

## Exactly what is verbatim and what is transformed
The stored `holdout/logs/<id>.log` is the failed step's lines from the job log, with ONLY:
1. UTF-8 decode, one leading BOM removed (undecodable bytes are an error);
2. ANSI escape sequences removed (`logtext.strip_ansi`);
3. credential-shaped text redacted (`redact_lines` then `redact`; the result must be a fixed point of `redact` because
   `holdout-validate` requires it, else the case is refused). A private-key block makes the case refused rather than
   stored with lines cut out.
Kept exactly as received: line endings (CRLF stays CRLF; files are written as bytes), the per-line timestamps, `##[...]`
markers, blank lines, everything else. Selecting the step's lines is itself a cut of the whole log (see Extraction). The exact fetch token, if it occurs, is
replaced by `[REDACTED]` first.

## Tested (`tests/holdout/test_holdout_fetch_*.py`, offline, stub transport, no sleeping)
Hostile owner/repo/run/job ids and URLs (traversal, query, userinfo, port, other verbs' paths); every request of a full
fetch is an allowed GET; Authorization absent on the blob request; redirect host matrix; single redirect; size caps; four
rate-limit shapes and a plain 403; token shape; paging; step extraction (neighbour steps in a shared second trimmed), CRLF,
BOM, ANSI, redaction (credential never reaches any stored file or output), private key refusal; sha256 equals what
`holdout.py` validates and a filled-in skeleton passes `validate`; id allocation across labels/logs/fetched; refusal and
idempotence; matrix job naming; bad run metadata; expired logs (410); partial-batch continuation; clock-injected fetch
date; token never in output, files or exception text; `labels.json` bytes unchanged; plan mode makes zero requests and
needs no token; `--yes-fetch` required and unknown options rejected; skeleton prints metadata only; HUMAN_ONLY refusal under
`.autonomous`; architecture (no import from agents/tools/llm, no adapters/targets, no write verbs, env only in the CLI).

Log text in tests is the verified `docker_pull_rate_limit` catalog entry plus the runner's own
`##[error]Process completed with exit code 1.` line. Everything else (timestamps, `##[group]` / `##[endgroup]`,
`Set up job`, `Post Run`, `Complete job`, `Post job cleanup.`) is INVENTED structural scaffolding, listed as such in
`tests/holdout/fetch_helpers.py` (invariant 9).

Round 3 tests (`tests/holdout/test_holdout_fetch_hardening.py`): step selection (two failed steps refused with the list,
name and number select, early continue-on-error scenario, passing/unknown step refused, schema validation of `step`),
untimestamped own header, `Post job cleanup` / orphan-line refusal, CRLF / lone CR / BOM / 1 MB line layouts, a passing
neighbour echoing an error in the same second, `.env` duplicates / non-UTF-8 / BOM, `.autonomous` via lexists and the
worktree `.git` file, cross-field record consistency.

Round 2 tests (`tests/holdout/test_holdout_fetch_extraction.py`): adversarial layouts (neighbour with its own header in
the same second before the step, neighbour after, adjacent and distant failing steps, failed `Set up job` and other
phase steps, composite action with a nested header, nested header outside the window, missing / un-timestamped /
out-of-window `##[error]`, an earlier step's error in the window, trailing output), the record and stdout fields against
the stored bytes, refusal writes nothing, token masking, `.autonomous` inside the module, `.env` reading, Arabic-Indic
digits, the real transport with a fake session, and the skeleton failing closed when `labels.json` is unreadable (a
missing file still means nothing is labelled).

## Open doubts
- **Never exercised against real GitHub.** The job-log format, the `steps[]` timestamps and the redirect hosts are from
  documentation and memory. The first `--yes-fetch` should be a single candidate; read the stored log against the run
  page before fetching 30. The redirect allow-list may need a host added (the refusal says which host).
- **Step extraction is anchored, but its log framing is ASSUMED.** See "Extraction" above. What is SOURCED: the error
  text in the tests (verified catalog entries, source recorded in `generator/error_catalog.json`) and the runner wording
  `##[error]Process completed with exit code N.`. What is ASSUMED and has never been checked against a live log: the
  `YYYY-MM-DDTHH:MM:SS.fffffffZ ` timestamp prefix, the `##[group]Run ` / `##[group]Post ` header forms, the runner-phase
  headers (`Set up job`, `Initialize containers`, `Stop containers`, `Complete job`), and that a failing step ends with
  a timestamped `##[error]`. `tests/github/fixtures/job_9001.log` is itself documented as a stand-in, not a capture, so
  it is not evidence for any of this. Real post steps may start with a plain `Post job cleanup.` line and logs may end
  with `Cleaning up orphan processes`; the extractor does NOT accept those forms (nothing sourced), it REFUSES with
  "unrecognised runner framing: paste a sample so the extractor can be built from a real log". The first live fetch of
  ONE candidate is the real test; expect refusals and read the stored log against the run page. Each refusal is safe.
- **Multi-failure (human decision, round 3).** If more than one step of the job concluded `failure` the case is REFUSED
  (nothing stored) unless the candidate names the step with the optional `step` field (name or number). The first failed
  step is not necessarily the one that failed the job (an early `continue-on-error` step also concludes `failure`).
  The refusal lists the failed steps by number and name. A named step must be one of the failed steps (a passing,
  absent or ambiguous name is refused) and still needs its own `##[error]` anchor. `other_failed_steps` lists the
  rest. A matrix run with several failed jobs must name the job; one case per job needs one candidate per job.
- **Re-runs.** The jobs list is `filter=latest`: the latest attempt. A run that failed then passed on re-run is refused
  (run conclusion). `run_attempt` is recorded.
- **Logs expire after 90 days**, so `fetch_date` is the only proof the log existed then; a refetch later returns 410.
  Fetch promptly after choosing runs and commit the result.
- **Token scope.** A fine-grained token with Actions: read (and metadata) is expected to be enough for PUBLIC repos
  that the token's owner does not own? Fine-grained tokens are scoped to repositories the owner can select, which may
  not include arbitrary third-party repos; a classic token with `public_repo` may be required. Unverified.
- **Redaction can alter evidence** (a redacted token inside an error message), by design; the label's `source` still
  identifies the original run.
- Invariant 12 wording needs a human clarification (BLOCKERS.md).
- **Remaining weak points of the cut (honest list).** (a) Real GitHub framing is unverified: if a real failing step does
  not end with a timestamped `##[error]` followed by a header, every such case is refused (safe, but unhelpful). (b) The
  refusal is conservative: a legitimate step that prints a user group titled like a step header, or prints after its
  error line, is refused. (c) A user group titled `Run ...` can only cause a refusal, not a wrong cut. (d) The window is
  second-resolution; consistently wrong API timestamps would not be detected. (e) Un-timestamped continuation lines
  between start and error are kept as received. (f) The line numbers help a human verify the cut; they do not prove it.
- A present but unreadable `labels.json` stops `--skeleton` (exit 2) so no possibly test-split log is hashed.

## Job counts and `--limit` (follow-up, schema version 3)

- **Why.** The abort ceiling refuses an action that touches 100% of its resource set. For a Tier 0 re-run of one failed
  job that set is the jobs of the run, so the runner needs the REAL number of jobs the run had. The jobs list the fetcher
  already downloads (latest attempt, `filter=latest`) says so. Nothing is invented and no gate is changed
  (docs/design/N1-runner.md, "Real job counts").
- **What is stored.** `jobs`: `{total, success, failure, cancelled, skipped, other}`, counts only. No job names (beyond
  the already-recorded failed job) and no log text. `other` is every conclusion not in the four named values, including
  a missing one (in progress, `neutral`, `timed_out`, `action_required` ...). The contract is
  `contracts/holdout_candidates.schema.json` (`fetched_record`, `schema_version` const 3, `jobs` required).
- **Pagination and truncation.** The client pages up to 500 jobs. It refuses the case (nothing written) when the list is
  over that cap OR when a short page leaves fewer jobs than `total_count` says. A partial count is never stored.
- **Cross-field check** (`holdout_jobs.jobs_inconsistency`, run when a record is written and by `--skeleton`):
  `total >= 1`, `total == success + failure + cancelled + skipped + other`, and `failure >= 1` (the fetched job is a
  counted failure).
- **Version 2 is refused**, not upgraded: `holdout_jobs.load_fetched_facts` and `--skeleton` say "schema version 2 is no
  longer accepted (it has no job counts); re-fetch ...", and `run-holdout` preflight refuses with a counts-only message
  (the test split is sealed). No version 2 record exists yet.
- **`--limit N`** processes only the first N not-yet-fetched candidates in input order. N must be a positive whole
  number (argparse rejects 0, negatives, text). Case ids are allocated before the cut, so a limited run and a later one
  agree. Plan mode lists exactly the selected candidates plus a line saying how many were left out. A candidate that
  names its `job` and is already fetched is shown as REFUSED and does not use up the limit. An UNNAMED candidate cannot
  be recognised as fetched until its failed job is known at fetch time; if it turns out to be fetched already it is
  refused then and does not use up the limit either (so the plan can show it as one of the N, and one more candidate may
  then be taken; the plan says so).
- **Tests** (`tests/holdout/test_holdout_jobcount.py`): all-success siblings, mixed and unknown conclusions, no sibling
  names stored, pagination, truncated list refused with nothing written, page cap, contract v3, cross-field rules,
  version 2 refused (loader, skeleton, preflight), `--limit` (order, plan, named and unnamed already-fetched, larger than
  the list, invalid values).
