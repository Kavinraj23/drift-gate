# M8a: GitHub Actions adapter and playground workflow files

## What was built

- `adapters/github_client.py`: a guarded REST client. Every URL is built as `{api}/repos/{configured repo}{path}` from
  repo-relative paths, so a call to another repo cannot be expressed. Non-GET requests must match a short allow-list
  (re-run, re-run failed jobs, branch/ref creation under `driftgate/`, contents commits to `driftgate/` branches, PR
  creation from a `driftgate/` head, issue comments, workflow dispatch on a `driftgate/` ref). There is no DELETE, no
  PATCH, and any non-contents path that mentions a merge is refused. A per-client write budget (60) stops loops.
  Rate-limit headers (`X-RateLimit-Remaining/Reset`, `Retry-After`) are honoured with an injected clock and sleep, with a
  bounded retry count and a maximum single wait. The token lives in `GitHubConfig` (`repr=False`), is sent only in the
  `Authorization` header of API calls (not to the redirected log-storage host), and is scrubbed from error messages;
  session failures report only the exception class.
- `adapters/github_actions.py`:
  - `GitHubActionsSource`: execution = one attempt of a run, id `"<run>.<attempt>"` (a re-run is a new execution with
    `refs.retry_of`, which is what `flake_history` needs); run -> jobs -> steps tree; deepest failed leaf; job logs
    via the 302 redirect (zip also decoded); `list_executions` over `/actions/runs` with attempt expansion;
    `read_file` via the contents API with a size cap. `pipeline` is `"<owner>/<repo>:<workflow file>"`.
  - `GitHubActionsTarget`: Tier 0 `rerun_workflow` and `rerun_failed_job` (idempotent: if the run's attempt is already
    newer, nothing is posted; `dry_run` only reads), Tier 3 `open_pull_request`, and `verify` for both.
- Tier 3 order of calls: check the branch does not exist, create the `driftgate/` ref at the failing commit (additive),
  read each file's blob sha on the new branch, commit files (growing files before shrinking ones), open the PR. A branch
  that already exists is never touched. After a mid-way failure the branch is left in place (no deletion exists) and the
  case escalates.
- Verification of Tier 3 is CI on the PR branch head: the playground workflows are `workflow_dispatch`-only, so
  `verify` dispatches the failing run's workflow on the branch if no run exists for the head sha, polls (injected
  clock/sleep), and on green leaves one idempotent "Verified by CI" comment (hidden marker per sha). Red CI leaves no
  comment. Tier 0 verification polls the run for a newer attempt and reads its conclusion.
- `playground/`: six scenario directories (one workflow each; `lockfile/` and `provider-pin/` also hold `main.tf`, the
  former a mismatched `.terraform.lock.hcl`), and `README.md` (branch naming `scenario/<dir>`, the `baseline` tag,
  that pushing is human-only M8b, and the needs-human list).

## Orchestrator and shared-file changes (small, additive)

| Change | Why |
| --- | --- |
| `PullRequest` gained `files` (checked new contents, additive-first order) and `base_sha`; `PullRequestRecord` gained `simulated` (default True) | M6 follow-up 3: a real target commits the checked `new_contents`, never the model's raw diff text. `_open_pr` refuses if the check produced no content or a deletion |
| `Orchestrator(..., allow_unreviewed_tier3=False)`; with no `tier3_review` hook Tier 3 now **escalates** ("no independent Tier 3 reviewer is wired") | M6 follow-up 1a. `build_synthetic_orchestrator` forwards the flag (default False); `build_github_orchestrator` requires a reviewer and cannot set it |
| A reviewer returning `None` is recorded as `Review("reject", ...)` and escalates | M6 follow-up 1b: fail closed |
| `_validate` rejects protected declared paths (`is_protected`); `ReviewerHook` skips unsafe/protected paths when reading files for the reviewer prompt | M6 follow-up 2 |
| `Outcome.pr_opened`, audit field `opened`, reason "opened on" vs "proposed on". `kind` stays `pr_proposed` | M6 follow-up 4. A non-simulated `PullRequestRecord` means opened. Keeping `kind` unchanged avoids touching M7's `after_execution` and the e2e judge |
| The orchestrator sets `rem.dry_run["execution_id"]` (Tier 0/1 before dry run; Tier 3 when opening the PR) | The target must know which run to re-run or which workflow to dispatch; this is set by code, not taken from the model. The GitHub target also refuses an action whose `dry_run["repo"]` names any other repo |
| `base` of the PR = `refs.branch` when the source provides it (the scenario branch), else `main` | The scenario files live on `scenario/<name>` branches, so the fix must target that branch, not `main` |

Existing-test edit with justification: `tests/conftest.py::make_orch` now defaults `allow_unreviewed_tier3=True`
(`setdefault`, so a test can override). Without it, the M5/M6-era tests that run Tier 3 with no reviewer
(`tests/agents/test_orchestrator.py::test_tier3_stops_at_pr_proposed...`, `::test_after_execution_hook_sees_...`,
`tests/tier3/test_flow.py::test_without_a_reviewer_hook_the_m5_behaviour_is_unchanged`) would see the new fail-closed
behaviour. No assertion was changed, deleted or loosened; the new default is covered by
`tests/tier3/test_m8a_followups.py::test_without_a_reviewer_tier3_escalates_by_default`.

## Tests (`python -m pytest -q tests/github`: 233; plus 13 in `tests/tier3/test_m8a_followups.py`)

- Source mapping, tree, failed leaf, retry chain, statuses, prefilter interplay, cache, id validation, logs (redirect,
  token not sent to the storage host, tail truncation, zip), listing (window, filters, attempt expansion, page cap),
  file reads (cap, unsafe paths and refs refused with zero HTTP calls).
- Invariant 12: 28 near-miss repo names (case variants, suffixes, URLs, ssh form, whitespace, encoded slash) refused by the
  source and by the target, with zero HTTP calls.
- Invariant 4: no `merge`-named member on any adapter class or module, no DELETE anywhere, merge/auto-merge, force
  update, close, cancel, secrets, hooks, branch protection and writes to `main` or `scenario/*` refused before any HTTP;
  the allowed writes are exactly the documented ones.
- Rerun call shapes, idempotence, non-allowed gate decisions never reach GitHub, unsupported tiers/actions.
- PR call order (branch before commit before PR), commit built from `files` (a hostile `diff_text` never appears in any
  request), growing-before-shrinking commits, existing branch refused, unsafe paths refused, post-failure behaviour.
- Verification with a fake clock (no sleeping): rerun, timeout, CI dispatch, green comment once, red no comment,
  stale-sha runs ignored.
- Rate limits: reset wait, `Retry-After`, bounded retries, over-limit raises, plain 403 not retried.
- Token: absent from repr, URLs, params, bodies, error messages, session-failure messages, and captured logs.
- Playground: every workflow parses (a small in-test parser for the YAML subset used, because PyYAML is not an allowed
  dependency), is `workflow_dispatch`-only, uses hosted runners, has no `secrets.`, only `contents: read`.
- Architecture: `agents/`, `tools/`, `llm/` do not import `adapters`; nothing outside `adapters/` imports the GitHub
  modules; the adapter imports no HTTP library (the session is injected).

## Open doubts

1. **No GitHub shape was checked against the live API.** All fixtures are hand-written stubs (`tests/github/fixtures/README.md`).
   Least certain: the `/actions/runs/{id}/attempts/{n}` and `.../attempts/{n}/jobs` payloads; that job logs redirect with
   `302`; `head_sha` as a list filter; the `created=` range syntax; contents API `encoding`/`sha`; the `204` from
   `dispatches`; `201` from re-run endpoints; the 403 vs 429 behaviour and header names for secondary limits.
2. **Per-step logs.** GitHub serves logs per job, so a step node returns its whole job log (tail-truncated). The tools
   extract error blocks, so this should be fine, but step-level slicing is unimplemented.
3. **Governance detection** (`approval_rejected`) is a heuristic: all jobs failed, no steps, no runner. A real rejected
   deployment may look different. A false positive closes a case without a model; a miss lets the investigator see it.
4. **Verification dispatch.** Dispatching CI on the PR branch assumes the workflow file exists on that branch with
   `workflow_dispatch`, and that Actions write is on the token. A branch-protection or required-checks setup in the
   playground might want `pull_request` triggers instead; the PRD says dispatch only, so that is what the files use.
5. **Flaky scenario vs invariant 3** and the **throttle line** are logged in BLOCKERS.md.
6. **Lock file regeneration by an agent.** The agent cannot run `tofu init`; the fix it proposes is a hand-edited lock
   file (version and constraint, no hashes). Whether OpenTofu accepts a hash-less lock and CI goes green is unverified.
7. **`Execution.pipeline` carries the repo.** The orchestrator and tools pass `pipeline` as the `repo` argument, so the
   pipeline is `"<owner>/<repo>:<workflow>"` and `read_file` strips the `:workflow` part before the exact comparison.
8. **Fleet dimensions** (`connector`, `runner_pool`, `infra_def`) are `"none"` on GitHub; only `template`
   (`<workflow>@<sha>`) can correlate, which requires two or more pipelines, so GitHub fleet correlation is weak.
9. **One commit per file.** The contents API makes one commit per file; a git-trees commit would be one commit but needs
   more calls. Not important for the playground's one-file fixes.
10. **M7 merge.** M7 touches `orchestrator.py`, `verify.py`, `eval/`. Expect a trivial conflict near `_open_pr` and the
    constructor. M7's verify step needs `rem.dry_run["execution_id"]` and (for Tier 3) `dry_run["branch"]`, both set here.

## Reviewer questions for the human

1. Should Tier 3 CI verification dispatch the workflow on the PR branch (needs Actions write, current design) or should
   the playground workflows also trigger on `pull_request` (a change to the PRD's "dispatch only")?
2. Is "refuse every repo string that is not byte-identical to the configured one" the right strictness, given GitHub
   treats owner/repo names case-insensitively?

## Merge notes (mvp1/integration, M0-M7, into this branch)

Conflicts and resolutions:

- `src/driftgate/orchestrator.py`, Tier 3 review step: M8a's `_add_reviewer_usage` (separate reviewer budget) is dropped in favour of M7's shared budget (`out.report.run` is read from `out.investigation.budget`); M8a's fail-closed rule is kept (a reviewer returning `None` becomes `Review("reject", ...)`).
- `build_synthetic_orchestrator` signature: both `allow_unreviewed_tier3` (default False) and `verification` are kept. The merge also left the `verification` / `install_verification` tail after `build_github_orchestrator`'s return; it was moved back to the end of `build_synthetic_orchestrator`. `build_github_orchestrator` still requires a reviewer and cannot set the unreviewed opt-out.
- `tasks.py`: `IMPLEMENTED_MILESTONES` is M0..M7 plus M8a. PROGRESS.md and TASKS.md auto-merged (all entries kept; M8a not ticked).

Interactions:

- M7's `Verifier` treats `rollback` as an optional target protocol. `GitHubActionsTarget` has none (a PR cannot be undone and nothing merges), so a failed Tier 3 verification audits "rollback unavailable" and never merges.
- The reviewer now draws on the case's shared budget, so M8a's `ReviewerHook` needs `inv.budget`.

Test fixture change (no assertion changed): `tests/tier3/test_m8a_followups.py::test_the_reviewer_hook_never_reads_protected_or_unsafe_paths` uses a fake `Inv` that now carries `budget = InvestigationBudget()`, because M7's hook builds a `BudgetView` from it.

Checks after the merge: lint clean; full suite 774 passed, 1 skipped; github+tier3+verify 391 passed; e2e 38 scenarios, 0 mismatch; mvp-check M0..M8a pass, M9a pending.
