# DriftGate MVP 1: run order and prompts

Send these in order. Each step says whether you send a prompt or do something yourself.

## Step 0: Prep (you, no prompt)

1. **Check Claude Code's billing.** Claude Code must be signed in with your Claude subscription, not with an API key. If it bills to your $4.98 API credits, the build itself would use up the budget within minutes. In Claude Code, run `/status` and confirm it shows your subscription account.
2. **Archive and clear the repo:**
   ```
   git add -A
   git commit -m "Snapshot of v0 deterministic-first build"
   git tag v0-deterministic
   git push origin main --tags
   git rm -r .
   git commit -m "Clean slate for agent-driven rebuild (v0 at tag v0-deterministic)"
   ```
3. **Add the two source-of-truth files** and commit them on `main`:
   - `docs/PRD.md`
   - `CLAUDE.md`
   ```
   git add docs/PRD.md CLAUDE.md
   git commit -m "Add PRD and CLAUDE.md for agent-driven rebuild"
   git push
   ```
4. **Set up the API key for DriftGate itself** (used only in Step 4):
   - In the Claude Console, create a separate workspace for DriftGate, set its spend limit to about **$2.50**, and create a key in it.
   - Put it in a gitignored `.env` as `ANTHROPIC_API_KEY=...`. Don't paste it into Claude Code.

## Step 1: Bootstrap M0 (prompt, interactive)

Start a fresh Claude Code session in the repo and send:

```
Read CLAUDE.md and docs/PRD.md in full before doing anything. CLAUDE.md is already written and final. Do not rewrite it; implement what it describes.

Do milestone M0 only. Write no agent, LLM, adapter, gate, or generator logic. Work on a branch named mvp1/m0-skeleton.

Before writing config, use the claude-code-guide agent to confirm the current Claude Code syntax for:
- .claude/settings.json permissions (allow/ask/deny);
- hooks (PreToolUse, PostToolUse, Stop), including how a Stop hook blocks stopping and the field that prevents infinite Stop-hook loops;
- the subagent file format in .claude/agents/.
Use what the docs say, not memory.

Build:

1. pyproject.toml: src layout, package `driftgate`. Runtime deps: anthropic, requests, python-dotenv, jsonschema. Dev deps: pytest, ruff. Python >=3.11.
   .gitignore covering: .env*, .venv, data/, __pycache__, *.sqlite, .autonomous.
   .env.example listing variable names only (ANTHROPIC_API_KEY, GITHUB_TOKEN, DRIFTGATE_PLAYGROUND_REPO, DRIFTGATE_INVESTIGATOR_MODEL, DRIFTGATE_REVIEWER_MODEL).

2. Empty modules for every entry in CLAUDE.md "Module layout", each with a one-line docstring stating its responsibility from the PRD.

3. domain: dataclasses for Execution, Node, ExecutionSummary, LogChunk, FileContent, Remediation, DryRunResult, ExecutionResult, VerificationResult, Report (the full PRD output contract), and the ExecutionSource / RemediationTarget Protocols exactly as in the PRD.

4. contracts/: JSON Schemas for Report and Remediation, matching the PRD contract field for field, enums included. Tests:
   - a valid Report built from the dataclasses serializes and validates;
   - bad enums and missing required fields fail validation.

5. tasks.py: every target in the CLAUDE.md commands table.
   - Targets for later milestones print "not implemented (Mx)" and exit 1.
   - Human-only targets (record, eval-live, e2e-live, playground-reset) refuse to run if the file `.autonomous` exists.
   - `mvp-check` runs each MVP acceptance check listed in TASKS.md, prints a table (check, milestone, pass/fail/pending), and exits 1 if anything isn't passing.

6. llm/fake.py: a scripted fake model with the same call interface the gateway will expose, returning predefined responses including tool calls. Clearly marked as a test double. Include a smoke test.

7. Guardrails:
   - .claude/settings.json:
     - Allow: python tasks.py lint/test/data/baseline/e2e/eval/mvp-check, pytest, ruff, and git add/commit/checkout/switch/branch/worktree/merge/tag/diff/log/status.
     - Ask: python tasks.py record/eval-live/e2e-live/playground-reset.
     - Deny: git push, gh pr merge, reading .env and .env.*, edits to docs/PRD.md, contracts/**, config/kill_switch.json.
   - Hooks, written in Python (this is Windows) under .claude/hooks/:
     - PreToolUse on Bash: exit 2 with a clear message for git push, PR merges, curl/requests to api.github.com or api.anthropic.com, reading .env, and any human-only target while .autonomous exists.
     - PostToolUse on Edit/Write: ruff format + ruff check on changed .py files.
     - Stop: only when .autonomous exists, run `python tasks.py mvp-check`. If it fails and BLOCKERS.md doesn't account for every failing check, block the stop with a message to continue. Honor the loop-prevention field, and allow the stop after 3 consecutive blocks with no new commits.
   - tests/test_hooks.py feeds the PreToolUse hook a list of commands that must be blocked and a list that must be allowed, and tests the Stop hook's decision logic.

8. Architecture tests (pass trivially now, enforce later):
   - Only llm/gateway imports anthropic.
   - Nothing outside eval/ references ground truth.
   - Nothing under agents/ or tools/ imports a RemediationTarget implementation.
   - No test makes a real network call. Patch socket connect to raise in a conftest fixture.

9. Subagents in .claude/agents/ per CLAUDE.md "Subagents". The reviewer gets read-only tools only.

10. .github/workflows/ci.yml: lint + test on push and PR. No secrets.

11. TASKS.md: milestones M0–M9 from the PRD. Each has:
    - its acceptance commands, using `python tasks.py ...`;
    - its dependencies;
    - parallel-safe pairs (M1‖M2, M3‖M4, M6‖M7);
    - which parts are human-only.
    Split M8: M8a (GitHub adapter built and tested against recorded/stubbed HTTP responses, plus the playground scenario workflow files under playground/) and M8b (live: create repo, e2e-live; human-only). Split M9: M9a (eval harness, offline report, README, demo script; autonomous) and M9b (eval-live numbers; human-only).
    Also create PROGRESS.md, BLOCKERS.md (with a "Needs human" section) and IDEAS.md with headers, and a docs/design/ folder.

Then run lint, test, the hook tests, and mvp-check (expected: M0 passes, the rest pending). Show me the output. Commit on mvp1/m0-skeleton, write docs/design/M0.md, and stop for my review.
```

## Step 2: Review M0 (you, about 15 minutes)

Check these before going further. Everything after this depends on them.

- [ ] `tests/test_hooks.py` blocks `git push`, `.env` reads and human-only targets, and allows normal commands.
- [ ] `contracts/` matches the PRD output contract field for field.
- [ ] TASKS.md acceptance commands are real, runnable checks, not prose.
- [ ] `mvp-check` exists and reports M1–M9 as pending.
- [ ] Merge: `git switch main && git merge mvp1/m0-skeleton`.

If something's wrong, tell that session what to fix before moving on.

## Step 3: Autonomous run (prompt, then leave it)

New session in the repo. Use auto-accept edits mode so routine edits don't stall it.

```
Begin an autonomous MVP 1 run per "Autonomous run protocol" in CLAUDE.md.

Setup:
1. Create the .autonomous file.
2. Create branch mvp1/integration from main.
3. Read TASKS.md, PROGRESS.md and BLOCKERS.md.

Scope: every autonomous milestone, M1 through M7, M8a, M9a. Skip human-only work (M8b, M9b, record, eval-live, e2e-live) and list it under "Needs human".

Real API spend in this run is $0. Use llm/fake for all agent behavior; scripted scenarios must cover:
- a correct first diagnosis;
- a wrong first fix that recovers on re-investigation;
- a Tier 3 diff the reviewer rejects;
- a budget-truncated investigation.

Build the replay path in the gateway too, so that fixtures I record later drop in with no code changes.

Work until mvp-check passes for every autonomous milestone, or only blocked or human-only items remain. Then write MVP_REPORT.md, delete .autonomous, and stop. Don't ask me questions. Log them under "Needs human" and keep going.
```

**If the session stops partway** (context limit, crash, you closed it), start a new session and send:

```
Resume the autonomous MVP 1 run. Recreate .autonomous if missing. Read CLAUDE.md, TASKS.md, PROGRESS.md and BLOCKERS.md, check out mvp1/integration, run mvp-check to see where things stand, and continue the protocol from the next unblocked milestone.
```

## Step 4: Real model calls (prompt, interactive, spends about $2)

After reading MVP_REPORT.md. Merge `mvp1/integration` into `main` first, if you're happy with it.

```
Read MVP_REPORT.md, CLAUDE.md "LLM budget", and the gateway code. The .autonomous file must not exist.

We're spending real API money now. Hard limit $2.50 total; my Console workspace limit is also $2.50.

1. Before any call, show me:
   - the gateway's lifetime and daily caps, and confirm they're enforced;
   - the price table it uses for claude-haiku-4-5-20251001;
   - an estimate of cost per investigation, from the token counts the fake-model runs logged.
   Stop and wait for my go-ahead.
2. After I approve:
   - Run `python tasks.py record` on ONE scenario first, and show me the actual cost from usage.
   - Then record the rest of the e2e scenario set with Haiku, stopping if cumulative spend passes $1.25.
3. Run `python tasks.py e2e` on replay to confirm the fixtures work offline.
4. Run `python tasks.py eval-live` with whatever budget remains, keeping $0.25 in reserve. Use the held-out scenarios.
5. Update MVP_REPORT.md with the real numbers: the agent-vs-baseline table, cost per case, total spend.
```

Every command in this step needs your approval, since record and eval-live are "ask" targets. That's intended: you watch the spend as it happens.

## Step 5: Live playground (you, then prompt)

Yourself:
1. Create the public repo `drift-gate-playground` on GitHub.
2. Create a fine-grained PAT scoped to that repo only, with Actions, Contents, Pull requests, and Workflows read/write.
3. Add `GITHUB_TOKEN` and `DRIFTGATE_PLAYGROUND_REPO=<you>/drift-gate-playground` to `.env`.
4. Turn on branch protection for `main` there, requiring 1 review.

Then send:

```
Do M8b. The .autonomous file must not exist.

1. Push the scenario workflows from playground/ to drift-gate-playground:
   - one branch per scenario, per PRD "Playground environments";
   - create the `baseline` tag.
   Show me each git command before running it.
2. Run `python tasks.py playground-reset`, then `python tasks.py e2e-live`.
   - LLM spend comes from what's left of the budget. Stop if the gateway's lifetime cap is reached.
3. Report every run ID and PR URL touched, and whether each scenario hit its expected outcome.
4. Update MVP_REPORT.md and TASKS.md.
```

Pushing to the playground repo is the one push you allow in this whole sequence. The deny rule will block it; approve it per command, or push it yourself.

## Step 6: Wrap up (you)

- Read the reviewer questions in PROGRESS.md and answer them for yourself, module by module. This is your interview prep.
- Merge into `main` and push.
- Update the PRD doc's open questions with what you decided: ship date, $2.50 budget, Haiku as the default model.