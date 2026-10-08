# DriftGate

An agent that investigates failed CI pipeline executions like an on-call platform engineer: it gathers evidence with read-only tools, forms a hypothesis, proposes a bounded fix, acts only through deterministic safety gates, verifies the result, and re-investigates once if the fix didn't work.

**Design principle: the agent decides, deterministic code verifies.**

## Authority

- `docs/PRD.md` is the source of truth. It wins on any conflict, including with this file.
- `TASKS.md` is the tracker. `PROGRESS.md` is the append-only run log. `BLOCKERS.md` holds anything that stopped work. `IDEAS.md` holds out-of-scope ideas.
- Never edit `docs/PRD.md`. If it seems wrong or ambiguous, log it in BLOCKERS.md and continue with other work.
- The previous implementation is archived at git tag `v0-deterministic`. Do not port code from it.

## Environment and commands

- Windows, Python 3.11+, venv at `.venv`, package `driftgate` in a `src/` layout.
- Task runner: `python tasks.py <target>`. (The PRD says `make <target>`; they are the same targets.)

| Target | What it does | Who runs it |
| --- | --- | --- |
| `install` | `pip install -e .[dev]` | anyone |
| `lint` | `ruff format --check` + `ruff check` | anyone |
| `test` | full pytest suite, offline | anyone |
| `data` | generate the seeded synthetic dataset and repos | anyone |
| `baseline` | run the deterministic baseline, print scores | anyone |
| `e2e` | every synthetic scenario end to end, fake or replay model | anyone |
| `eval` | agent vs. baseline metrics table, offline | anyone |
| `mvp-check` | every MVP acceptance check, prints a pass/fail table | anyone |
| `record` | run scenarios against the real API and save replay fixtures | **human only** |
| `eval-live` | eval against the real API | **human only** |
| `e2e-live` | live scenarios in drift-gate-playground | **human only** |
| `playground-reset` | reset the playground repo to its baseline tag | **human only** |

## Module layout

`domain` (types, output contract) · `prefilter` · `tools/` (one module per agent tool, each a thin wrapper over a deterministic function) · `agents/investigator` · `agents/reviewer` · `llm/gateway` · `llm/fake` (scripted test double) · `gates` (SafetyGate, kill switch, abort ceiling, rate limit) · `audit` · `verify` · `adapters/synthetic` · `adapters/github_actions` · `baseline` · `eval/` · `generator/`. Schemas live in `contracts/`.

## Invariants

These are never weakened, bypassed, or "temporarily" disabled. Each has a test.

1. **Agents are read-only.** No agent tool has side effects. Only actions SafetyGate allows reach a `RemediationTarget`. Nothing under `agents/` or `tools/` imports a `RemediationTarget` implementation.
2. **SafetyGate can only downgrade.** It may turn a proposal into an escalation; it never raises a tier.
3. **Deterministic eligibility.** Tier 0 needs a deterministic signature match AND (flake precedent OR a Tier 0 known-transient rule). Tier 2 never qualifies on model confidence. Agent confidence is recorded, never sufficient on its own.
4. **Agents never merge.** Tier 3 opens a PR and stops. Branches the agent creates are prefixed `driftgate/`.
5. **Kill switch and abort ceiling** are checked on every gate decision. Global and per-tier kill switch; fleet-wide blast radius escalates.
6. **Rate limit:** at most 2 remediation attempts per fingerprint per hour, then hard escalate.
7. **Evidence integrity:** every evidence item's `source` must be a tool call id from that run. Uncited claims are dropped in code, not by prompting.
8. **Ground truth is reachable only by `eval/`.** Nothing else imports or opens it.
9. **Error strings are never synthesized.** CI/tool error text in data, fixtures and tests is copied verbatim from real sources and stored with its source noted. (Scripted fake-model responses are test doubles, not error text, and are allowed.)
10. **Only `llm/gateway` imports the Anthropic SDK.**
11. **Credentials only in the gitignored `.env`.** Never read, print, log, or commit them.
12. **Live GitHub work targets `drift-gate-playground` only.** The adapter refuses any other repo.
13. **Additive before subtractive** in any multi-step remediation.

## LLM budget

The project's total real-API budget is **$2.50**. The gateway enforces it; agents never work around it.

- **Models** (configurable via `.env`): investigator and reviewer default to `claude-haiku-4-5-20251001`. Use `claude-sonnet-5-5` only for a handful of demo cases, and only if the budget has room.
- **Lifetime spend cap** of $2.50 and **daily cap** of $1.00, persisted in SQLite under `data/`. They are computed from each response's `usage`, using a price table in config. When a cap is hit, calls fail fast and cases escalate.
- **Per-investigation budget:** max 8 tool calls, max 40k total tokens, max 1 re-investigation round.
- **Prompt caching** on the system prompt and tool definitions.
- Development, tests, `e2e`, `eval` and `mvp-check` use the fake model or replay fixtures and **must never need the API key**. A test fails if any of them attempt a network call.

## Engineering conventions

- Type hints everywhere; dataclasses for domain types; no global mutable state. Inject clocks, randomness and I/O so tests are deterministic.
- Tests are offline and fast. Use a fake clock for anything time-based; never `sleep` in tests.
- Small modules with one responsibility each, matching the layout above.
- No new dependencies beyond `anthropic`, `requests`, `python-dotenv`, `jsonschema` (runtime) and `pytest`, `ruff` (dev) without logging the reason in PROGRESS.md.

## Definition of done (any milestone)

1. Its acceptance command(s) in TASKS.md pass.
2. `python tasks.py lint` and `python tasks.py test` pass.
3. The reviewer subagent returns pass.
4. `DESIGN_NOTE.md` for the milestone is written (`docs/design/Mx.md`): what was built, why, what was tested, open doubts.
5. PROGRESS.md has an entry, and the TASKS.md box is ticked.

## Subagents

Defined in `.claude/agents/`. Every subagent reads this file and the relevant PRD sections first, and stops rather than changing a contract or invariant.

- **core:** domain, prefilter, gates, audit, verify, baseline
- **agents:** agents/, tools/, llm/
- **adapters:** adapters/synthetic, adapters/github_actions
- **eval:** generator/, eval/, scenarios, fixtures
- **reviewer:** read-only. Checks a diff against the invariants, contracts and PRD, with particular attention to **weakened tests**. Returns pass/fail with file:line findings, plus two questions for the human about the change. Never fixes anything.

## Autonomous run protocol

Applies when the file `.autonomous` exists at the repo root. The orchestrator creates it at the start of an autonomous run and deletes it at the end.

### Orchestrator role
- Plan, delegate, merge, and run checks. Do not write feature code yourself.
- At the start of every milestone, re-read TASKS.md, PROGRESS.md, BLOCKERS.md and the relevant PRD sections. Trust the files over your memory of the conversation.
- One milestone goes to one subagent with a fresh context, on its own branch (`mvp1/mN-short-name`). Run milestones in parallel only where TASKS.md marks them parallel-safe, using git worktrees.

### Loop
1. Pick the next unblocked milestone from TASKS.md.
2. Delegate it with: the milestone, its acceptance commands, the relevant PRD sections, and the invariants.
3. When the subagent reports done:
   - run its acceptance commands, `lint`, `test`, and `mvp-check`;
   - send the diff to the reviewer.
4. On reviewer fail: give the findings to a fresh implementing subagent. Max 3 rounds, then log a blocker.
5. On pass: merge into `mvp1/integration`, tag `mvp1-mN`, append to PROGRESS.md (what was built, decisions made, the reviewer's questions for the human), tick TASKS.md.
6. Repeat until `mvp-check` passes or every remaining item is blocked or human-only.

### Hard rules
- Never weaken a test, acceptance check, gate, budget or contract to get a pass. Changing an existing test assertion requires a justification line in PROGRESS.md, and the reviewer must flag unjustified changes.
- Never call real network APIs (Anthropic, GitHub). Use `llm/fake` and recorded or stubbed HTTP responses. Anything needing the real thing goes in BLOCKERS.md under **Needs human**.
- Never run human-only targets.
- No scope beyond the PRD's MVP list. Ideas go in IDEAS.md.
- Never push, never merge to `main`, never delete branches you didn't create.
- Same failure 3 times: stop that milestone, log what you tried in BLOCKERS.md, move to independent work.
- Don't ask the human questions during the run. Log them in BLOCKERS.md under **Needs human** and continue.

### Finishing
When `mvp-check` passes, or only blocked or human-only items remain:
1. Write `MVP_REPORT.md`:
   - what works and how it was verified
   - the `mvp-check` table
   - the PRD's never-cut list, with each item marked done, partial or blocked
   - the **Needs human** list, in order
   - the reviewer questions to answer before trusting each module
2. Delete `.autonomous`.
3. Stop.