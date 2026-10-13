# N1 runner: running the agent on the stored real-log holdout

Branch `mvp2/n1-runner`. N1 is NOT ticked: there are still no real logs, labels or real-model runs.

## What was built

- `src/driftgate/eval/holdout_source.py`: `HoldoutSource`, a read-only `ExecutionSource` for ONE stored case. It is built
  from a `HoldoutInput` (case id, log text, repo, run id, job, step name, commit sha, start/finish time and where the time
  came from). The type has no field for a layer, classification, action, rationale or split, and the module does no file
  I/O and does not import the holdout store. `get_execution`, `get_failed_leaf_nodes` and `get_step_logs` serve the real
  stored text (one failed job, one failed leaf step). `list_executions` returns only this execution, so `fleet_correlate`
  and `flake_history` return honest empties (no peers, no precedent); `read_file` raises `SourceError` ("not available");
  `list_changes` is empty. Nothing is synthesized.
- `src/driftgate/eval/holdout_run.py` (`python tasks.py run-holdout`, offline): the loader `inputs_for` (the only place a
  label-bearing case becomes agent-visible input; it reads `case.id` and `case.raw["source"]` only, enforced by an AST
  test that also catches aliases and `getattr`/`vars`/`dict`/`.get`/`.items` forms), the real `Orchestrator` and
  `Investigator` over a `ProposalOnlyTarget` (no capability: `dry_run` declines, `execute`/`verify`/`rollback` raise; no
  adapter is imported, so nothing can reach GitHub), the mapping to the holdout vocabulary (`rerun`, `pr_fix` with the
  catalog fix name, `escalate`, plus layer and classification), preflight, run records and sidecar.
- `src/driftgate/eval/holdout_meta.py`: the sidecar's provenance fields and the check `eval-holdout` runs (see
  "Provenance").
- `src/driftgate/autonomy.py`: the shared `.autonomous` check (see "Targets").
- `src/driftgate/eval/holdout_record.py` + `run_record_holdout` in `eval/live.py` (`python tasks.py record-holdout`,
  HUMAN ONLY): estimate, spend, caps, fixtures. Only `eval/live.py` builds a gateway or loads credentials.

## Targets (round 2: fixed dispatch)

`record-holdout` and `run-holdout` are in `RUNNER_TARGETS` (and `record-holdout` is in `HUMAN_ONLY`). They are NOT in
`LIVE_TARGETS` (pinned by tests to `{record, eval-live, e2e-live}`) or `ARG_TARGETS`; `tasks.main` dispatches them by
name, before the generic branches:

- `record-holdout [...]` runs `python -m driftgate.eval.live record-holdout [...]` (`--estimate`, `--yes-spend`,
  `--cap-usd`, `--tools`, ...). Plain `record-holdout` never runs the offline runner and never spends: it prints "not
  spending: pass --yes-spend" (exit 2).
- `run-holdout [...]` runs `python -m driftgate.eval.holdout_run [...]` (offline).
- Under `.autonomous` `record-holdout` is refused by `tasks.py` and again inside `live.main`. The check is
  `autonomy.autonomous_present_at(root)`: `os.path.lexists` (a dangling symlink counts) at the root, at the main
  checkout when the root is `<main>/.claude/worktrees/<name>`, and at the main checkout named by the worktree's `.git`
  file. `holdout_fetch.autonomous_present` has the same rules but is bound to its own repository root; a test pins the
  two to the same answers.

## Models

- `--mode fake`: a deterministic scripted double (`plumbing_model`) that reads only tool results and reproduces the
  deterministic baseline's decision through the real tools, gate and mapping. It shows plumbing, not model quality.
  **Fake-mode runs never write run records or a sidecar** (round 2: the `--allow-fake-records` path is removed), so a
  scripted double's decisions cannot reach `holdout/runs.json` as `system: agent`.
- `--mode replay`: the real `Gateway` in replay mode over `fixtures/replay-holdout/` (no client, no key, no SDK). A case
  with no fixture, or a stale one, is reported as "without fixture" and not run.
- `--mode auto` (default): replay where a fixture exists, otherwise the double (whose result is printed, not recorded).
- Live recording is `record-holdout` only.

## Gates and what is scored (round 2: no lift)

Gate inputs are exactly what the production derivation produces; nothing is invented. A SINGLE-JOB run is 100% of its
resource set (`proposed_set_size == resource_set_total == 1`), so the abort ceiling refuses a Tier 0 re-run there. That
is the honest gate outcome. A run with k failed jobs of N is judged on the real counts (next section) and is refused by
the ceiling exactly when k == N (a single-job run, or every job failed). The earlier `assume_other_jobs` lift is REMOVED, with its code, flag and text.

Each case therefore records two things side by side:

1. `action_after_gates` is the headline. It is the `action` of the run record in `holdout/runs.json` and the only thing
   scored in the main table.
2. `proposal_before_gates` (sidecar, `runs_meta.json`) is what the agent proposed (`rerun` / `pr_fix` / `escalate`, tier
   and action), plus the gate's decision, its reason text and a short reason code, and `refused_by_abort_ceiling_only`
   (the gate refused on the 100%-of-set ceiling AND every other condition of the proposal's OWN tier held, read from the
   gate's own inputs, nothing re-decided. Tier 0: signature match, known-transient rule or flake precedent, no earlier
   failed retry, blast radius within the ceiling, at most one shared resource. Tier 1: never (its facts are not derivable
   from one stored log). Tier 2: lock holder dead, death proof and dry run ok plus the common checks, which are unset on a
   holdout source, so false there. Tier 3: never, the gate does not apply the ceiling to a PR). A thin `FactsRecordingOrchestrator` only remembers
   the facts it handed the gate.

`eval-holdout` prints the headline table and, below it, a block "Proposal before gates (ungated; NOT the headline)" with
counts of rerun / pr_fix / escalate and `refused by the abort ceiling only`: per case for dev, aggregate only for the
test split.

Tier 3 is scored on the proposal (non-empty diff and paths) with `allow_unreviewed_tier3`: no repository content is
stored, so there is nothing to diff-check and no reviewer. A proposal without a diff or paths counts as `escalate`.

## Records and provenance (round 2)

`holdout/runs.json` entries follow the unchanged schema; new records are appended and the latest wins per case at load
time. They are written, with their sidecar entry, as soon as each case finishes (atomic replace; `runs.json` first, then
`runs_meta.json`), so a crash after spending keeps the earlier cases. A gateway-failed case writes no record.

`holdout/runs_meta.json` (sidecar, keyed by case id, version 2) holds `provenance` (`recorded_real_model`), mode
(`record` = live, `replay` = served from fixtures that `record-holdout` wrote), model, fixture id, `record_sha256` (the
digest of the exact run record), prompt fingerprint, tools and their backing, model/tool calls, tokens, cost, and
`proposal_before_gates`. No label, no log text.

`eval-holdout` builds the agent column only if EVERY latest run record has a matching real-model entry. It refuses (exit 1,
counts only, no case ids, no numbers) when a record comes from a fake/scripted double, has no sidecar entry (unknown
provenance), or does not match its entry (digest mismatch: changed or appended by hand after the run, or not marked as a
recorded real-model run). So a later fake record cannot silently replace a real one: the latest record is the one
checked, and it has to carry its own matching entry. The report header states the provenance: model, how many cases were
replayed from fixtures versus recorded live, and the tool set. For replayed records the model is taken from the
fixtures' `manifest.json` entry for that case (written by `record-holdout`), never from the default config; a manifest
with no model gives `model: unknown`, and the header says so plainly.

Recovery when `eval-holdout` refuses because a run record has no sidecar entry (a crash between the two writes): no
payment is needed. Run `python tasks.py run-holdout --mode replay --rerun --write --cases <ids>` for the affected cases;
it rewrites both files from the recorded fixtures (offline, no key). The refusal message states this (counts only, no ids).

The eval-holdout report puts a caveat directly under the headline table (headline rerun recall is 0 by construction on
runs where every job failed, single-job runs included; a run with k failed jobs of N is refused exactly when k = N)
when any case was refused by the ceiling alone, when any case is a known single-job or all-jobs-failed run, or when it
cannot tell (an agent run without a recorded ungated
proposal). Dev lines show `jobs in run (failed/total)` per case, e.g. `3/6 failed`; the test split gets counts only ("k of n
test cases have every job failed or are single-job runs (a single-job runs, b all-jobs-failed runs)", plus how many have no stored job count).

Limit: replay provenance rests on the fixtures having been written by `record-holdout`; a hand-made fixture directory is
not detected (fixtures are keyed by the request hash and the prompt fingerprint, not signed).

## Refusals and sealing

- Hard refusals (exit 1, reasons contain no label content): structural problems (`validate`), no cases, `labeled_at` not
  strictly before the run time for every case, `labels.json` with uncommitted changes, or an unknown git state (fail
  closed). Git status is injectable.
- Test split: it is run (and priced) through `Holdout.test_aggregate`; only a count line is printed
  ("test (sealed: aggregate only)"): no ids, outcomes, reasons, signatures or log text. Dev cases print one line each.
  The estimate prints dev cases individually and one aggregate for the test split.
- Stop lines (round 2): when the case that stops a recording is in the test split, the line is generic ("stopped: cap
  reached" or "stopped: gateway error"): no estimate, no id, no exception text, since those would reveal a test case's
  size or content. Dev cases keep the detail (scrubbed of the key).
- **Per-case data exists on disk for test cases**: `holdout/runs_meta.json` (cost, tool counts, proposals before gates)
  and `fixtures/replay-holdout/` (recorded model responses) hold it. Do not open them. The runner prints this note in its
  header output, at the start and end of a recording, and in the sidecar's `notes`.

## record-holdout

`--estimate` (no key, no gateway, no network) runs the plumbing double through the real tools and prompts on each stored
log, sizes each request (chars/4), assumes 300 output tokens per call, prices with the config price table, and prints
with-margin and worst case (40k-token budget at the output rate) and the ledger. `--yes-spend` is required to spend. The
run cap `--cap-usd` (default 1.25) is checked before each case against the with-margin estimate and before every call
against actual cost; the gateway's lifetime ($2.50) and daily ($1.00) caps are the backstop and are never set or raised
here. Test cases run first, then dev; the run stops at the first cap or gateway failure and later cases are not run. A
case that hit a gateway failure gets no run record (a spend cap is not an agent decision). The key is scrubbed from
errors. Fixtures go to `fixtures/replay-holdout/` (manifest entry per case).

## Optional tools are refused

`--tools` takes `none` (default) | `similar_history` | `changes_in_window` | `both`, but anything other than `none` is
REFUSED (exit 2, message printed) for holdout runs, in both `run-holdout` and `record-holdout`. Stored real logs have no
history index and no change timeline, so those tools could only return empties; an "on" arm would be an unfair or
meaningless comparison, and a synthetic index would leak non-holdout data. The N2/N3 on/off comparison therefore stays on
the synthetic data. `runs_meta.json` records `tools: none` and `tools_backing: none`.

## Tests (tests/holdout/test_holdout_run.py, plus helpers)

Source serves only log and metadata; no label field exists on the input type; the loader reads only id and
`raw["source"]` (AST check with hostile-snippet self-tests for aliases, `.get`, `.items`, `.copy`, `dict()`, `getattr`,
`vars`, `__dict__`, walrus, passing `case` on, deepcopy and stores); honest empties through the real tool dispatcher; no
label answer field named in source/agents/tools/orchestrator; nothing under agents/tools/llm imports the runner;
runner/source have no network, SDK, adapter or environment access; proposal-only target; schema-valid records accepted
by `holdout-validate` and `eval-holdout`; fake runs write nothing and `--allow-fake-records` is gone; the gate sees the
production facts (no lift); a lone-job Tier 0 rerun is refused by the ceiling with both views recorded, and the
ceiling-only flag is false when another blocker exists; the report block counts; provenance (real accepted and stated in
the header, fake mode / scripted provenance / fake model name refused, no sidecar entry refused, a hand-appended or
altered record refused, a fake run cannot replace a real record, replay counts); sealed stop lines for cap, gateway
error and exception text (and dev detail kept and scrubbed); crash safety for both `record-holdout` and `run-holdout`;
`tasks.main` dispatch for `--estimate`, `--yes-spend`, plain, `--tools` and `run-holdout`; `.autonomous` refusal through
`tasks.main` and `live.main`, including a dangling symlink (skipped where symlinks cannot be created, plus a variant
that makes `Path.exists` lie) and the main-checkout case; parity with the fetcher's check; Tier 0 only via the
known-transient rule and an over-eager Tier 0 proposal downgraded; vocabulary mapping; optional tools refused; future /
equal / dirty / unknown-git / tampered refusals; a changed label voids its record; sealed output; estimate with no
gateway; `--yes-spend` required; test-first order; run-cap stop; gateway-failure case has no record; key scrubbing; and
a record -> replay round trip through a REAL record-mode `Gateway` (stub SDK client, fake clock, real ledger file)
then `run-holdout --mode replay`. Log text in the test holdout is copied at test time from verbatim-captured
`error_catalog.json` entries (their sources are recorded there); only structure (repos, ids, SHAs, labels) is
scaffolding, marked in `tests/holdout/holdout_helpers.py`. Records in tests come through the record path with a stub
gateway (a stand-in for the SDK), not from the fake double.

## Open doubts

1. A run where every job failed (a single-job run included) is refused by the ceiling, so headline `rerun` recall on
   such cases stays 0 by construction (correct and honest: re-running every job is 100% of the set). Other runs are
   judged on the real failed/total counts. If many chosen runs are single-job or all-failed workflows the headline rerun recall will be low whatever the agent
   does; the ungated block and the single-job / all-failed counts say so. See "Real job counts" below.
2. Fetched logs without timestamps: the execution start time falls back to the fetch date at 00:00:00Z, flagged
   `refs.time_basis = fetch_date`. It only matters for time windows, which are empty here anyway.
3. The step name and the job counts come from `holdout/fetched/<id>.json` (schema version 3) when present; with no
   record the step name is the job name and the job count is unknown (the source is then a lone job, as before).
4. `fixtures/replay-holdout/` is keyed by the request hash; changing the prompts or tools makes fixtures stale and those
   cases are reported "without fixture" until re-recorded (spend again).
5. Tier 3 `pr_fix` is scored on the proposal only; no diff is checked against real repository content.
6. The estimate comes from the plumbing double's path (3 calls); a real model may take up to 8 tool calls, hence the
   margin and the worst-case line. Order of magnitude only.
7. `holdout_meta` ties a record to its sidecar entry by digest, but a person with write access to both files can forge
   both; the guarantee is against accidents and silent replacement, not against a malicious editor. Git history is the
   real audit trail.

## Real job counts (follow-up)

- **Source.** `holdout-fetch` stores `jobs` (counts of the run's latest attempt, from the jobs list it already downloads;
  docs/design/N1-fetch.md). `holdout_jobs.load_fetched_facts` reads them strictly (schema version 3 only);
  `inputs_for` passes them to `HoldoutInput.job_counts`; preflight refuses unreadable, invalid or version 2 records
  (counts only in the message).
- **How the real total reaches the gate, with no override.** `Orchestrator._gate_facts` (read only here) sets, for Tier 0,
  `proposed_set_size = len(source.get_failed_leaf_nodes(...))` and `resource_set_total` = the number of leaves of
  `source.get_execution(...).root`. So `HoldoutSource` builds a truthful tree: when the run had more than one job the
  root is `run` with the stored job (`job` -> failed leaf step `step-1`) plus `jobs.total - 1` sibling LEAF nodes. Gate
  facts are then `jobs.failure` of `jobs.total`, produced by the unmodified derivation. Nothing edits `gates.py`, `orchestrator.py`
  or the facts (a test pins that the runner source contains no `resource_set_total=`, `proposed_set_size=` or
  `replace(facts`). The proposal sidecar records the set sizes the gate saw (`proposed_set_size`, `resource_set_total`).
- **Siblings are real metadata only.** Names and ids are `sibling-2` ... (no job names), the status is the real
  conclusion (`failed`, `cancelled`, `skipped`, `success`, `other`), listed in that fixed order so nothing says which
  real job is which. No logs, no error summary, no refs, no label fields.
- **Siblings are not executions.** `list_executions` still returns only the stored case, so `fleet_correlate` finds no
  peers: a successful sibling is never fleet evidence, and failed siblings of the same run are not the same failure in
  other executions (the fleet tool counts executions that share a join key with the same signature; a sibling job has
  neither a log nor a join key). So `blast_radius` stays 0 and the fleet-wide escalation is not tripped by the run's own
  jobs; it would trip only if real fleet data existed, which one stored log does not have. `flake_history` is likewise
  empty. Both are tested with a run that has five failed jobs.
- **Numerator and denominator (round 2 of the follow-up).** A "re-run failed jobs" action re-runs EVERY failed job, so
  `proposed_set_size` is `jobs.failure` and `resource_set_total` is `jobs.total`. `get_failed_leaf_nodes` returns the
  stored job's `step-1` first, then exactly `jobs.failure - 1` failed sibling leaves (conclusion `failure`); cancelled,
  skipped, success and other siblings keep their own status (`cancelled`, `skipped`, `success`, `other`) and are not
  failed leaves. The tree and the failed-leaf list agree exactly (property-tested over several shapes). A run with k
  failed jobs of N is refused by the ceiling exactly when k == N: single-job runs and runs where every job failed (4 of
  4, 6 of 6). Round 1 returned only `step-1` as failed, which gave 1 of N for an all-failed run and let it through; that
  flattered the agent and is fixed. Accepted cost: lower headline rerun recall (a human decision).
- **Sibling leaves carry no log.** Reading one raises `SourceError`. `FailureAnalyzer._analyze` and
  `Orchestrator._holder_proof` already treat that as empty text and an unknown signature of the lowest rank, so the
  fingerprint, signature and classification still come from `step-1`'s real text only (tested: the analysis with five
  failed siblings equals the analysis with none). `get_step_logs` defaults to `step-1` and returns an error for a
  sibling node id.
- **Tree-shape caveat (read agent-vs-baseline comparisons with this in mind).** The model now sees N generic
  `sibling-k` job leaves, some of them failed with no logs. That can change investigator behaviour: extra tool calls
  aimed at siblings, or escalating because it cannot see a failed sibling's logs. The baseline never sees the tree, so a
  gap between the agent and the baseline on multi-job holdout runs may partly come from this tree shape and not from
  the quality of the diagnosis. No recordings exist yet, so no replay fixture is stale; any recording made from here on
  is made with this tree shape.
- **Tests** (`tests/holdout/test_holdout_jobcount.py`): sibling counts and conclusions, no label or join-key fields,
  tool honesty (fleet, flake, get_execution), gate facts 1 of 6 and 3 of 6 allowed, 1 of 2 allowed, 1 of 1, an unknown count, and all-failed 2/2, 4/4 and 6/6 refused
  by the ceiling alone, cancelled/skipped/other siblings not counted, tree and failed-leaf list agreement over several
  shapes, analysis unchanged by failed siblings, five failed siblings do not trip the blast radius (the set is 5 of 6), the v3 loader, and the
  report text.
