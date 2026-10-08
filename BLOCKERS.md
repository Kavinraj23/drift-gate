# BLOCKERS

Anything that stopped work. The Stop hook treats a failing mvp-check item as accounted for only if its name or milestone appears here.

## Blocked

## Needs human


### M1 (generator) - Needs human: real error strings I could not vouch for verbatim

Left out of `src/driftgate/generator/error_catalog.json` per invariant 9. Please copy a real sample (with its source) for each and tell me, then the eval agent can add the signature:

- Governance / approval rejected or expired (Harness or GitHub Environments): no log text was invented. The governance scenario carries only the `approval_rejected` execution status and a `rejected` node, with no log file. Pre-filter must key on status.
- Terraform `Error: Inconsistent dependency lock file` (full text of the provider-lock mismatch block). The lockfile scenario currently uses npm `npm ci` only.
- GitHub Actions unresolvable action/reusable workflow ref (the template-not-found shape).
- Kubernetes `Unschedulable` / `Evicted` / `FailedScheduling` event text.
- Terraform `Error: Value for undeclared variable` (the `-var` flag variant).
- Please also spot-check wrap points and spacing of: the `Error acquiring the state lock` block (esp. the `ConditionalCheckFailedException` line and closing paragraph), the `Failed to query available provider packages` two-line body, and the `kubectl describe` State/Reason/Exit Code indentation. Wording is recalled from widely-posted real output but I have no network to confirm byte-for-byte.
- PRD ambiguity: the PRD's first bullet says the synthetic repos have a "lockfile mismatch" and "broken provider pin" Tier 3 scenario but gives no exact error text for either; npm EUSAGE and Terraform "no available releases match the given constraints" were used.
