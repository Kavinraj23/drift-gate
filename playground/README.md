# Playground scenario files

These are the files for the six live scenarios of the `drift-gate-playground` repo (PRD, "Playground environments").
They live here, in the project repo, only so they are versioned and tested offline (`tests/github`). Nothing in this
directory is run from here, and the project repo's own CI never executes it.

**Pushing is a human step (M8b).** The autonomous build does not create the repo, push, or run anything live.

## Layout and branch naming

Each directory is the content of one scenario branch of the playground repo:

| Directory | Branch | Injected fault | Expected outcome |
| --- | --- | --- | --- |
| `flaky/` | `scenario/flaky` | fails on attempt 1, passes on re-run | Tier 0 re-run, verified green |
| `throttle/` | `scenario/throttle` | attempt 1 prints a throttling error line and fails | Tier 0 re-run (known-transient rule), verified green |
| `lockfile/` | `scenario/lockfile` | `.terraform.lock.hcl` pins null provider 3.1.0 but `main.tf` requires 3.2.1 | Tier 3 PR regenerating the lock file |
| `provider-pin/` | `scenario/provider-pin` | `main.tf` constraint `~> 9.0` matches no null provider release | Tier 3 PR pinning a working version |
| `undefined-var/` | `scenario/undefined-var` | expression uses `inputs.relase_tag`, the declared input is `release_tag` | Tier 3 PR fixing the expression |
| `approval-rejected/` | `scenario/approval-rejected` | job gated on an environment whose reviewer rejects | pre-filter closes it, no model call |

Branch name = `scenario/` + directory name. Copy the directory contents to the root of the branch (so the workflow is
at `.github/workflows/<name>.yml`). The `baseline` tag is placed on a commit that holds every scenario's broken state
(`main` plus all six scenario branches merged or tagged per M8b), and `python tasks.py playground-reset` (human-only)
force-resets each scenario branch to it.

## Rules every workflow here follows

- Trigger is `workflow_dispatch` only. CI on an agent's `driftgate/` PR branch is started by the target dispatching the
  same workflow on that branch, so no `pull_request` trigger is needed and none is used.
- GitHub-hosted runners only (`ubuntu-latest`). No self-hosted runners.
- No repository secrets: no workflow references `secrets.`; the playground has none. Permissions are `contents: read`.
- Terraform scenarios use the `null` provider through OpenTofu (`opentofu/setup-opentofu`), so no cloud account is needed.
- Error text is never invented by us. The two failing-by-exit-code scenarios (`flaky`, `undefined-var`) print nothing
  of their own. The `throttle` scenario prints one line that is **not yet verified** (see below).

## Needs human before M8b

- **Throttle line.** The PRD wants a throttling line copied verbatim from a real log. `throttle/.github/workflows/throttle.yml`
  uses the line recalled in `src/driftgate/generator/error_catalog.json` (`aws_throttling`, `verified: false`). Replace it
  with a captured real line and its source. Logged in `BLOCKERS.md`.
- **Environment.** For `approval-rejected`, create an environment named `needs-approval` with a required reviewer, dispatch
  the workflow, and reject the deployment.
- **Third-party action.** `opentofu/setup-opentofu@v1` is the only non-GitHub action; confirm it is acceptable or pin it
  by SHA.
- **Branch protection.** `main` requires a review so nothing an agent opens can merge without a human.
- **Token.** Fine-grained PAT scoped to the playground only (Actions, Contents, Pull requests read/write; Workflows write;
  Actions write is needed for re-run and dispatch), stored only in the gitignored `.env`.
