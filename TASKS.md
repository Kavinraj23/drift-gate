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
- [ ] **M1 Synthetic generator + synthetic repos + ground truth.** Deps: M0. Accept: `python tasks.py data` (seeded dataset, schema validation passes). Parallel-safe with M2.
- [ ] **M2 LLM gateway** (rate limits, budgets, daily and lifetime caps, retries, caching, record/replay). Deps: M0. Accept: `python -m pytest -q tests/gateway`. Parallel-safe with M1.
- [ ] **M3 Deterministic tools + baseline + pre-filter + log extraction.** Deps: M1. Accept: `python tasks.py baseline` prints scores vs. ground truth. Parallel-safe with M4.
- [ ] **M4 SafetyGate, audit log, SyntheticTarget.** Deps: M0 (M1 for synthetic data in tests). Accept: `python -m pytest -q tests/gates` (kill switch, abort ceiling, rate limit, Tier 0 eligibility paths, downgrade-only). Parallel-safe with M3.
- [ ] **M5 Investigator agent loop + evidence integrity.** Deps: M2, M3, M4. Accept: `python tasks.py e2e` runs every scenario on fake/replay model. Parallel: none.
- [ ] **M6 Tier 3 path + PR reviewer agent.** Deps: M5. Accept: `python -m pytest -q tests/tier3` (diffs produced; reviewer catches seeded bad diffs). Parallel-safe with M7.
- [ ] **M7 Verification + re-investigation loop.** Deps: M5. Accept: `python -m pytest -q tests/verify` (wrong first fix recovers or escalates correctly). Parallel-safe with M6.
- [ ] **M8a GitHub Actions adapter + playground workflow files.** Deps: M6, M7. Accept: `python -m pytest -q tests/github` against recorded/stubbed HTTP; scenario workflows present under `playground/`. Autonomous.
- [ ] **M8b Live playground.** Deps: M8a. Create repo, push scenario branches, `python tasks.py playground-reset`, `python tasks.py e2e-live`. **Human-only.**
- [ ] **M9a Eval harness, offline report, README, demo script.** Deps: M5, M6, M7. Accept: `python tasks.py eval` prints the metrics table. Autonomous.
- [ ] **M9b Live numbers.** Deps: M9a, recorded fixtures. `python tasks.py record`, `python tasks.py eval-live`. **Human-only.**

Parallel-safe pairs: M1 ‖ M2, M3 ‖ M4, M6 ‖ M7.

Human-only targets (never run autonomously): `record`, `eval-live`, `e2e-live`, `playground-reset`.
