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
  test), the real `Orchestrator` and `Investigator` over a `ProposalOnlyTarget` (no capability: `dry_run` declines,
  `execute`/`verify`/`rollback` raise; no adapter is imported, so nothing can reach GitHub), the mapping to the holdout
  vocabulary (`rerun`, `pr_fix` with the catalog fix name, `escalate`, plus layer and classification), preflight, run
  records and sidecar.
- `src/driftgate/eval/holdout_record.py` + `run_record_holdout` in `eval/live.py` (`python tasks.py record-holdout`,
  HUMAN ONLY): estimate, spend, caps, fixtures. Only `eval/live.py` builds a gateway or loads credentials.
- Targets: `run-holdout` (offline, takes options), `record-holdout` (in `HUMAN_ONLY` and `LIVE_TARGETS`; refused under
  `.autonomous` by `tasks.py` and again inside `live.main`). `docs/DEMO.md` Part 2 lists it.

## Models

- `--mode fake`: a deterministic scripted double (`plumbing_model`) that reads only tool results and reproduces the
  deterministic baseline's decision through the real tools, gate and mapping. It shows plumbing, not model quality. Its
  runs are NOT written as run records unless `--write --allow-fake-records` is passed (so they cannot be mistaken for
  agent results in `eval-holdout`).
- `--mode replay`: the real `Gateway` in replay mode over `fixtures/replay-holdout/` (no client, no key, no SDK). A case
  with no fixture, or a stale one, is reported as "without fixture" and not run.
- `--mode auto` (default): replay where a fixture exists, otherwise the double.
- Live recording is `record-holdout` only.

## Gates and what is scored

Gate facts are deterministic exactly as in the synthetic pipeline. With no history, Tier 0 can qualify only through a
deterministic signature match plus a Tier 0 known-transient rule; Tier 2 never qualifies on confidence. The one
deviation is `assume_other_jobs` (see doubts): documented in the module, printed in every report, recorded in the
sidecar. Tier 3 is scored on the proposal (non-empty diff and paths) with `allow_unreviewed_tier3`: no repository content
is stored, so there is nothing to diff-check and no reviewer. A proposal without a diff or paths counts as `escalate`.

## Records

`holdout/runs.json` entries follow the unchanged schema (`case_id`, `system: agent`, `ran_at` from the injected clock,
`label_sha256`, `layer`, `classification`, `action`); new records are appended and the latest wins per case at load time.
`holdout/runs_meta.json` (sidecar, keyed by case id) holds mode, model, tool set, `tools_backing`, fixture id, prompt
fingerprint, model/tool calls, tokens, cost and the gate assumption. No outcome text, label or log content.

## Refusals and sealing

- Hard refusals (exit 1, reasons contain no label content): structural problems (`validate`), no cases, `labeled_at` not
  strictly before the run time for every case, `labels.json` with uncommitted changes, or an unknown git state (fail
  closed). Git status is injectable.
- Test split: it is run (and priced) through `Holdout.test_aggregate`; only a count line is printed
  ("test (sealed: aggregate only)"): no ids, outcomes, reasons, signatures or log text. Dev cases print one line each.
  The estimate prints dev cases individually and one aggregate for the test split.

## record-holdout

`--estimate` (no key, no gateway, no network) runs the plumbing double through the real tools and prompts on each stored
log, sizes each request (chars/4), assumes 300 output tokens per call, prices with the config price table, and prints
with-margin and worst case (40k-token budget at the output rate) and the ledger. `--yes-spend` is required to spend. The
run cap `--cap-usd` (default 1.25) is checked before each case against the with-margin estimate and before every call
against actual cost; the gateway's lifetime ($2.50) and daily ($1.00) caps are the backstop and are never set or raised
here. Test cases run first, then dev; the run stops at the first cap or gateway failure and later cases are not run. A
case that hit a gateway failure gets no run record (a spend cap is not an agent decision). The key is scrubbed from
errors. Fixtures go to `fixtures/replay-holdout/` (manifest entry per case), records and meta are appended.

## Optional tools are refused

`--tools` takes `none` (default) | `similar_history` | `changes_in_window` | `both`, but anything other than `none` is
REFUSED (exit 2, message printed) for holdout runs, in both `run-holdout` and `record-holdout`. Stored real logs have no
history index and no change timeline, so those tools could only return empties; an "on" arm would be an unfair or
meaningless comparison, and a synthetic index would leak non-holdout data. The N2/N3 on/off comparison therefore stays on
the synthetic data. `runs_meta.json` records `tools: none` and `tools_backing: none`.

## Tests (tests/holdout/test_holdout_run.py, 33 tests, plus helpers)

Source serves only log and metadata; no label field exists on the input type; the loader reads only id and source (AST);
honest empties through the real tool dispatcher; no label answer field named in source/agents/tools/orchestrator; nothing
under agents/tools/llm imports the runner; runner/source have no network, SDK, adapter or environment access;
proposal-only target; schema-valid records accepted by `holdout-validate` and `eval-holdout` on a tmp holdout; fake
results not recorded without the flag; Tier 0 only via the known-transient rule, an over-eager Tier 0 proposal is
downgraded; vocabulary mapping; optional tools refused; future / equal / dirty / unknown-git / tampered refusals; a
changed label voids its record; sealed output (no test ids, rationales, run ids, log lines); human-only refusal under
`.autonomous`; estimate with no gateway; `--yes-spend` required; test-first order; run-cap stop; gateway-failure case has
no record; key scrubbing; and a record -> replay round trip through a REAL record-mode `Gateway` (stub SDK client, fake
clock, real ledger file) then `run-holdout --mode replay`. Log text in the test holdout is copied at test time from
verbatim-captured `error_catalog.json` entries (their sources are recorded there); only structure (repos, ids, SHAs,
labels) is scaffolding, marked in `tests/holdout/holdout_helpers.py`.

## Open doubts

1. `assume_other_jobs`: the abort ceiling refuses a Tier 0 action touching 100% of its resource set. A lone stored job
   would always trip it, making `rerun` unreachable. The runner lifts the total by one for Tier 0 only. Every other check
   is unchanged and the gate still only downgrades, but this is a judgement call on invariant 5's inputs: a human should
   confirm it (`HoldoutOrchestrator._gate_facts`; flag `assume_other_jobs=False` shows the strict behaviour).
2. Fetched logs without timestamps: the execution start time falls back to the fetch date at 00:00:00Z, flagged
   `refs.time_basis = fetch_date`. It only matters for time windows, which are empty here anyway.
3. The step name comes from `holdout/fetched/<id>.json` when present, else the job name.
4. `fixtures/replay-holdout/` is keyed by the request hash; changing the prompts or tools makes fixtures stale and those
   cases are reported "without fixture" until re-recorded (spend again).
5. Tier 3 `pr_fix` is scored on the proposal only; no diff is checked against real repository content.
6. The estimate comes from the plumbing double's path (3 calls); a real model may take up to 8 tool calls, hence the
   margin and the worst-case line. Order of magnitude only.
