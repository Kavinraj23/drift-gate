# BLOCKERS

Anything that stopped work. The Stop hook treats a failing mvp-check item as accounted for only if its name or milestone appears here.

## Blocked

## Needs human


- Confirm real claude-sonnet-5-5 prices and account rate limits in src/driftgate/llm/config.py before any live spend (M2).
- ~~Abort ceiling N~~ RESOLVED (user, 2026-10-09): keep `FLEET_ABORT_CEILING` = `FLEET_MIN_EXECUTIONS - 1` (= 2) in `src/driftgate/orchestrator.py`; the M4 default of 10 is superseded.

### M1 (generator) - Needs human: real error strings I could not vouch for verbatim

**Update 2026-10-09:** real samples were found online (`docs/research/real_error_samples.md`). `docker_pull_rate_limit`, `aws_expired_token`, `aws_throttling` and `pip_no_matching_distribution` now match real output and are `verified: true` with source URLs. Further verbatim samples (undeclared variable via `-var`, saved-plan lock file, unresolvable action, FailedScheduling, environment rejection) are in the research doc only; the catalog test requires every entry to have a classifier signature, so adding them means adding signatures first. Still open: `npm_ci_lock_out_of_sync` (no `Missing:` lines sample), `tf_undeclared_variable` and `tf_provider_constraints` as currently worded (the real samples are different variants), the `Error acquiring the state lock` block, and `kubectl describe` indentation. No real log was found for the governance rejection body, a CI-hosted Terraform lock error, or reusable-workflow-not-found.


Left out of `src/driftgate/generator/error_catalog.json` per invariant 9. Please copy a real sample (with its source) for each and tell me, then the eval agent can add the signature:

- Governance / approval rejected or expired (Harness or GitHub Environments): no log text was invented. The governance scenario carries only the `approval_rejected` execution status and a `rejected` node, with no log file. Pre-filter must key on status.
- Terraform `Error: Inconsistent dependency lock file` (full text of the provider-lock mismatch block). The lockfile scenario currently uses npm `npm ci` only.
- GitHub Actions unresolvable action/reusable workflow ref (the template-not-found shape).
- Kubernetes `Unschedulable` / `Evicted` / `FailedScheduling` event text.
- Terraform `Error: Value for undeclared variable` (the `-var` flag variant).
- Please also spot-check wrap points and spacing of: the `Error acquiring the state lock` block (esp. the `ConditionalCheckFailedException` line and closing paragraph), the `Failed to query available provider packages` two-line body, and the `kubectl describe` State/Reason/Exit Code indentation. Wording is recalled from widely-posted real output but I have no network to confirm byte-for-byte.
- PRD ambiguity: the PRD's first bullet says the synthetic repos have a "lockfile mismatch" and "broken provider pin" Tier 3 scenario but gives no exact error text for either; npm EUSAGE and Terraform "no available releases match the given constraints" were used.
- Catalog entries not captured verbatim (all entries are `verified: false`; these were flagged by the reviewer as most doubtful):
  - `npm_ci_lock_out_of_sync`: real npm output usually has `npm error Missing: X from lock file` lines after the EUSAGE message; the catalog may lack them.
  - `pip_no_matching_distribution`: check the exact `ERROR: Could not find a version that satisfies the requirement` / `No matching distribution found` pair.
  - `docker_pull_rate_limit`: check the `toomanyrequests` message wording.
  - `aws_expired_token`: check the exact AWS CLI/SDK expired-token message.
  - `tf_undeclared_variable`: check the diagnostic layout and wrap.
  - `tf_provider_constraints`: check the wrap point of the two-line body.
- ~~Label conflict (M3)~~ RESOLVED (user, 2026-10-09): flaky-test failures are labelled escalate (invariant 3); generator, eval and report already do this. Same for the M8a `scenario/flaky` item below: the PRD's "Tier 0 re-run" row for it cannot hold without a deterministic signature, and none exists in public sources (`docs/research/real_error_samples.md`, Gaps).
- **Redaction hardened 2026-10-10 (before `record`).** A probe of 32 vendor and structural shapes leaked 21; `redaction.py` now covers Mailgun, Twilio, DigitalOcean, Shopify, Databricks, Vault, Sentry, Linear, Postman, Doppler, Fly.io, Telegram, Discord and Teams webhooks, kubeconfig `client-key-data`, `.netrc`, `Cookie`/`Set-Cookie`, `::add-mask::`, `--from-literal=` and `*_MASTER_KEY`. Corpus: 21 new cases and 9 false-positive guards (`tests/tools/gen_redaction_corpus.py`). Still regex-only: a secret with no vendor prefix and no credential-word key name (for example a bare random string in prose) is not caught. All corpus values are fake, built in the real format; the vendor-shaped ones are stored as fragments and joined by the test, because GitHub push protection flags contiguous token shapes even when they are fake.
- Abort ceiling in the orchestrator (M5): `Orchestrator` builds SafetyGate with ceiling N = `FLEET_MIN_EXECUTIONS - 1` (= 2), so any fleet-wide correlation (3+ executions) escalates, as the PRD's "fleet-wide problems are not for an agent to fix" requires. The M4 default of 10 would have let a fleet of 3 to 10 executions through. Still your call: it is one constant (`FLEET_ABORT_CEILING` in `src/driftgate/orchestrator.py`).
- Tier 2 (M5): the orchestrator has no lock table and no holder-liveness source, so `lock_holder` is never `dead` and every Tier 2 proposal is downgraded to an escalation with "holder death not proven deterministically". Correct and safe, but Tier 2 can never execute until a deterministic holder-liveness source exists (post-MVP).

### M8a (GitHub adapter) - Needs human

- **RESOLVED 2026-10-09 (accepted as a simulation):** the `throttle` workflow echoes the `aws_throttling` line, which now matches real AWS CLI output (aws-cli #2948, #4635; catalog entry `verified: true`); the live run printed it followed by the runner's `Process completed with exit code 1.` There is no AWS account behind the playground, so the line is a replay of a real shape, not a live response. Original note: **Throttling line (before M8b).** The PRD wants the `scenario/throttle` failure to print a throttling line copied verbatim from a real log. `playground/throttle/.github/workflows/throttle.yml` uses the `aws_throttling` shape from `src/driftgate/generator/error_catalog.json` (`verified: false`, recalled), with `DescribeStacks` and `2` filled in as run-specific values. Replace it with a captured real line and note its source. Also unverified: whether the real line is the one an AWS CLI step prints, and whether the operation name is plausible for `ThrottlingException`.
- **Every GitHub REST shape is unverified against the live API.** `tests/github/fixtures/` are hand-written stubs of the documented shapes. M8b should record real responses (run, attempt, jobs, job log redirect, contents, refs, PR creation, comments, rate-limit headers) and compare. See `docs/design/M8a.md`, "Open doubts".
- **RESOLVED 2026-10-09 (one sample):** the governance rule matches a real rejected run (jobs `failure`, `steps: []`, `runner_id: 0`); fixtures `real_rejected_*.json`. Original note: **Governance mapping rule is a guess.** `GitHubActionsSource` reports `approval_rejected` for a run whose jobs all ended `failure` with no steps and no runner. How GitHub really reports a rejected environment deployment (conclusion, steps, runner, annotations) was not verifiable offline. Capture a real rejected run.
- **CONFIRMED live 2026-10-09:** `scenario/flaky` attempt 1 failed with only the script `exit 1` and `Error: Process completed with exit code 1.`; attempt 2 passed. No deterministic signature exists, so the agent escalates; the PRD's "Tier 0 re-run" row for this scenario stays unattainable under invariant 3 (decision: escalate, see RESOLVED label conflict above). Original note: **Flaky scenario vs invariant 3.** `scenario/flaky` fails with only the runner's generic `Process completed with exit code 1.`, which is not a deterministic signature, so invariant 3 (Tier 0 needs a deterministic signature match AND flake precedent) means the agent must escalate rather than re-run it. This is the same label conflict logged under M3. The PRD's "Tier 0 re-run via flake precedent, verified green" for this scenario cannot happen under invariant 3 unless a signature is added; decide before `e2e-live`.
- **Provider-pin scenario wording.** The PRD says the constraint "resolves to an incompatible provider version"; `playground/provider-pin` uses a constraint that matches no release (`~> 9.0`), which is the failure shape already in the error catalog. Confirm that is what you want.
- **Token scopes.** Re-run and workflow dispatch need Actions write on the fine-grained PAT, which the PRD's scope list (Actions, Contents, Pull requests read/write, Workflows write) covers only if "Actions" is read/write. Confirm when creating the token.

### Blockers pass B (reviewer failed 3 rounds) - Needs human

- **Tier 3 `check_diff` secret detection is too narrow (`src/driftgate/tier3.py:65-74`).** Since PR files are no longer redacted (that was lossy on code), `check_diff`'s five `secret_literal` patterns are the only protection for committed code. These shapes pass it and would be committed: Slack `xox*-` tokens and `hooks.slack.com` webhooks, Stripe `sk_live_`/`rk_live_`, Google `AIza`, `glpat-`/`github_pat_`, JWTs, `user:pw@host` URL credentials, passwords under 12 chars or starting with `@` etc., and key names like `client_secret_blob`. Proposed fix (not applied): reuse the `redaction.py` detectors as reject-only (flag a line if `redact(line) != line`) and add a test per shape. Risk to weigh: the broad set also flags benign lines such as `token: ${{ secrets.GITHUB_TOKEN }}` and `password = var.db_password`, so those need an allow rule or they will escalate legitimate secret-scope and variable fixes.
- Minor: `.env` read architecture test misses `.env.local` and constructed names; partial guard.
- Everything else in pass B was confirmed by the reviewer (round 3): free-text-only PR redaction, files built from checked `new_contents`, audit no longer imports tools, mid-scenario cap, gate ceiling required.
- **RESOLVED (user approved, fresh round, redesigned after review):** `check_diff` rejects an added line when the raw line matches a vendor-shape floor pattern, or when the broad redaction detectors (`redact(line) != line`) fire on the line with whole-value references neutralised. Allowed whole-value references (the entire value, quoted or not): `${{ x.y }}`, `${VAR}`, `${var.x}`, `$word`, paths rooted at `secrets.` `vars.` `env.` `inputs.` `var.` `local.` `data.` `module.` `each.` `github.` (e.g. `password = "data.xxxxxxxxxxxx"`), `os.environ["X"]`, `process.env.X`. ARN shielding is off for this check (`redact(..., shield_arns=False)`; log redaction keeps it), so a token glued to an ARN-looking prefix is caught, and a plain ARN passes; value classes redaction skips for logs (`--token -x`, `--password <x>`, `DB_PWD=/x`) have their own patterns; a vendor-shaped token inside or beside any reference/placeholder/ARN is covered by a test matrix. Lines containing the text `[REDACTED]` and lines over 4000 characters are rejected (fail closed). Residual risks: a password shaped exactly like an attribute path (`data.hunter2hunter2`) is treated as a reference; lines are scanned one at a time, so a secret split across lines is missed; a token glued to a word character (including `_AKIA...`) escapes the ``-anchored AWS pattern. The old generic pattern no longer flags `password = local.x`. `.env.*` names covered by the architecture test.

### M8b (live playground) - Needs human
- **RESOLVED 2026-10-10:** `.env` repo setting, playground setup, token scopes (Actions and Pull requests read/write) and `e2e-live` on all six scenarios; see docs/design/M8b.md, "Live results".
- Confirm one `baseline/<name>` tag per scenario is acceptable against the PRD's "a tagged baseline".
- Confirm real claude-sonnet-5-5 prices in `src/driftgate/llm/config.py`: the lockfile scenario needed Sonnet for the investigator, and the spend cap is computed from that table.
- Decide whether to reserve tool calls for the reviewer (the shared 8-call budget can starve it); this changes the PRD budget rule, so it was not done.
- Review the open PRs #2 to #4 on drift-gate-playground; do not merge.

### N1 (real-log holdout) - Needs human
- The 30 real runs, `holdout-fetch` (network, human-only), and labels do not exist; `holdout-validate` fails by design until they do. Labels must be committed before any agent run on them.
- Ambiguity: the PRD says "Both stub" for the acceptance commands but does not say how the agent runs on stored logs offline. This milestone reads agent results from `holdout/runs.json` run records; a holdout source adapter and a record path are not built (see `docs/design/N1.md`).
- Ambiguity: PRD says escalation runs are "at least 3" without naming layers; the schema allows L0..L6 for them.

### N2 (`similar_history`) - Needs human
- Decision (round 2): "incident finish time". The PRD says the index holds incidents that "finished before the case". Live, `record_incident(outcome, completed_at)` now takes the time the incident COMPLETED (report, remediation and verification finished); the index is as-of completion, the conservative choice. The synthetic eval still uses the failed execution's own `finished_at` and the report says so. Human may overrule.
- Ambiguity: the shipping rule's "improves" has no numbers in the PRD. `eval/retrieval_eval.py` uses at least 3 cases of recall or verified fixes, or a 10 percent drop in mean cost per case with no loss of recall or success rate, and any rise in false remediation vetoes. Constants `MIN_CASES`, `MIN_COST_DROP`; change if you disagree.
- RESOLVED (round 2): a model could pass another execution's id to `similar_history`. The tool now takes no arguments; the case id and start time are bound by code in `Investigator.investigate`, and an unbound context refuses.
- Needs human: recorded real-model on/off fixtures (`record` with the tool offered) and a loader for a recorded arm in `eval-retrieval`; until then the verdict stays "stays off: no recorded evidence".
### N3 (change correlation) - Needs human
- Recording fixtures with `changes_in_window` enabled (`record`) and any live eval are human-only; until then no real model has used the tool and the report's N3 rows are metric-code validation only.
- Ambiguity: the PRD does not say which timestamp "first failure" means for a recurring or retried failure. Implemented as the start of the pipeline's unbroken run of same-fingerprint failures ending at the case (see `docs/design/N3.md`).
- Ambiguity: how `suspected_change.ref` identifies a change is unspecified. Scoring accepts `change_id`, a commit sha (7+ char prefix) or exact `(kind, ref, at)`; the prompt asks for the `change_id`.
- Ambiguity: the PRD's `ExecutionSource` protocol has no change-listing method. Kept additive: a separate `ChangeSource` protocol in the tool module; `ExecutionSource` unchanged.
- Ambiguity: the scored "causal" and "decoy" sets come from the generator's labels (decoys = other changes within 6h before the failure touching the pipeline's own refs). Changes in the window outside both sets count only in the blame denominator.
### Needs human / later (integration of N2 + N3)
- When the live path that calls `Orchestrator.record_incident` is built, it must take the verification-finished time from the audit log itself (the `verification` entry's `ts`), not from a caller-supplied `completed_at`. A wrong value there could let a case see an incident that was still in progress.

### N4 (Tier 1/2 approvals) - Needs human

- Tier 2 approval window: the PRD gives Tier 2 no timeout. `TIER2_TIMEOUT_S` = 60 minutes (src/driftgate/approvals.py) is my assumption; an unbounded window was not acceptable. Confirm or change.
- Tier 2 on the generated dataset: no execution records which execution holds a state lock, so the 4 labelled Tier 2 scenarios (sc-03, sc-11, sc-29, sc-33) can only be downgraded ("holder death not proven"). The allowed path is exercised on the simulated lock table of the dedicated `eval-tier12` cases. Giving the generator lock-holder data would change seeded dataset bytes and invalidate MVP 1 fixtures, so I did not. Decide whether a later dataset version should carry holders.
- The dataset has no "who triggered this run" field. The orchestrator takes a `triggered_by` resolver; with none, no approval request is opened (fail closed). The CLI cannot create requests. Decide where the trigger identity should come from for a real source (GitHub: the run's `triggering_actor`).
- Identities are unauthenticated strings (trusted local CLI). Fine for a local simulation; real approval (Slack) needs authentication first. See docs/design/N4.md.
- Old note "Tier 2 (M5): ... can never execute": superseded for the simulated lock table (N4); still true for the generated scenarios.
