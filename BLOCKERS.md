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
