# holdout/ (MVP 2, N1): real-log holdout

Real failed GitHub Actions runs from public repositories, used to test whether the agent generalizes beyond the
synthetic generator. **Only `src/driftgate/eval/` reads this directory** (invariant 8 extended; an architecture test
enforces it). Status: no cases yet. The data must be chosen, fetched and labelled by a human.

## Layout

| Path | What |
| --- | --- |
| `labels.json` | Versioned labels (`schema_version: 1`), validated against `contracts/holdout.schema.json`. One record per case: layer, classification, correct action (`rerun`, `pr_fix` with a named fix, or `escalate`), one-line rationale, split, source (repo, run id, job, commit SHA, URL, fetch date), `log_sha256`, `labeled_at`. No log text. |
| `logs/<case_id>.log` | The failed step's log, verbatim, with credential-shaped text already redacted. Must be unchanged by `driftgate.redaction.redact`. |
| `runs.json` | Run records of the agent on holdout cases (written by the human-run replay/record path). Each carries `ran_at` and the digest of the label it ran against. Starts empty. |

## Rules

1. 30 cases: 10 `dev`, 20 `test`. At least 4 per layer L1 to L4, and at least 3 whose correct action is `escalate`.
2. **Labels before runs.** A case's `labeled_at` must precede every run record for it, and the run record's
   `label_sha256` must still match the case's current label. Editing a label after a run invalidates that run.
3. **Test split is sealed.** Never open `test` labels or logs while developing. The code gives only aggregate
   metrics for `test`; per-case detail is available for `dev` only.
4. Logs are stored verbatim (invariant 9: never rewrite or synthesize error text), except that redaction runs first.
   Record where each log came from in its label's `source`.
5. `python tasks.py holdout-validate` checks schema, quotas, redaction, hashes and label timing.
   `python tasks.py eval-holdout` is offline and never needs the API key. `holdout-fetch` is human-only (see below).

## Fetching (human-only)

1. Put a separate read-only token in the gitignored `.env` as `HOLDOUT_FETCH_TOKEN` (never the playground token).
2. Write `holdout/candidates.json` by hand (`contracts/holdout_candidates.schema.json`):
   `{"schema_version": 1, "candidates": [{"repo": "owner/name", "run_id": 123, "job": "optional exact job name"}]}`.
   Public repositories only. A run with several failed jobs needs `job`.
3. `python tasks.py holdout-fetch` prints the plan and makes no request. `python tasks.py holdout-fetch --yes-fetch` fetches.
   It writes `logs/<id>.log` and `fetched/<id>.json` (source metadata and the log's SHA-256) and never touches
   `labels.json`. Logs expire on GitHub after 90 days; fetch promptly and commit the result.
4. `python tasks.py holdout-fetch --skeleton` prints a labels skeleton (ids, source, hash; every label field a
   placeholder that fails `holdout-validate`). It reads no log text. You choose split, layer, classification, action and
   rationale yourself and set `labeled_at`, before the agent ever runs.
