# BLOCKERS

Anything that stopped work. The Stop hook treats a failing mvp-check item as accounted for only if its name or milestone appears here.

## Blocked

## Needs human


- Confirm real claude-sonnet-5-5 prices and account rate limits in src/driftgate/llm/config.py before any live spend (M2).
- Decide N for the abort ceiling "blast radius over N executions" (M4 uses 10; PRD gives no value).

### M1 (generator) - Needs human: real error strings I could not vouch for verbatim

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
- Label conflict (M3): generator labels flaky-test failures Tier 0 via flake precedent, but their only signature is the generic exit code, and invariant 3 requires a deterministic signature match. Decide: give the flaky fault a real, verbatim deterministic signature (needs a real sample), or relabel those cases escalate in the generator/eval.
- Redaction is regex-only; harden with a corpus of real secret shapes before the first `record` run.
- Abort ceiling in the orchestrator (M5): `Orchestrator` builds SafetyGate with ceiling N = `FLEET_MIN_EXECUTIONS - 1` (= 2), so any fleet-wide correlation (3+ executions) escalates, as the PRD's "fleet-wide problems are not for an agent to fix" requires. The M4 default of 10 would have let a fleet of 3 to 10 executions through. Still your call: it is one constant (`FLEET_ABORT_CEILING` in `src/driftgate/orchestrator.py`).
- Tier 2 (M5): the orchestrator has no lock table and no holder-liveness source, so `lock_holder` is never `dead` and every Tier 2 proposal is downgraded to an escalation with "holder death not proven deterministically". Correct and safe, but Tier 2 can never execute until a deterministic holder-liveness source exists (post-MVP).

### M8a (GitHub adapter) - Needs human

- **Throttling line (before M8b).** The PRD wants the `scenario/throttle` failure to print a throttling line copied verbatim from a real log. `playground/throttle/.github/workflows/throttle.yml` uses the `aws_throttling` shape from `src/driftgate/generator/error_catalog.json` (`verified: false`, recalled), with `DescribeStacks` and `2` filled in as run-specific values. Replace it with a captured real line and note its source. Also unverified: whether the real line is the one an AWS CLI step prints, and whether the operation name is plausible for `ThrottlingException`.
- **Every GitHub REST shape is unverified against the live API.** `tests/github/fixtures/` are hand-written stubs of the documented shapes. M8b should record real responses (run, attempt, jobs, job log redirect, contents, refs, PR creation, comments, rate-limit headers) and compare. See `docs/design/M8a.md`, "Open doubts".
- **Governance mapping rule is a guess.** `GitHubActionsSource` reports `approval_rejected` for a run whose jobs all ended `failure` with no steps and no runner. How GitHub really reports a rejected environment deployment (conclusion, steps, runner, annotations) was not verifiable offline. Capture a real rejected run.
- **Flaky scenario vs invariant 3.** `scenario/flaky` fails with only the runner's generic `Process completed with exit code 1.`, which is not a deterministic signature, so invariant 3 (Tier 0 needs a deterministic signature match AND flake precedent) means the agent must escalate rather than re-run it. This is the same label conflict logged under M3. The PRD's "Tier 0 re-run via flake precedent, verified green" for this scenario cannot happen under invariant 3 unless a signature is added; decide before `e2e-live`.
- **Provider-pin scenario wording.** The PRD says the constraint "resolves to an incompatible provider version"; `playground/provider-pin` uses a constraint that matches no release (`~> 9.0`), which is the failure shape already in the error catalog. Confirm that is what you want.
- **Token scopes.** Re-run and workflow dispatch need Actions write on the fine-grained PAT, which the PRD's scope list (Actions, Contents, Pull requests read/write, Workflows write) covers only if "Actions" is read/write. Confirm when creating the token.

### Blockers pass B (reviewer failed 3 rounds) - Needs human

- **Tier 3 `check_diff` secret detection is too narrow (`src/driftgate/tier3.py:65-74`).** Since PR files are no longer redacted (that was lossy on code), `check_diff`'s five `secret_literal` patterns are the only protection for committed code. These shapes pass it and would be committed: Slack `xox*-` tokens and `hooks.slack.com` webhooks, Stripe `sk_live_`/`rk_live_`, Google `AIza`, `glpat-`/`github_pat_`, JWTs, `user:pw@host` URL credentials, passwords under 12 chars or starting with `@` etc., and key names like `client_secret_blob`. Proposed fix (not applied): reuse the `redaction.py` detectors as reject-only (flag a line if `redact(line) != line`) and add a test per shape. Risk to weigh: the broad set also flags benign lines such as `token: ${{ secrets.GITHUB_TOKEN }}` and `password = var.db_password`, so those need an allow rule or they will escalate legitimate secret-scope and variable fixes.
- Minor: `.env` read architecture test misses `.env.local` and constructed names; partial guard.
- Everything else in pass B was confirmed by the reviewer (round 3): free-text-only PR redaction, files built from checked `new_contents`, audit no longer imports tools, mid-scenario cap, gate ceiling required.
- **RESOLVED (user approved, fresh round, redesigned after review):** `check_diff` rejects an added line when the raw line matches a vendor-shape floor pattern, or when the broad redaction detectors (`redact(line) != line`) fire on the line with whole-value references neutralised. Allowed whole-value references (the entire value, quoted or not): `${{ x.y }}`, `${VAR}`, `${var.x}`, `$word`, paths rooted at `secrets.` `vars.` `env.` `inputs.` `var.` `local.` `data.` `module.` `each.` `github.` (e.g. `password = "data.xxxxxxxxxxxx"`), `os.environ["X"]`, `process.env.X`. Lines containing the text `[REDACTED]` and lines over 4000 characters are rejected (fail closed). Residual risks: a password shaped exactly like an attribute path (`data.hunter2hunter2`) is treated as a reference; lines are scanned one at a time, so a secret split across lines is missed; a token glued to a word character (including `_AKIA...`) escapes the ``-anchored AWS pattern. The old generic pattern no longer flags `password = local.x`. `.env.*` names covered by the architecture test.
