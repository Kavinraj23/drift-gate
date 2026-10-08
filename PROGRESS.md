# PROGRESS

Append-only run log. One entry per milestone: what was built, decisions made, reviewer questions for the human.

## M0: repo skeleton (2026-10-08)
- Built: packaging, module stubs, domain types, JSON Schema contracts, tasks.py, fake model, guardrails (settings, hooks), architecture tests, subagent definitions, CI, tracker files. See docs/design/M0.md.
- Verified: lint, 112 tests, hook tests, mvp-check (M0 pass, rest pending). Reviewer verdict: pass (run through a general-purpose agent with the reviewer's instructions, since project agents were not loaded in that session).
- Reviewer findings acted on: pre-tool hook hardened against wrappers (`bash -c`, `env`, `cmd /c`, `$(...)`, `(...)`, env-var prefixes, `< .env`, `gh -R`), with tests.
- Reviewer findings not acted on: Stop hook's BLOCKERS.md match is substring-based; Grep tool on `.env` has no permission rule; `generator/` is exempt from the ground-truth import scan because it writes ground truth. All three are accepted limits for now.
- Decision: the human review of M0 (prompts Step 2) is replaced by the reviewer subagent, per the user's instruction to run unattended.
- Reviewer questions for the human: (1) Are heuristic hooks plus permission rules enough for an unattended run, or should it run in a sandbox without push credentials? (2) Should `generator/` write ground truth through a narrow, separately tested path, given invariant 8 says only `eval/` reaches it?
