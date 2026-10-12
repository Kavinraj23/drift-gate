# DriftGate live evaluation (REAL model): agent vs. deterministic baseline

Mode: `live` (real model through the gateway). Agent model source: claude-haiku-4-5-20251001, none.
Cases: 33 labelled synthetic scenarios (dev and held-out reported separately).
Ran 33; not run (budget or reserve): none. Spend this run: $0.9219; tokens in/out: 648,851/54,613.

## Honesty

**Read this before the numbers.** The agent column in THIS report is a REAL model (investigator: claude-haiku-4-5-20251001, reviewer: claude-haiku-4-5-20251001) called through the gateway, on the same synthetic scenarios and the same simulated remediation target as the offline report; nothing was run against production. It is not a scripted model, so these numbers, unlike the offline ones, say something about the model: but only on this small, generated, template-based dataset, with `n` shown beside every figure. A trailing `*` marks n < 10, which is too small to read as a rate; every held-out cell is small. Cost, tokens and latency are measured from each response's `usage` and wall clock, and spend was recorded in the persistent ledger ($0.9219, reserve $0.25 kept). A single run is one sample from a stochastic model. The baseline column is the deterministic pipeline with no model calls. Known label conflicts (BLOCKERS.md) still count as misses in the first table. Scenarios the budget did not reach are listed as not run and are absent from every n. This file never replaces the offline `report.md`.

Figures are `k/n = rate`. A trailing `*` marks n < 10: too small to read as a rate. Every held-out cell is small.

## Metrics

| metric | baseline / dev (n=24) | agent / dev (n=24) | baseline / held-out (n=9) | agent / held-out (n=9) |
| --- | --- | --- | --- | --- |
| False remediation rate (all cases; lower is better) | 0/24 = 0% | 0/24 = 0% | 0/9 = 0%* | 0/9 = 0%* |
|   of which: acted where label says escalate/close | 0/15 = 0% | 0/15 = 0% | 0/4 = 0%* | 0/4 = 0%* |
|   of which: acted with a different action than the label | 0/9 = 0%* | 0/9 = 0%* | 0/5 = 0%* | 0/5 = 0%* |
| Remediation success (verified / attempted) | n/a (no verifier) | 6/6 = 100%* | n/a (no verifier) | 3/3 = 100%* |
|   proxy: label-correct share of actions taken | 3/3 = 100%* | 6/6 = 100%* | 2/2 = 100%* | 3/3 = 100%* |
| Remediation recall (label-remediate cases fixed correctly) | 3/9 = 33%* | 6/9 = 67%* | 2/5 = 40%* | 3/5 = 60%* |
| Escalation precision | 15/21 = 71% | 15/18 = 83% | 4/7 = 57%* | 4/6 = 67%* |
| Classification accuracy | 23/24 = 96% | 19/24 = 79% | 9/9 = 100%* | 9/9 = 100%* |
| Layer accuracy | 24/24 = 100% | 24/24 = 100% | 9/9 = 100%* | 9/9 = 100%* |
| Tool efficiency (resolved with cheap tools only) | 18/18 = 100% | 4/21 = 19% | 6/6 = 100%* | 1/7 = 14%* |
| Recovery rate after a failed first attempt | n/a (never retries) | 0/0 (drills, dev)* | n/a (never retries) | n/a (no held-out drills) |

Flaky-test failures (generic exit code only, no deterministic signature) are labelled escalate under invariant 3, so declining them counts as correct. They are scored like any other escalate case.

## Cost and latency per case

| per case (mean) | baseline / dev | agent / dev | baseline / held-out | agent / held-out |
| --- | --- | --- | --- | --- |
| input tokens | 0 | 20,152 | 0 | 18,355 |
| output tokens | 0 | 1,687 | 0 | 1,570 |
| USD, real (from response usage, price table in llm/config.py) | $0.0000 | $0.0286 | $0.0000 | $0.0262 |
| tool calls | 2.2 | 6.0 | 1.9 | 5.1 |
| model calls | 0.0 | 4.3 | 0.0 | 4.1 |
| latency (s, model calls only, wall clock) | 0.0 | 18.7 | 0.0 | 18.6 |

## Verification loop (agent only)

Computed over the 33 cases that ran (no scripted loop drills in a live report).

- Remediation success (verified / attempted): 9/9 = 100%*
- Recovery rate (verified fix after a failed first attempt): n/a (n=0)
- Correct escalation after a failed attempt (not counted as recovery): n/a (n=0)
- Not recovered: n/a (n=0)
- Wrong first fix stopped by the gate before it ran: 2/2 = 100%*
- Baseline: not applicable; it never verifies or retries.

## Tier 3 PR quality (agent only; the baseline cannot write a diff)

- Diffs proposed on labelled scenarios: 7; matching the injected fault's correct fix (by resulting-file hash): 2; correct diffs approved: 2; correct diffs rejected: 0.
- Seeded bad-diff drills use a scripted reviewer and were not run here.

## What this does and does not show

- Shows: how the configured real model behaved on these scenarios, end to end through the same gates, with real cost and latency.
- Does not show: generalization beyond the generator's templates, behaviour on real production logs, run-to-run variance (one run), or anything about a different model.
