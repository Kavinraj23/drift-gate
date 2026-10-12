# DriftGate offline evaluation: agent vs. deterministic baseline

Mode: `auto` (replay where fixtures exist, otherwise the scripted fake model). Agent model source: fake (no fixture), none, replay. The agent column mixes recorded real-model responses (replay) with SCRIPTED cases.
Cases: 33 labelled synthetic scenarios (dev and held-out reported separately).

## Honesty

**Read this before the numbers.** For every scenario with a recorded fixture, the agent column is the REAL model's recorded responses, replayed through the gateway: real model behaviour on a small, generated, template-based dataset, one recorded sample per scenario from a stochastic model. It is not a success rate over many runs, `n` is shown beside every figure, and every held-out cell is too small to read as a rate. A scenario recorded more than once replays its latest recording, which can be a better draw than the first. The scripted fake model still drives the loop drills and any scenario without a fixture (the model source line says which), and the seeded bad-diff reviewer is scripted. Replay spends nothing; the recordings did cost real money. The baseline column is the deterministic pipeline with no model calls. The synthetic dataset is generated and its error text is partly unverified (BLOCKERS.md). No production traffic is involved.

Figures are `k/n = rate`. A trailing `*` marks n < 10: too small to read as a rate. Every held-out cell is small.

## Metrics

| metric | baseline / dev (n=24) | agent / dev (n=24) | baseline / held-out (n=9) | agent / held-out (n=9) |
| --- | --- | --- | --- | --- |
| False remediation rate (all cases; lower is better) | 0/24 = 0% | 0/24 = 0% | 0/9 = 0%* | 0/9 = 0%* |
|   of which: acted where label says escalate/close | 0/15 = 0% | 0/15 = 0% | 0/4 = 0%* | 0/4 = 0%* |
|   of which: acted with a different action than the label | 0/9 = 0%* | 0/9 = 0%* | 0/5 = 0%* | 0/5 = 0%* |
| Remediation success (verified / attempted) | n/a (no verifier) | 8/8 = 100%* | n/a (no verifier) | 3/3 = 100%* |
|   proxy: label-correct share of actions taken | 3/3 = 100%* | 8/8 = 100%* | 2/2 = 100%* | 3/3 = 100%* |
| Remediation recall (label-remediate cases fixed correctly) | 3/9 = 33%* | 8/9 = 89%* | 2/5 = 40%* | 3/5 = 60%* |
| Escalation precision | 15/21 = 71% | 15/16 = 94% | 4/7 = 57%* | 4/6 = 67%* |
| Classification accuracy | 23/24 = 96% | 20/24 = 83% | 9/9 = 100%* | 9/9 = 100%* |
| Layer accuracy | 24/24 = 100% | 24/24 = 100% | 9/9 = 100%* | 9/9 = 100%* |
| Tool efficiency (resolved with cheap tools only) | 18/18 = 100% | 3/23 = 13% | 6/6 = 100%* | 3/7 = 43%* |
| Recovery rate after a failed first attempt | n/a (never retries) | 1/5 (drills, dev)* | n/a (never retries) | n/a (no held-out drills) |

Flaky-test failures (generic exit code only, no deterministic signature) are labelled escalate under invariant 3, so declining them counts as correct. They are scored like any other escalate case.

## Cost and latency per case

| per case (mean) | baseline / dev | agent / dev | baseline / held-out | agent / held-out |
| --- | --- | --- | --- | --- |
| input tokens | 0 | 18,489 | 0 | 15,473 |
| output tokens | 0 | 1,651 | 0 | 1,618 |
| USD (price table, as claude-haiku-4-5-20251001; nothing was spent) | $0.0000 | $0.0267 | $0.0000 | $0.0236 |
| tool calls | 2.2 | 5.8 | 1.9 | 4.9 |
| model calls | 0.0 | 4.2 | 0.0 | 3.6 |
| latency (s; scripted model, not meaningful offline) | 0.0 | 0.0 | 0.0 | 0.0 |

## Verification loop (agent only)

Computed over 38 cases: the labelled scenarios plus 5 scripted loop drills (a wrong first fix is injected, because the real gate stops most before they run).

- Remediation success (verified / attempted): 12/16 = 75%
- Recovery rate (verified fix after a failed first attempt): 1/5 = 20%*
- Correct escalation after a failed attempt (not counted as recovery): 2/5 = 40%*
- Not recovered: 2/5 = 40%*
- Wrong first fix stopped by the gate before it ran: 2/4 = 50%*
- Baseline: not applicable; it never verifies or retries.

## Tier 3 PR quality (agent only; the baseline cannot write a diff)

- Diffs proposed on labelled scenarios: 9; matching the injected fault's correct fix (by resulting-file hash): 3; correct diffs approved: 3; correct diffs rejected: 0.
- Seeded bad diffs (27 runs, 7 kinds, strict scripted reviewer): caught 27/27 (no PR opened), missed 0. Several kinds are stopped by deterministic diff checks before the reviewer sees them; the reviewer here is scripted, so a real reviewer's catch rate is unmeasured.

## What this does and does not show

- Shows: how the recorded real model behaved on the replayed scenarios, end to end through the same gates, against the deterministic baseline, with the price-table cost of what it actually used.
- Does not show: run-to-run variance (one recording per scenario), generalization beyond the generator's templates, behaviour on production logs, or a real reviewer's catch rate on seeded bad diffs (scripted). The loop-drill rows are scripted.
