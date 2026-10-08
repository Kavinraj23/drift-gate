# DriftGate demo script

A short walkthrough. Part 1 is offline, free, and needs no credentials. Part 2 is human-only.

## Part 1: offline (about 5 minutes)

Run from the repo root on Windows with the venv active.

1. `python tasks.py install` then `python tasks.py test`
   Point out: the suite is offline and blocks the network; the invariants (read-only agents, only the gateway imports the SDK, ground truth only in `eval/`) each have a test.
2. `python tasks.py data`
   Point out: a seeded, labelled synthetic fleet (about 1,500 executions, bursts, flaky pipelines, decoy changes, a repo per pipeline with the injected fault). Same seed, same data.
3. `python tasks.py baseline`
   Point out: the deterministic cheapest-first pipeline with no model calls. This is the bar the agent must beat. It never acts wrongly here, but it only ever re-runs.
4. `python tasks.py e2e`
   Point out: every scenario goes through pre-filter, investigator, SafetyGate, target, verification. Find a Tier 3 row (`pr_proposed ... opened (approve)`), a gate-blocked wrong first fix (`first fix blocked by gate`), and the drills `d1` to `d5` (wrong first fix, then recover or hard-escalate). The `known label conflict` rows are explained in BLOCKERS.md.
5. `python tasks.py eval`
   Point out: the agent-vs-baseline table, dev and held-out separate, the label-conflict cases shown both ways, and the honesty block: the agent column is scripted and correct by construction, so this validates the machinery, not model quality. Output is also written to `data/eval/report.md` and `report.json`.
6. `python tasks.py mvp-check`
   Point out: one pass/fail row per acceptance check; milestones that need live access show as pending.

Talking points: the model never acts directly (SafetyGate can only downgrade); evidence that cites no tool call is dropped in code; the agent opens a PR and stops, a human merges; the budget and kill switch are enforced outside the model.

## Part 2: live (HUMAN ONLY, do not run autonomously)

These spend real API budget (cap $2.50 total) and touch the GitHub playground repo `drift-gate-playground` only. They need a gitignored `.env` with the key; never paste a key into a prompt or shell history.

- `python tasks.py record`: runs scenarios against the real API and saves replay fixtures.
- `python tasks.py eval-live`: the eval against the real API with a fixed spend cap; produces the numbers that should be reported.
- `python tasks.py playground-reset`, then `python tasks.py e2e-live`: reset the playground to its baseline tag and run live scenarios (a real re-run and a real PR, never merged by the agent).

Until these are run there are no real-model numbers.
