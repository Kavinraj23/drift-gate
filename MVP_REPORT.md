# DriftGate MVP 1: report

Branch `mvp1/integration` (tags `mvp1-m0` to `mvp1-m9a`). Nothing was pushed. `main` is untouched. Real API spend: $0. No real network call was made by any test or run.

## What works, and how it was verified

All verification below is offline and deterministic.

- **Pipeline end to end:** pre-filter, investigator tool-use loop, deterministic gate facts, SafetyGate, SyntheticTarget execution, audit log, verification, one re-investigation. Verified by `python tasks.py e2e`: 38 scenarios (33 labelled plus 5 loop drills), 35 ok, 3 known label conflicts, 0 mismatches.
- **Safety spine:** gate can only downgrade (property test over random incoming decisions), kill switch re-read on every decision and fail-closed, abort ceiling, 2-per-fingerprint-per-hour rate limit, Tier 0 eligibility paths, Tier 2 never executes, one shared investigation budget per case (8 tool calls, 40k tokens, 1 re-investigation, 1 revision), evidence integrity enforced in code (uncited evidence dropped; unique tool-call ids).
- **Tier 3:** deterministic diff checks plus an independent reviewer that sees only the diff, target files and hypothesis. PR record is never merged, `driftgate/` branch prefix enforced.
- **LLM gateway:** rate limits, SQLite spend ledger ($2.50 lifetime, $1.00 daily), retries, prompt-cache markers, record/replay fixtures. Verified against a stub SDK client only.
- **GitHub adapter:** guarded client (one configured repo, tight write allow-list, no merge/delete/patch). Verified against hand-written HTTP stubs only.
- **Eval:** `python tasks.py eval` prints the agent-vs-baseline table and writes `data/eval/report.md`.
- **Guardrails:** `.claude/settings.json` rules and hooks (hardened against `bash -c`, `env`, `cmd /c`, `$(...)`); these blocked a subagent's attempt to run `record`.
- **Counts:** lint clean, 798 tests passing, 1 skipped (real-symlink test; symlinks are not permitted on this Windows account, and a simulated-escape test runs instead).

### What the numbers do and do not mean

The offline agent is a **scripted model that is correct by construction**. The eval table validates the pipeline, the gates and the metric code. It says nothing about how a real model performs. Real agent numbers need your `record` and `eval-live` runs. The baseline column is real. The synthetic dataset's error text is recalled from well-known real outputs and is **not byte-verified** (every catalog entry is `verified: false`).

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
| Investigator agent with MVP tools | Partial | Built and tested with a scripted model only. Never run against a real model. |
| SafetyGate, kill switch, abort ceiling, rate limit | Done | The abort ceiling N=2 is a judgement call (see Needs human). |
| LLM gateway with budgets | Partial | Built and tested offline. Response shapes unverified against the live SDK; Sonnet prices and rate limits are placeholders. |
| Real Tier 0 and Tier 3 on GitHub Actions | Partial | Adapter built against stubs. Nothing has touched real GitHub. Live proof is M8b (human-only). |
| PR reviewer agent | Partial | Built; only a scripted reviewer has been exercised. A real reviewer's catch rate is unmeasured. |
| Verification and re-investigation loop | Done | Tier 3 CI is simulated from file paths in the synthetic target. |
| Agent-vs-baseline eval on synthetic data | Partial | Harness done. The agent column is scripted, so the comparison is not yet evidence about a real agent. |

## Needs human, in order

1. Confirm Claude Code bills to your subscription, not the API key (`/status`). Create the Console workspace (limit about $2.50) and put the key in the gitignored `.env`. Never paste it into Claude Code.
2. Confirm real `claude-sonnet-5-5` prices and your account's rate limits, then edit `src/driftgate/llm/config.py`. Current values ($3/$15 per MTok, 40 rpm, 40k tpm) are placeholders.
3. Decide the **flaky-test label conflict**: the generator labels flaky-test failures Tier 0 via flake precedent, but their only signature is the generic exit code, and invariant 3 requires a deterministic signature. Either supply a real verbatim signature or relabel those cases as escalations. The `scenario/flaky` playground case has the same problem.
4. Supply real captured error samples (with sources) for the unverified catalog entries, the throttling line for `scenario/throttle`, and governance text. The full list is in `BLOCKERS.md`.
5. Decide the abort-ceiling N. The orchestrator uses 2 (any fleet of 3 or more escalates); the gate default is 10.
6. Harden log redaction with a corpus of real secret shapes before the first `record` run (it is regex-only).
7. Run `python tasks.py record` on ONE scenario and check the real cost, then the rest, then `python tasks.py e2e` on replay, then `python tasks.py eval-live`. Changing the system prompt, tool schemas, `max_tokens` or tool result formats invalidates recorded fixtures. `record` and `eval-live` are not implemented yet. They are the only unimplemented targets, and both need your hands (see `docs/design/M5.md` for what `record` should do).
8. M8b: create `drift-gate-playground`, a repo-scoped PAT (Actions read/write, Contents, Pull requests, Workflows), branch protection, then push the `playground/` scenario branches and the `baseline` tag yourself. Record real GitHub responses to replace the stubs, and capture a real rejected-approval run. `playground-reset` and `e2e-live` are not implemented yet.
9. Decide the open reviewer follow-ups: tighten `_agent_ref` and allow-list redirect hosts; whether workflow-editing PRs may have CI auto-dispatched; whether a revision gets a reserved budget slice or is exempt from the hourly counter.
10. Review the work, merge `mvp1/integration` into `main` and push. Update the PRD's open questions (ship date, $2.50 budget, Haiku default).

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
