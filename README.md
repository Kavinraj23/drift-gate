# DriftGate

DriftGate is an agent that investigates failed CI pipeline executions the way an on-call platform engineer would. It gathers evidence with read-only tools, forms a hypothesis, proposes a bounded fix, acts only through deterministic safety gates, verifies the result, and re-investigates once if the fix did not work.

**Design principle: the agent decides, deterministic code verifies.** The model proposes. Code decides whether the proposal is eligible, whether the evidence is real, whether the budget allows it, and whether the fix worked. The model's confidence is recorded and never sufficient on its own.

> Status: portfolio project built solo, without production CI access. Everything offline runs on a synthetic fleet and a **scripted** model. See [Honest framing](#honest-framing-and-non-goals) and [Status and limitations](#status-and-limitations) before reading any number.

## The loop

```
failed execution
      |
 [pre-filter]  governance and non-failures closed in code, no model call
      |
 [investigator agent]  read-only tools, max 8 calls, 40k tokens
      |   report: classification, layer, hypothesis, evidence (each cites a tool call id), proposed remediation
      v
 [evidence integrity]  uncited evidence dropped in code
      |
 [SafetyGate]  deterministic eligibility; can only DOWNGRADE to an escalation
      |   kill switch (global / per tier), abort ceiling, rate limit (2 per fingerprint per hour)
      v
 Tier 0: re-run          Tier 3: diff -> [reviewer agent] -> PR (never merged by the agent)
      |                                   |
      +----------> [verify] <-------------+     re-run green / CI on the PR branch
                      |
        not fixed --> re-investigate once with both attempts --> fix, or hard escalate with evidence
```

## Architecture

```
                 +------------------- agents/ (read-only) -------------------+
 CI failure ---> | investigator --> tools/ (get_execution, classify_signature, |
                 |                  get_step_logs, read_repo_file,           |
                 |                  fleet_correlate, flake_history, ...)     |
                 | reviewer (Tier 3 only; read_repo_file only)               |
                 +---------------------------+-------------------------------+
                                             | proposal
        llm/gateway (only SDK importer; budgets, caching, record/replay)
                                             v
   prefilter -> orchestrator -> gates (SafetyGate) -> audit -> RemediationTarget
                     |                                   (SyntheticTarget | GitHub Actions adapter)
                     +--> verify (success check, one re-investigation)

   eval/ (the only reader of ground truth): baseline vs agent, metrics, report
```

Module layout (`src/driftgate/`): `domain` (types, output contract), `prefilter`, `tools/` (one thin module per agent tool), `agents/investigator`, `agents/reviewer`, `llm/gateway` and `llm/fake`, `gates`, `audit`, `verify`, `tier3`, `diffs`, `orchestrator`, `adapters/synthetic`, `adapters/github_actions`, `baseline`, `eval/`, `generator/`. JSON Schemas live in `contracts/`.

## Safety spine

| Tier | What | Gate | Offline | Live |
| --- | --- | --- | --- | --- |
| 0 idempotent | re-run the failed job | deterministic signature match AND (flake precedent OR a Tier 0 known-transient rule); max 2 per fingerprint per hour | simulated target | GitHub Actions (playground only) |
| 1 bounded platform action | refresh a token, bump a pool | single approval, dry-run first | stubbed | stubbed |
| 2 state-touching | force-unlock a state lock | holder provably dead, two-person approval; model confidence never qualifies | stubbed | stubbed |
| 3 code change | pin a provider, regenerate a lockfile, fix a variable | reviewer agent approves, then the PR is the gate | simulated target | real PR (playground only) |

Invariants, each with a test (full list in [CLAUDE.md](CLAUDE.md)):

1. Agents are read-only; only actions SafetyGate allows reach a `RemediationTarget`.
2. SafetyGate can only downgrade a proposal to an escalation, never raise a tier.
3. Tier 0 eligibility is deterministic. Agent confidence is recorded, never sufficient.
4. Agents never merge. Branches are prefixed `driftgate/`.
5. Kill switch and abort ceiling are checked on every decision; a fleet-wide blast radius escalates.
6. At most 2 remediation attempts per fingerprint per hour, then hard escalate.
7. Every evidence item must cite a tool call id from that run; uncited claims are dropped in code.
8. Ground truth is reachable only by `eval/`.
9. Error strings are never synthesized; they are copied from real sources.
10. Only `llm/gateway` imports the Anthropic SDK; the real-API budget is $2.50 total, enforced there.
11. Credentials live only in the gitignored `.env`.
12. Live GitHub work targets `drift-gate-playground` only.
13. Additive before subtractive in any multi-step remediation.

## Real vs. simulated

| Real | Simulated |
| --- | --- |
| The deterministic pipeline: pre-filter, signatures, gates, audit, verification, diff validation | The fleet, weeks of history, and Harness-specific structure (generated, seeded, labelled) |
| The gateway: budgets, price table, retries, record/replay | The model, offline: a scripted test double written from the labels |
| GitHub Actions adapter (stubbed HTTP in tests; the live run is a human step) | Tier 1 and 2 actions (gated, dry-run, audited, stubbed) |
| Error text meant to be copied from real tools (catalog still unverified, see limitations) | All CI traffic: there is no production traffic |

## How to run

Windows, Python 3.11+. The task runner is `python tasks.py <target>`.

```
python tasks.py install     # pip install -e .[dev]
python tasks.py lint        # ruff format --check + ruff check
python tasks.py test        # full pytest suite, offline, network blocked
python tasks.py data        # generate the seeded synthetic dataset and repos into data/
python tasks.py baseline    # deterministic baseline vs ground truth
python tasks.py e2e         # every scenario end to end (scripted model or replay fixtures)
python tasks.py eval        # agent vs baseline metrics table; writes data/eval/report.md and report.json
python tasks.py mvp-check   # every MVP acceptance check, pass/fail table
```

Human-run targets that need an API key or the GitHub playground (not part of the offline flow): `record`, `eval-live`, `e2e-live`, `playground-reset`. Credentials go only in a gitignored `.env`. A short walkthrough is in [docs/DEMO.md](docs/DEMO.md).

## Eval results (offline)

`python tasks.py eval` runs the deterministic baseline and the agent pipeline over the same 33 labelled synthetic scenarios (24 dev, 9 held-out, reported separately). Full output: `data/eval/report.md`.

> **The agent column is a scripted model and is correct by construction offline.** This table validates the pipeline, the gates and the metric code. It says nothing about how a real model performs; that needs the human-run `record` and `eval-live`. The baseline column is real. A `*` marks n < 10.

Label-conflict cases counted as misses:

| metric | baseline / dev (n=24) | agent / dev (n=24) | baseline / held-out (n=9) | agent / held-out (n=9) |
| --- | --- | --- | --- | --- |
| False remediation rate (all cases; lower is better) | 0/24 = 0% | 0/24 = 0% | 0/9 = 0%* | 0/9 = 0%* |
|   of which: acted where label says escalate/close | 0/12 = 0% | 0/12 = 0% | 0/4 = 0%* | 0/4 = 0%* |
|   of which: acted with a different action than the label | 0/12 = 0% | 0/12 = 0% | 0/5 = 0%* | 0/5 = 0%* |
| Remediation success (verified / attempted) | n/a (no verifier) | 9/9 = 100%* | n/a (no verifier) | 5/5 = 100%* |
|   proxy: label-correct share of actions taken | 3/3 = 100%* | 9/9 = 100%* | 2/2 = 100%* | 5/5 = 100%* |
| Remediation recall (label-remediate cases fixed correctly) | 3/12 = 25% | 9/12 = 75% | 2/5 = 40%* | 5/5 = 100%* |
| Escalation precision | 12/21 = 57% | 12/15 = 80% | 4/7 = 57%* | 4/4 = 100%* |
| Classification accuracy | 23/24 = 96% | 23/24 = 96% | 9/9 = 100%* | 9/9 = 100%* |
| Layer accuracy | 24/24 = 100% | 24/24 = 100% | 9/9 = 100%* | 9/9 = 100%* |
| Tool efficiency (resolved with cheap tools only) | 15/15 = 100% | 5/21 = 24% | 6/6 = 100%* | 1/9 = 11%* |
| Recovery rate after a failed first attempt | n/a (never retries) | 1/5 (drills, dev)* | n/a (never retries) | n/a (no held-out drills) |

Label-conflict cases excluded:

| metric | baseline / dev (n=21) | agent / dev (n=21) | baseline / held-out (n=9) | agent / held-out (n=9) |
| --- | --- | --- | --- | --- |
| False remediation rate (all cases; lower is better) | 0/21 = 0% | 0/21 = 0% | 0/9 = 0%* | 0/9 = 0%* |
|   of which: acted where label says escalate/close | 0/12 = 0% | 0/12 = 0% | 0/4 = 0%* | 0/4 = 0%* |
|   of which: acted with a different action than the label | 0/9 = 0%* | 0/9 = 0%* | 0/5 = 0%* | 0/5 = 0%* |
| Remediation success (verified / attempted) | n/a (no verifier) | 9/9 = 100%* | n/a (no verifier) | 5/5 = 100%* |
|   proxy: label-correct share of actions taken | 3/3 = 100%* | 9/9 = 100%* | 2/2 = 100%* | 5/5 = 100%* |
| Remediation recall (label-remediate cases fixed correctly) | 3/9 = 33%* | 9/9 = 100%* | 2/5 = 40%* | 5/5 = 100%* |
| Escalation precision | 12/18 = 67% | 12/12 = 100% | 4/7 = 57%* | 4/4 = 100%* |
| Classification accuracy | 20/21 = 95% | 20/21 = 95% | 9/9 = 100%* | 9/9 = 100%* |
| Layer accuracy | 21/21 = 100% | 21/21 = 100% | 9/9 = 100%* | 9/9 = 100%* |
| Tool efficiency (resolved with cheap tools only) | 15/15 = 100% | 5/21 = 24% | 6/6 = 100%* | 1/9 = 11%* |
| Recovery rate after a failed first attempt | n/a (never retries) | 1/5 (drills, dev)* | n/a (never retries) | n/a (no held-out drills) |

Reading it honestly:

- False remediation (the heaviest-weighted metric) is 0 for both systems on this dataset. For the agent that is guaranteed by the script plus the gate, so it checks that the gate and the metric work; it is not evidence of a safe model.
- The baseline can only re-run, so it fixes few of the cases that need a diff (low recall); the scripted agent covers them. That gap is the scripted agent's design, not a measured model gain.
- The 3 label-conflict cases (flaky tests labelled Tier 0 via flake precedent, but with no deterministic signature) are shown twice: counted as misses, and excluded. They are an open label question, not a model result.
- Held-out has 9 scenarios; every held-out figure is small-n.
- Cost is the price table applied to the scripted usage; nothing was spent. Latency offline is not meaningful.

## Honest framing and non-goals

Built without production CI access, using a fault-injection harness that generates labelled incidents. The same engine runs unchanged against real GitHub Actions, where re-run and pull-request remediations execute for real (on a dedicated public playground repo). Tier 1 and 2 actions are gated, dry-run and audited with stubbed execution. **No claim of production traffic, real incident volume, or team adoption.**

Non-goals: exhaustive root-causing; acting above a tier's deterministic eligibility whatever the model's confidence; remediating fleet-wide incidents (those escalate by design); paging on governance or transient classifications; irreversible action without verifiable deterministic evidence; merging PRs (a human always merges).

## Status and limitations

- Built and passing offline: generator, gateway, baseline, SafetyGate and audit, investigator and tools, Tier 3 flow and reviewer, verification and re-investigation, eval harness.
- The error-string catalog is **unverified**: strings are meant to be copied verbatim from real tools, but several still need checking against real samples (see BLOCKERS.md).
- The offline model is scripted. No real-model numbers exist yet. `record` (replay fixtures) and `eval-live` are human-run and have not been run.
- The live GitHub playground work (M8b) and live numbers (M9b) are human-only steps.
- Tier 2 can never execute: there is no deterministic lock-holder liveness source, so every Tier 2 proposal escalates (safe by design).
- 3 flaky-test scenarios have a label that conflicts with invariant 3 (BLOCKERS.md).
- No real-log holdout yet; generalization beyond the generator's templates is untested.

## Repo layout

```
src/driftgate/   package (see Architecture)
tests/           offline pytest suite (network blocked)
contracts/       JSON Schemas for the report and remediation
config/          kill switch file
docs/            PRD.md (source of truth), design/ (one note per milestone), DEMO.md
data/            generated dataset and eval output (gitignored)
tasks.py         task runner
TASKS.md PROGRESS.md BLOCKERS.md IDEAS.md   tracker, run log, blockers, out-of-scope ideas
```
