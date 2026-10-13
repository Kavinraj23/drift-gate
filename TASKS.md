# TASKS

Tracker for MVP 1. Source of truth for scope is `docs/PRD.md`. Tick a box only when the milestone's Definition of Done in CLAUDE.md is met.

The "MVP acceptance checks" table below mirrors `CHECKS` in `tasks.py`; `python tasks.py mvp-check` runs them. If you change one, change the other.

## MVP acceptance checks

| Check | Milestone | Command |
| --- | --- | --- |
| lint + tests + hook tests | M0 | `python tasks.py test` |
| seeded dataset generates | M1 | `python tasks.py data` |
| gateway tests (fake clock, no SDK outside gateway) | M2 | `python -m pytest -q tests/gateway` |
| baseline prints scores | M3 | `python tasks.py baseline` |
| gate tests | M4 | `python -m pytest -q tests/gates` |
| e2e on replay fixtures | M5 | `python tasks.py e2e` |
| tier 3 e2e + reviewer | M6 | `python -m pytest -q tests/tier3` |
| verification + re-investigation | M7 | `python -m pytest -q tests/verify` |
| github adapter on stubbed HTTP | M8a | `python -m pytest -q tests/github` |
| eval table + README | M9a | `python tasks.py eval` |

When a milestone lands, add it to `IMPLEMENTED_MILESTONES` in `tasks.py` so `mvp-check` stops reporting it as pending.

## Milestones

Every milestone also needs: `python tasks.py lint` and `python tasks.py test` green, reviewer subagent pass, `docs/design/Mx.md`, a PROGRESS.md entry.

- [x] **M0 Repo skeleton.** Deps: none. Accept: `python tasks.py test`; `python -m pytest -q tests/test_hooks.py`. Parallel: none. Human: none.
- [x] **M1 Synthetic generator + synthetic repos + ground truth.** Deps: M0. Accept: `python tasks.py data` (seeded dataset, schema validation passes). Parallel-safe with M2.
- [x] **M2 LLM gateway** (rate limits, budgets, daily and lifetime caps, retries, caching, record/replay). Deps: M0. Accept: `python -m pytest -q tests/gateway`. Parallel-safe with M1.
- [x] **M3 Deterministic tools + baseline + pre-filter + log extraction.** Deps: M1. Accept: `python tasks.py baseline` prints scores vs. ground truth. Parallel-safe with M4.
- [x] **M4 SafetyGate, audit log, SyntheticTarget.** Deps: M0 (M1 for synthetic data in tests). Accept: `python -m pytest -q tests/gates` (kill switch, abort ceiling, rate limit, Tier 0 eligibility paths, downgrade-only). Parallel-safe with M3.
- [x] **M5 Investigator agent loop + evidence integrity.** Deps: M2, M3, M4. Accept: `python tasks.py e2e` runs every scenario on fake/replay model. Parallel: none.
- [x] **M6 Tier 3 path + PR reviewer agent.** Deps: M5. Accept: `python -m pytest -q tests/tier3` (diffs produced; reviewer catches seeded bad diffs). Parallel-safe with M7.
- [x] **M7 Verification + re-investigation loop.** Deps: M5. Accept: `python -m pytest -q tests/verify` (wrong first fix recovers or escalates correctly). Parallel-safe with M6.
- [x] **M8a GitHub Actions adapter + playground workflow files.** Deps: M6, M7. Accept: `python -m pytest -q tests/github` against recorded/stubbed HTTP; scenario workflows present under `playground/`. Autonomous.
- [x] **M8b Live playground.** Deps: M8a. Create repo, push scenario branches, `python tasks.py playground-reset`, `python tasks.py e2e-live`. **Human-only.**
- [x] **M9a Eval harness, offline report, README, demo script.** Deps: M5, M6, M7. Accept: `python tasks.py eval` prints the metrics table. Autonomous.
- [x] **M9b Live numbers.** Deps: M9a, recorded fixtures. `python tasks.py record`, `python tasks.py eval-live`. **Human-only.**

Parallel-safe pairs: M1 ‖ M2, M3 ‖ M4, M6 ‖ M7.

Human-only targets (never run autonomously): `record`, `eval-live`, `e2e-live`, `playground-reset`.

## MVP 2

Scope is the "MVP 2" section of `docs/PRD.md`. Same Definition of Done as above, with design notes at `docs/design/Nx.md`. Targets marked *stub* exist in `tasks.py` as "not implemented (Nx)" until their milestone lands. Not yet added to `mvp-check`.

- [x] **Prerequisite (human).** Merge `mvp1/m8b-live-playground` to `main`, tag `v1.0-mvp`, copy the MVP 1 eval tables (`docs/results/report-replay.md`, `docs/results/report-live.md`) into `docs/eval/mvp1.md`. Must precede any MVP 2 change. **Human-only** (merge, tag, push). Done locally 2026-10-11: `main` fast-forwarded to `d1c9444`, tag `v1.0-mvp` there, tables in `docs/eval/mvp1.md` (uncommitted). Push of `main` and the tag is still yours.
- [ ] **N1 Real-log holdout.** Deps: prerequisite. Accept: `python tasks.py holdout-validate` (labels pass the schema; quotas: 30 runs, 4+ per layer L1–L4, 3+ escalations; 10 dev / 20 test) and `python tasks.py eval-holdout` (offline, separate real-data column, counts alongside percentages). Both *stub*. Autonomous: schema, validator, eval column, split enforcement. **Human-only:** `python tasks.py holdout-fetch` (*stub*; fetches logs from GitHub), choosing the 30 runs, writing labels before the agent ever runs on them, and never opening the test split. Data lives in `holdout/`.
- [ ] **N2 `similar_history` retrieval.** Deps: N1 (baseline numbers). Accept: `python -m pytest -q tests/retrieval` (including the leakage test) and `python tasks.py eval-retrieval` (offline on/off comparison, *stub*). Parallel-safe with N3. **Human-only:** recording fixtures for the on/off runs (`python tasks.py record`) and any live comparison (`python tasks.py eval-live`).
- [ ] **N3 `changes_in_window` change correlation.** Deps: N1. Accept: `python -m pytest -q tests/changes` and `python tasks.py eval` reports suspected-change accuracy and the decoy blame rate. Parallel-safe with N2. **Human-only:** recording fixtures and any live eval.
- [ ] **N4 Tier 1/2 with persisted CLI approvals.** Deps: N2, N3 (order from the PRD; the approval code itself does not need them). Accept: `python -m pytest -q tests/approvals` covering: timeout escalates; Tier 2 refuses a live holder; Tier 2 refuses one approver; the same person can't approve twice; self-approval is refused; pending approvals survive a restart; model confidence never qualifies Tier 2; plus `python -m pytest -q tests/gates` still green and gate decisions matching ground truth on every synthetic Tier 1/2 scenario. CLI: `python tasks.py approvals` (*stub*). Autonomous (stubbed execution only).
- [ ] **N5 Static HTML trace report.** Deps: N4. First to cut. Accept: `python tasks.py trace-report` builds offline from a sample audit log (*stub*). Autonomous.

Parallel-safe pairs (MVP 2): N2 ‖ N3.

Human-only (MVP 2): the prerequisite, `holdout-fetch`, labeling, `record` and `eval-live` for MVP 2 runs. The live budget has to be set by the owner first (see the PRD's MVP 2 Budget section).
