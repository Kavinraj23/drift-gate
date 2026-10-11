# M8b: live playground (prep and `e2e-live`)

## Built
- `scripts/playground.py`: human-run `setup`, `sync-main`, `reset` for `Kavinraj23/drift-gate-playground` (dry run unless `--yes`, uses the user's git login, never reads the env file). One tag per scenario, `baseline/<name>`, because each workflow must sit at the root of its own branch; the PRD says "a tagged baseline".
- `python tasks.py playground-reset` runs `reset --yes`.
- `eval/live.py` `e2e-live`: plan mode by default (no network, no key); `--yes-live` investigates the newest failed run of each scenario's workflow through `build_github_orchestrator`, with the real gateway for investigator and reviewer, `ReviewerHook`, and `install_verification`. Prints expected vs. actual outcome per scenario and model spend; `--cap-usd` bounds spend on top of the gateway's lifetime and daily caps.
- `adapters/github_live.py` (composition; no HTTP import) and `adapters/http_session.py` (the only `requests` import). Needed because two architecture tests forbid HTTP imports in `adapters/github_*.py` and forbid imports of the client or actions modules from outside `adapters/github*`; neither test was changed.
- Token is read from `PLAYGROUND_PAT`, repo from `DRIFTGATE_PLAYGROUND_REPO`, only in `load_github_config`.

## Design choices
- `e2e-live` never dispatches a workflow: the client's write allow-list only permits dispatch on `driftgate/` refs. The human starts each scenario run in the browser. Its only writes are the ones the adapter already allows (re-run, `driftgate/` branch, PR, comment).
- Reset deliberately lives outside `GitHubClient`, which has no DELETE and no force push.
- Expected outcomes: flaky -> escalated (invariant 3), throttle -> executed and verified, three Tier 3 scenarios -> a real PR, approval-rejected -> closed with no model call.

## Tested
Plan mode needs no config or gateway; `.autonomous` refusal; missing GitHub config stops before any call; tasks dispatch. Real captures: rejected-deployment run, `tests/github/fixtures/real_rejected_*.json`.

## Open doubts
- The live path of `e2e-live` (listing runs, orchestrator on real executions, real PR creation, verification polling) has no offline test and has never run. First run should be `--scenario approval-rejected` (no model call), then `throttle`.
- A fresh failed run must exist per scenario; `playground-reset` does not delete old runs, so "newest failed run" is chosen by start time.
- Flaky and throttle: if attempt 1 was already re-run, the target reports nothing to do. Dispatch fresh runs and do not re-run them by hand.
- The PRD's single `baseline` tag vs. one tag per scenario needs the human's OK.

## Live results (2026-10-10, `e2e-live --yes-live`, one scenario at a time)
All six scenarios matched their expected outcome. Model spend for the whole session was about $0.67 of the $2.50 lifetime cap, most of it on failed lockfile attempts.

| Scenario | Expected | Got | Notes |
| --- | --- | --- | --- |
| approval-rejected | closed | closed | pre-filter, no model call, $0 |
| throttle | executed | executed | re-run requested, `verified=True` |
| flaky | escalated | escalated | "no deterministic signature match" (invariant 3) |
| provider-pin | pr_proposed | pr_proposed | PR #2, `verified=True`, Haiku |
| undefined-var | pr_proposed | pr_proposed | PR #3, `verified=True`, Haiku |
| lockfile | pr_proposed | pr_proposed | PR #4, `verified=True`, **Sonnet 5.5 investigator**; Haiku's diff failed to apply in most of about six attempts |

PRs #2 to #4 were left open and unmerged, as required.

## What the first live runs found (all fixed without weakening a gate)
- **Evidence integrity dropped everything.** Tool results did not show the model its own call id. Each result now carries `tool_call_id`, and the prompt says to copy it. Validation in code is unchanged.
- **Free-text `action`.** The model wrote a sentence instead of a catalog identifier; the gate refused it. The report schema now has an enum of catalog actions.
- **Tier 3 proposal without a `diff`.** The prompt now states a diff is mandatory, with a format example, and asks for exact copies of context lines, exact indentation and at most one context line either side.
- **Wasted tool calls.** A wrong path (`lockfile.yml` instead of `.github/workflows/lockfile.yml`) costs a call. The `read_repo_file` description now gives the path convention, and the prompt makes `fleet_correlate` and `flake_history` optional for code-level fixes.
- **Reviewer starved by the shared budget.** The case shares one 8-call budget, so a reviewer tool call after the investigator used all 8 failed closed. The reviewer prompt now says to judge from the diff and files it was given.
- **`e2e-live` with different investigator and reviewer models** sent the display label "a + b" as a request model. It now passes an empty model so the gateway chooses per role (regression test added).
- **Audit gained diagnostics:** per-call tool trace (tool, path@ref or error), `diff_chars`, `hypothesis`, `escalation_reason`, and the redacted rejected diff (2000 chars).
- **Token scopes:** the fine-grained PAT needed Actions and Pull requests read/write.

## Open doubts after the live run
- Haiku 4.5 is unreliable at writing applying unified diffs for lock files (it mislabels which lines change). The deterministic checks caught every bad diff before GitHub; the cost is retries. Sonnet fixed it in one attempt, on one sample.
- When the investigator spends all 8 calls, only the reviewer prompt keeps the reviewer from failing closed. Reserving calls for review would change the PRD budget rule, so it is not done.
- Checked afterwards: `record` and `eval-live` are not affected by the combined-label bug. `e2e._run` uses the label only as a display column and builds the orchestrator without a `model=`, so the gateway picks each role's model.
- GitHub REST fixtures other than the rejected-run ones are still hand-written stubs.
