# PROGRESS

Append-only run log. One entry per milestone: what was built, decisions made, reviewer questions for the human.

## M0: repo skeleton (2026-10-08)
- Built: packaging, module stubs, domain types, JSON Schema contracts, tasks.py, fake model, guardrails (settings, hooks), architecture tests, subagent definitions, CI, tracker files. See docs/design/M0.md.
- Verified: lint, 112 tests, hook tests, mvp-check (M0 pass, rest pending). Reviewer verdict: pass (run through a general-purpose agent with the reviewer's instructions, since project agents were not loaded in that session).
- Reviewer findings acted on: pre-tool hook hardened against wrappers (`bash -c`, `env`, `cmd /c`, `$(...)`, `(...)`, env-var prefixes, `< .env`, `gh -R`), with tests.
- Reviewer findings not acted on: Stop hook's BLOCKERS.md match is substring-based; Grep tool on `.env` has no permission rule; `generator/` is exempt from the ground-truth import scan because it writes ground truth. All three are accepted limits for now.
- Decision: the human review of M0 (prompts Step 2) is replaced by the reviewer subagent, per the user's instruction to run unattended.
- Reviewer questions for the human: (1) Are heuristic hooks plus permission rules enough for an unattended run, or should it run in a sandbox without push credentials? (2) Should `generator/` write ground truth through a narrow, separately tested path, given invariant 8 says only `eval/` reaches it?

## M2: LLM gateway (2026-10-08)
- Built: gateway with rate limits, concurrency cap, SQLite spend ledger (lifetime $2.50, daily $1.00), retries, prompt caching, per-investigation budget, record/replay fixtures. See docs/design/M2.md.
- Verified: 31 gateway tests, lint, full suite (143), reviewer verdict pass. No existing assertions changed (`Usage` gained two defaulted fields in llm/types.py).
- Carry-forward for M5: the gateway enforces only the token limit; the agent loop must call `consume_tool_call()` and `begin_reinvestigation()` on the budget and must have tests proving it.
- Needs human: confirm real Sonnet 5.5 prices and account rate limits before any live spend (price table and 40 rpm / 40k tpm are placeholders).
- Reviewer questions for the human: (1) Should the gateway hard-enforce the 8-tool-call and 1-re-investigation limits itself? (2) Can you confirm Sonnet 5.5 prices and your tier's rate limits before live spend?

## M4: SafetyGate, audit log, SyntheticTarget (2026-10-08)
- Built: downgrade-only SafetyGate (kill switch re-read every decision, abort ceiling, rate limit, Tier 0/2 eligibility), append-only audit log, SyntheticTarget. See docs/design/M4.md.
- Verified: 41 gate tests, lint, full suite (184). Reviewer: round 1 fail (gate could upgrade a refused proposal; property test too weak; target ran non-allowed actions; missing set facts skipped checks), round 2 pass after fixes.
- Test edits with justification: three existing fixtures in tests/gates gained `proposed_set_size=1, resource_set_total=5` because missing set facts now fail closed for tiers 0-2; no assertion loosened (details in docs/design/M4.md).
- Carry-forward: callers (M3/M5) must compute `shared_resources`, set sizes and blast radius; `BlastRadius` default 0 is fail-open if unpopulated, so M5 must always populate it; one RateLimiter per process.
- Reviewer questions for the human: (1) Is "PR plus reviewer" enough of a gate for Tier 3 when set-size facts are missing, or should every tier require them? (2) Should unknown blast radius be representable and fail closed, and should "only SafetyGate produces allowed" be structural (gate-issued token) rather than a settable field?
- Needs human: choose N for "blast radius over N" (currently 10).
