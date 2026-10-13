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
  `session.get(..., allow_redirects=False)` (asserted by a source scan; no `post/put/patch/delete/request`).
- **Three paths only**, built from strictly validated parts and re-checked against an allow-list before every send:
  `/repos/{o}/{r}/actions/runs/{id}`, `.../runs/{id}/jobs` (fixed query `filter=latest&per_page=100[&page=N]`),
  `.../actions/jobs/{id}/logs`. Host must be exactly `api.github.com` over https (no userinfo, no port, no fragment).
  Owner `[A-Za-z0-9-]{1,39}` not starting with `-`; repo `[A-Za-z0-9_.-]{1,100}` and not `.`/`..`; ids are `int` > 0
  (bool, str, float rejected).
- **Redirects**: only the logs endpoint, one hop, to https on an allow-listed host, sent WITHOUT `Authorization`.
  Allow-list (`BLOB_HOST_PATTERNS`): `pipelines.actions.githubusercontent.com`,
  `pipelinesghub<xx><N>.actions.githubusercontent.com`, `productionresultssa<N>.blob.core.windows.net`. Anything else,
  a second redirect, or a redirect from another path is refused. Error messages name only the offending host, never
  the signed URL.
- **Bounds**: 30 s timeout; 2 MiB cap for JSON, 32 MiB for a log; jobs paged up to 5 x 100.
- **Rate limit** (429, or 403 with `x-ratelimit-remaining: 0`, `retry-after`, or "rate limit" in the body): the whole
  batch stops with the reset time. No retry, no sleep. Cases already stored stay; ids keep counting up on the next run.
- **Token**: `HOLDOUT_FETCH_TOKEN` from the process environment, else only that key from the gitignored `.env`
  (`dotenv_values`, so nothing is exported). Used only in the `Authorization` header. Every exception message is built
  from fixed text plus a status code or host; unexpected exceptions print their type name only; the CLI also masks the
  token in anything it prints. `eval/holdout_fetch.py` was added to the architecture test's `ENV_READERS` for this.

### Behaviour
- **Plan by default** (no token needed, zero requests): validates `holdout/candidates.json` against the schema and
  strict repo/id checks, prints the ids it would allocate and the request count (4 per run), exits 2 like `e2e-live`.
  `--yes-fetch` is the only way to touch the network.
- **Per run**: run metadata (must be `completed`/`failure`; `head_sha` is 40 hex; `html_url` must equal the requested
  repo/run), jobs list, pick the job (named, or the single failed one; several failed and none named is refused, naming
  them), first step with conclusion `failure`, job log, extract, store.
- **Ids**: `hc-NN`, max used + 1 over `labels.json` ids, `logs/*.log` and `fetched/*.json`, in input order; gaps are never
  refilled. Files are created with `xb`; identical bytes are tolerated, different bytes are refused.
- **Duplicates**: the same repo (case-insensitive) + run + job as an existing label or fetched record is REFUSED with the
  existing id and nothing is written (exit 0, so a re-run after a partial batch is clean). An unnamed job is checked after the
  jobs list, before the log download.
- **Never labels**: `labels.json` is only read (ids and sources). The code path that stores a case has no label field.
  `--skeleton` prints placeholders (`FILL_ME`, empty rationale) that fail `holdout-validate`.

## Exactly what is verbatim and what is transformed
The stored `holdout/logs/<id>.log` is the failed step's lines from the job log, with ONLY:
1. UTF-8 decode, one leading BOM removed (undecodable bytes are an error);
2. ANSI escape sequences removed (`logtext.strip_ansi`);
3. credential-shaped text redacted (`redact_lines` then `redact`; the result must be a fixed point of `redact` because
   `holdout-validate` requires it, else the case is refused). A private-key block makes the case refused rather than
   stored with lines cut out.
Kept exactly as received: line endings (CRLF stays CRLF; files are written as bytes), the per-line timestamps, `##[...]`
markers, blank lines, everything else. Selecting the step's lines is itself a cut of the whole log (below).

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
`##[error]Process completed with exit code 1.` line; timestamps and `##[group]` framing are marked as structural
scaffolding in `tests/holdout/fetch_helpers.py` (invariant 9).

## Open doubts
- **Never exercised against real GitHub.** The job-log format, the `steps[]` timestamps and the redirect hosts are from
  documentation and memory. The first `--yes-fetch` should be a single candidate; read the stored log against the run
  page before fetching 30. The redirect allow-list may need a host added (the refusal says which host).
- **Step extraction is a heuristic.** The API names the failed step but the log has no step names: the section is the
  step's `started_at`..`completed_at` window (second resolution), trimmed at the runner's `##[group]Run`/`Post` headers.
  Weak points: a step that fails in under a second next to a neighbour; steps whose header is not `Run `/`Post `
  (e.g. `Set up job` failures, composite actions) are cut by timestamps only; a tool that prints its own
  `##[group]Run ...` inside the step truncates the section at it; log lines without timestamps are attributed to the
  previous stamped line.
- **Multi-failure.** Only the first `failure` step is stored; `other_failed_steps` records how many more exist. A
  matrix run with several failed jobs must name the job; one case per job needs one candidate per job.
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
