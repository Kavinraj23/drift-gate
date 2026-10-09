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
