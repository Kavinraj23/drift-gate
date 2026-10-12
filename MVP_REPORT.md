# DriftGate MVP 1: report

**Status (2026-10-11): feature-complete, live-verified on the playground, and evaluated with a real model twice on all 33 scenarios (a recorded pass and an independent live pass).** Latest work is on branch `mvp1/m8b-live-playground` (pushed, two commits ahead of `main`, fast-forwardable); `main` has M0 to M8a plus the `e2e-live` code. Real API spend to date: $2.7995, which is $0.30 over the original $2.50 budget by the owner's explicit decision (live playground runs $0.6648; first `record` smoke test $0.0228; `record --all` about $0.88; an accidental duplicate run about $0.31; the `eval-live` pass $0.9219). Note: `tasks.py data` and `mvp-check` used to delete the spend ledger and the live audit log; fixed on 2026-10-11 and the ledger restored with a labelled carry-over row (see `PROGRESS.md`), but the live audit trail of the M8b runs is lost. No test or offline target makes a network call.

## Live verification (M8b, 2026-10-10 to 2026-10-11)

`python tasks.py e2e-live --yes-live` was run one scenario at a time against `Kavinraj23/drift-gate-playground`. All six matched their expected outcome:

| Scenario | Outcome | Notes |
| --- | --- | --- |
| approval-rejected | closed, no model call | pre-filter on the real rejected run |
| throttle | Tier 0 re-run executed, `verified=True` | echoes a real-shape AWS CLI line; no AWS account behind it |
| flaky | escalated | no deterministic signature, so invariant 3 forbids a re-run |
| provider-pin | PR #2 opened, `verified=True` | Haiku |
| undefined-var | PR #3 opened, `verified=True` | Haiku |
| lockfile | PR #4 opened, `verified=True` | Sonnet 5.5 investigator; Haiku's diff failed to apply in most of about six attempts |

PRs #2 to #4 were left open, unmerged. Each scenario was run once to a match, with several failed attempts before it. This shows the end-to-end path works on real GitHub; it is **not** a success rate. Every failed attempt was stopped by a deterministic layer before anything wrong reached GitHub (evidence integrity, the catalog check, the diff check, the reviewer's fail-closed budget, and token scopes). The fixes (call ids visible to the model, an enum of catalog actions, stricter diff instructions, reviewer prompt, redaction hardening) did not weaken any gate, budget, check or contract. Details: `docs/design/M8b.md`.

## What works, and how it was verified

Offline items below are deterministic; the live items are in the section above.

- **Pipeline end to end:** pre-filter, investigator tool-use loop, deterministic gate facts, SafetyGate, SyntheticTarget execution, audit log, verification, one re-investigation. Verified by `python tasks.py e2e`: 38 scenarios (33 labelled plus 5 loop drills), 35 ok, 3 known label conflicts, 0 mismatches.
- **Safety spine:** gate can only downgrade (property test over random incoming decisions), kill switch re-read on every decision and fail-closed, abort ceiling, 2-per-fingerprint-per-hour rate limit, Tier 0 eligibility paths, Tier 2 never executes, one shared investigation budget per case (8 tool calls, 40k tokens, 1 re-investigation, 1 revision), evidence integrity enforced in code (uncited evidence dropped; unique tool-call ids).
- **Tier 3:** deterministic diff checks plus an independent reviewer that sees only the diff, target files and hypothesis. PR record is never merged, `driftgate/` branch prefix enforced.
- **LLM gateway:** rate limits, SQLite spend ledger ($2.50 lifetime, $1.00 daily), retries, prompt-cache markers, record/replay fixtures. Verified against a stub SDK client and live (the real SDK was exercised in the `e2e-live` runs; ledger and Console agree on spend).
- **GitHub adapter:** guarded client (one configured repo, tight write allow-list, no merge/delete/patch). Verified against hand-written HTTP stubs and live: Tier 0 re-run, branch, commit and PR creation, CI dispatch on the PR branch, and verification all ran against real GitHub. Most response fixtures are still hand-written stubs; only the rejected-run fixtures are real captures.
- **Eval:** `python tasks.py eval` prints the agent-vs-baseline table and writes `data/eval/report.md`.
- **Guardrails:** `.claude/settings.json` rules and hooks (hardened against `bash -c`, `env`, `cmd /c`, `$(...)`); these blocked a subagent's attempt to run `record`.
- **Counts:** lint clean, 1282 tests passing, 1 skipped (real-symlink test; symlinks are not permitted on this Windows account, and a simulated-escape test runs instead).

### Real-model results (M9b `record`, 2026-10-11)

`python tasks.py record --all` ran all 31 model-using synthetic scenarios against Haiku 4.5 (investigator and reviewer). `python tasks.py eval` then replays those recordings against the deterministic baseline. A second, independent live pass (`eval-live`) follows in the next section.

**First complete run, pass@1: 27 of 31 scenarios matched their label.** The four misses:

| Scenario | Set | Fault | What the real model did |
| --- | --- | --- | --- |
| sc-01 | dev | lockfile mismatch | used all tool calls without reporting |
| sc-23 | dev | lockfile mismatch | diff did not apply (stopped by the diff check) |
| sc-27 | held-out | lockfile mismatch | proposed no remediation |
| sc-25 | held-out | provider pin | diff touched an unrelated file (stopped by the diff check) |

Three of the four are lockfile cases, the same weakness seen live. Every miss was an escalation: no wrong action reached any target (false remediation 0 on both sets).

**Disclosure about sc-01.** The same `record --all` command was submitted twice by accident; the duplicate was stopped partway, but it had already re-recorded sc-01, and that recording passed. Replay therefore shows sc-01 passing (3 mismatches in `e2e`, not 4). The 27/31 above is the first complete run; the replay table is slightly kinder. The duplicate also cost about $0.31.

Replay table (`data/eval/report.md`; dev n=24, held-out n=9, so held-out cells are too small to read as rates; one recorded sample per scenario, not a success rate):

| metric | baseline / dev | agent / dev | baseline / held-out | agent / held-out |
| --- | --- | --- | --- | --- |
| False remediation | 0/24 | 0/24 | 0/9 | 0/9 |
| Remediation success (verified / attempted) | n/a | 8/8 | n/a | 3/3 |
| Remediation recall | 3/9 (33%) | 8/9 (89%) | 2/5 (40%) | 3/5 (60%) |
| Escalation precision | 15/21 (71%) | 15/16 (94%) | 4/7 (57%) | 4/6 (67%) |
| Classification accuracy | 23/24 (96%) | **20/24 (83%)** | 9/9 | 9/9 |
| Layer accuracy | 24/24 | 24/24 | 9/9 | 9/9 |
| Tool efficiency (cheap tools only) | 18/18 | 3/23 (13%) | 6/6 | 3/7 |
| Mean cost per case | $0 | $0.0267 | $0 | $0.0236 |

What this says: the real agent fixes far more of the cases that need a diff than the baseline (recall 89% against 33% on dev) without a single wrong action, and escalates more precisely. It also classifies **worse** than the deterministic baseline on dev (83% against 96%), and it uses far more tools. The recovery-after-a-failed-attempt figure (1 of 5) comes from the scripted loop drills, not a real model. The seeded bad-diff reviewer test is scripted, so a real reviewer's catch rate on bad diffs is still unmeasured.

Held-out outcomes (sc-25, sc-27) were **not** used to tune any prompt, and none will be: tuning on them would contaminate the held-out set, and any prompt or tool-schema change invalidates the recorded fixtures.

Spend at this point was $1.8775 of $2.50 (before the live pass below). An incident along the way (the spend ledger being wiped by `tasks.py data`) is described in `PROGRESS.md`; it is fixed and the ledger restored.

### Second, independent live pass (M9b `eval-live`, 2026-10-11)

`python tasks.py eval-live` ran all 33 labelled scenarios again against Haiku 4.5, held-out first, with nothing skipped for budget. It wrote `data/eval/report-live.md` and left the offline report alone. It cost $0.9219 and took the lifetime ledger to **$2.7995, which is $0.30 over the original $2.50 budget**. That overspend was the owner's explicit decision, applied through `DRIFTGATE_LIFETIME_CAP_USD`; the code default and `CLAUDE.md` still say $2.50.

**Pass@1 of the live pass: 26 of 31 model scenarios matched** (the two governance cases close in the pre-filter with no model call). Compared with the first `record` run (27 of 31):

| Scenario | Fault | `record` run | `eval-live` |
| --- | --- | --- | --- |
| sc-01 | lockfile (dev) | miss: tool budget spent | miss: tool budget spent |
| sc-23 | lockfile (dev) | miss: diff did not apply | miss: diff did not apply |
| sc-27 | lockfile (held-out) | miss: no remediation | miss: escalated, told the user to regenerate the lock file |
| sc-25 | provider pin (held-out) | miss: unrelated file in diff | miss: unrelated file in diff |
| sc-30 | undefined variable (dev) | PR, verified | **miss: submitted no evidence, so it abstained** |

The same four scenarios failed in both independent passes, so they look systematic for Haiku 4.5, not noise: three lockfile cases and one provider pin. sc-30 is the one that flipped; with one sample per pass, treat that as ordinary run-to-run variance. Every miss in both passes was an escalation: **0 false remediations in 33 of 33 cases, twice.** The gate also stopped both wrong-first-fix cases before they ran.

Live-pass table (dev n=24, held-out n=9; held-out cells are too small to read as rates):

| metric | baseline / dev | agent / dev | baseline / held-out | agent / held-out |
| --- | --- | --- | --- | --- |
| False remediation | 0/24 | 0/24 | 0/9 | 0/9 |
| Remediation recall | 3/9 (33%) | 6/9 (67%) | 2/5 (40%) | 3/5 (60%) |
| Escalation precision | 15/21 (71%) | 15/18 (83%) | 4/7 (57%) | 4/6 (67%) |
| Classification accuracy | 23/24 (96%) | **19/24 (79%)** | 9/9 | 9/9 |
| Tool efficiency (cheap tools only) | 18/18 | 4/21 (19%) | 6/6 | 1/7 |
| Mean cost per case | $0 | $0.0286 | $0 | $0.0262 |
| Mean model latency per case | 0 s | 18.7 s | 0 s | 18.6 s |
| Mean tool calls per case | 2.2 | 6.0 | 1.9 | 5.1 |

The replay table above was slightly kinder because it kept the duplicate run's passing sc-01 recording (dev recall 89% there, 67% here). **The live pass is the cleaner figure.** Against the baseline: the real agent fixes about twice as many of the cases that need a diff (67% against 33% on dev), escalates more precisely (83% against 71%), never acts wrongly, but classifies worse (79% against 96%) and uses far more tools.

Caveats on the Tier 3 numbers. In the live pass 7 diffs were proposed, 2 matched the injected fix byte for byte, 3 were stopped by the checks or the reviewer, and 2 opened PRs that differ from the injected fix; the strict scorer counts those as misses, though a different diff can still be a valid fix. "Verified" for these synthetic cases comes from a CI simulation based on file paths, which is a weak signal. The live GitHub runs (three PRs with real green CI) are the stronger evidence. The recovery rate after a failed first attempt is still scripted drills only, and the seeded bad-diff reviewer test is scripted.

What this is not: two samples per scenario from one model at temperature defaults, on a generated, template-based dataset. It says nothing about other models, production logs or generalization, and the held-out set was not used for any tuning.

### Earlier offline results with the scripted agent (kept for reference)

In the earlier all-fake mode the agent is a **scripted model that is correct by construction**. The eval table validates the pipeline, the gates and the metric code. It says nothing about how a real model performs. Real agent numbers need your `record` and `eval-live` runs. The baseline column is real. The synthetic dataset's error text is mostly recalled from well-known real outputs: four catalog entries are verified against real output with sources, the rest are not byte-verified.

Headline offline results (dev set, n=24; held-out n=9 is too small to read as rates):

| metric | baseline | scripted agent |
| --- | --- | --- |
| False remediation rate | 0/24 | 0/24 |
| Remediation recall (label-remediate cases fixed) | 3/12 (25%) | 9/12 (75%) |
| Escalation precision | 12/21 (57%) | 12/15 (80%) |
| Classification accuracy | 23/24 | 23/24 |
| Recovery after a failed first attempt (verified fix only) | n/a | 1/5 (drills) |

The three label-conflict cases (sc-05, sc-07, sc-13) are counted as misses in the first table and excluded in the second. Full table: `data/eval/report.md` (regenerate with `python tasks.py eval`).

## `mvp-check`

```
check                                               milestone  status
lint + tests + hook tests                           M0         pass
seeded dataset generates                            M1         pass
gateway tests (fake clock, no SDK outside gateway)  M2         pass
baseline prints scores                              M3         pass
gate tests                                          M4         pass
e2e on replay fixtures                              M5         pass
tier 3 e2e + reviewer                               M6         pass
verification + re-investigation                     M7         pass
github adapter on stubbed HTTP (M8a)                M8a        pass
eval table + README (M9a)                           M9a        pass
```

## PRD never-cut list

| Item | Status | Note |
| --- | --- | --- |
| Pre-filter | Done | Closes governance outcomes with zero model calls; verified in e2e. |
| Investigator agent with MVP tools | Done (live, small n) | Ran against real models on all six playground scenarios (Haiku 4.5; Sonnet 5.5 for lockfile). Success rate over many cases is unmeasured until M9b. |
| SafetyGate, kill switch, abort ceiling, rate limit | Done | The abort ceiling N=2 is a judgement call (see Needs human). |
| LLM gateway with budgets | Done | Exercised live: caps and ledger agree with the Console; mixed per-role models work. Sonnet price and rate-limit values in `llm/config.py` still to confirm if Sonnet is used more widely. |
| Real Tier 0 and Tier 3 on GitHub Actions | Done | Live on the playground: Tier 0 re-run verified green, three Tier 3 PRs opened unmerged with green CI, governance closed with no model call. |
| PR reviewer agent | Partial | A real reviewer approved the three correct diffs and failed closed when its shared budget was spent. Its catch rate on bad diffs is still unmeasured against a real model (M9b). |
| Verification and re-investigation loop | Done | Offline drills for the wrong-first-fix path; live verification (`verified=True`) on the throttle re-run and on the three PRs' CI. A live wrong-first-fix recovery has not occurred. |
| Agent-vs-baseline eval on synthetic data | Done (two samples per scenario) | A recorded pass and an independent live pass, both against the baseline: 27/31 and 26/31 matched, 0 false remediations in both, the same four lockfile/provider-pin scenarios missed both times, classification worse than the baseline's. Small n; generated dataset. |

## Needs human, in order

Done since the first version of this report: Console workspace and key; flaky-test label (relabelled as escalation, invariant 3); abort-ceiling N (kept at 2); the playground (created, pushed, all six scenarios run live, PRs #2 to #4 reviewed); redaction hardening (21 new corpus cases, 9 false-positive guards; still regex-only); four error-catalog entries verified against real output.

1. **M9b is done:** `record --all`, the replay `eval` and `eval-live` all ran. Remaining judgement for you: the lifetime spend is $2.7995 against the $2.50 in `CLAUDE.md`; decide whether to update that figure or treat this as a one-time overrun. Any agent prompt or tool-schema change now invalidates the recorded fixtures.
2. Confirm real `claude-sonnet-5-5` prices and rate limits in `src/driftgate/llm/config.py` if Sonnet is used for more than a few demo cases. The Haiku-only estimate and the observed spend agree with the Console.
3. Error catalog: entries still not byte-verified (`npm_ci_lock_out_of_sync`, `tf_undeclared_variable`, `tf_provider_constraints`, the state-lock block, `kubectl describe` indentation) and the missing real governance, CI-hosted lock and reusable-workflow samples. Full list in `BLOCKERS.md`.
4. Decisions logged in `BLOCKERS.md`: whether to reserve tool calls for the reviewer (the shared 8-call budget can starve it; changing it changes the PRD budget rule); one `baseline/<name>` tag per scenario versus the PRD's single tag; the open reviewer follow-ups (tighten `_agent_ref`, allow-list redirect hosts, whether workflow-editing PRs may have CI auto-dispatched, whether a revision gets a reserved budget slice).
5. `python tasks.py playground-reset` (human-only) when you want a clean playground; it closes PRs #2 to #4 and deletes the `driftgate/*` branches, and old failed runs then need fresh dispatches.
6. Merge: `mvp1/m8b-live-playground` fast-forwards onto `main`; push `main` yourself and tag it. Update the PRD's open questions (ship date, the $2.50 budget, Haiku as the default model).

## Reviewer questions to answer before trusting each module

- **Hooks and guardrails (M0):** Are heuristic hooks plus permission rules enough for an unattended run, or should it run in a sandbox with no push credentials? Should `generator/` write ground truth through a narrow, separately tested path?
- **Gateway (M2):** Should the gateway itself hard-enforce the tool-call and re-investigation limits (it enforces only tokens)? Are the Sonnet prices and rate limits right?
- **Generator (M1):** Is the flaky pipelines' elevated failure and retry density an acceptable signal? Is "unexamined scenarios, not unseen families" what the PRD means by held-out?
- **Gate and audit (M4):** Is "PR plus reviewer" enough of a gate for Tier 3 when set-size facts are missing? Should unknown blast radius be representable and fail closed, and should "only SafetyGate produces allowed" be structural rather than a settable field?
- **Tools and baseline (M3):** Should the baseline be held to invariant 3 (as now) or deliberately act on flake precedent alone? Is regex-only redaction enough before the first real call?
- **Investigator and orchestrator (M5):** Is an abort ceiling of 2 right? Should a Tier 1 that passes the gate be reported as awaiting approval or as an escalation?
- **Tier 3 and reviewer (M6):** Should the orchestrator refuse Tier 3 with no reviewer (it now does)? Should the PR commit be built from checked content (it now is)?
- **Verification (M7):** Should a revision get a reserved budget slice so recovery is possible? Should a revision be exempt from the 2-per-hour fingerprint counter?
- **GitHub adapter (M8a):** Should workflow-editing PRs wait for human approval before CI is dispatched? Keep exact-byte repo matching?
- **Eval (M9a):** Annotate or drop the baseline's structural 100% tool-efficiency? Keep the scripted-reviewer Tier 3 numbers in the report?

## Process notes

- Every milestone ran as a fresh subagent on its own branch with an independent read-only review. Review rounds: M1 took 3 (ground-truth leakage into agent-visible files; overclaimed error-catalog sources; flaky pipelines distinguishable by name), M4 took 2 (gate could turn a refused proposal back into allowed), M7 took 3 (budget doubled across rounds; reviewer usage outside the shared budget; two revisions per case), M2, M3, M5, M6, M8a and M9a each passed on the first review (M3 got a follow-up hardening pass).
- Existing test assertions changed, each justified in PROGRESS.md and the design notes: not-implemented example targets in `tests/test_tasks.py` as targets became implemented, plus setup-only fixes at the end (an unreviewed-Tier-3 opt-out flag in `tests/conftest.py`; budget tests replaced by stricter shared-budget tests in M7; baseline recall floors lowered once the baseline was held to invariant 3). No assertion was weakened to get a pass.
- The project subagent files in `.claude/agents/` were not registered in the session that created them, so reviews ran as general-purpose agents given the reviewer's instructions.
- Design notes: `docs/design/M0.md` to `M9a.md`. Run log: `PROGRESS.md`. Open items: `BLOCKERS.md`.
