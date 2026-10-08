# DriftGate PRD — Agent-Driven CI Remediation

Oct 7, 2026

## Summary

DriftGate is an agent that investigates failed CI pipeline executions the way an on-call platform engineer would. It gathers evidence with tools, forms a hypothesis, proposes a bounded fix, acts through deterministic safety gates, verifies the result, and re-investigates if the fix didn't work. The deliverable is a fix or a fix proposal (a real re-run or a real pull request), not a routing decision.

**Design principle: the agent decides, deterministic code verifies.** The agent drives every investigation and chooses what to look at. Deterministic code owns the facts the agent's actions depend on: signature matches, flake precedent, blast radius, lock-holder death, and every gate.

This is a clean-slate rebuild that supersedes the September PRD. That version was a deterministic pipeline with an optional LLM on the residual, and the agent was explicitly not the deliverable. This version inverts that.

|  | September PRD | This PRD |
| --- | --- | --- |
| Who drives investigation | Fixed 8-step pipeline; LLM on residual only | Agent runs every investigation |
| Deterministic steps | Pipeline stages | Tools the agent calls, and the gates |
| Classifier's role | Foundation of the product | Eval baseline + an agent tool |
| Verification | Observe, then escalate | Re-investigate with the failed attempt as evidence |
| Multi-agent | None | Investigator + independent PR reviewer |
| LLM spend | Not addressed | Budgeted and rate-limited as a safety constraint |
| Never-cut | Excludes the agent | Agent loop, verification loop, agent-vs-baseline eval |

Carried over unchanged in spirit: the incident-derived safety spine, the tier model, the failure taxonomy, the provider protocols, the tool-cited output contract, log handling, the eval design, and the honest framing.

## Background

Built by a student after a SWE internship on a developer platform team. The main internship project was a governed self-service platform for OpenTofu/Terraform state operations, with human approval gates and an immutable audit log.

Three internship incidents form the safety spine:

- **Additive before subtractive.** A cross-workspace move removed a resource from the source state before importing it to the destination. A partial import failure orphaned a live production resource. The fix was import-first, so the worst case became a harmless duplicate.
- **Abort ceilings.** Three variable-wipe incidents during a migration traced to a shell counting bug and a missing-map contract. They were closed out with a hard abort on empty proposed sets and a ceiling that refuses to delete 100% of a resource set.
- **Symptoms that don't match layers.** A stalled UI traced to firewalled runner webhook callbacks. The symptom appeared several layers from the cause and never produced a failed status.

The third lesson is also the strongest argument for an agent: a fixed pipeline checks the layers it was built to check, while an investigator can follow evidence across layers.

## What it is

The audience is the platform on-call engineer. For each failed execution, the agent produces an evidence-backed report and either a remediation it executed (Tier 0), a remediation awaiting approval (Tier 1–2), a pull request (Tier 3), or a reasoned escalation.

The loop:

1. **Pre-filter (deterministic).** Governance outcomes (L5) and user aborts close immediately with no model call. Everything else goes to the agent.
2. **Investigate (agent).** The agent calls tools to gather evidence, cheapest first by guidance rather than by hard-coded order.
3. **Hypothesize and propose (agent).** The agent emits a report with cited evidence, a hypothesis, and a proposed remediation with its tier.
4. **Gate (deterministic).** SafetyGate independently checks the facts the proposal depends on. It can downgrade to escalation; it can never upgrade a tier.
5. **Review (agent, Tier 3 only).** An independent reviewer agent critiques the diff before the PR opens.
6. **Execute.** Through the RemediationTarget, after dry-run.
7. **Verify and re-investigate.** If the next execution still fails, the failed attempt goes back to the agent as new evidence, within a retry budget. Exhausted budget means escalate with everything attached.
8. **Audit.** Every proposal, gate decision, execution, and verification is logged.

The investigator never acts directly; SafetyGate can only downgrade, and an unverified fix returns to the investigator once before escalating.

The agent never mutates anything directly. Its tools are read-only; all side effects pass through SafetyGate and RemediationTarget.

## Agent design

Two agents, each with a distinct job. Both use a tool-use loop written directly against the Anthropic SDK, with no agent framework, routed through the LLM gateway (see Safety).

### Investigator agent

Owns investigation, hypothesis, and remediation proposal. All tools are read-only and return structured results with a stable `source` id that evidence must cite.

| Tool | Returns | Cost | MVP |
| --- | --- | --- | --- |
| `get_execution` | Execution tree, failed leaf nodes, refs (connector, template version, runner pool) | Cheap | Yes |
| `classify_signature` | Deterministic signature match, layer, and whether a Tier 0 rule exists | Cheap | Yes |
| `fleet_correlate` | Other executions failing on the same shared dimension in the last 30 min | Cheap | Yes |
| `flake_history` | Fail-then-pass history for this fingerprint | Cheap | Yes |
| `get_step_logs` | Extracted, redacted, budgeted error blocks from the failed step only | Medium | Yes |
| `read_repo_file` | A file at a ref (workflow YAML, lockfile, config), size-capped | Medium | Yes |
| `changes_in_window` | Commits and events before first failure, including decoys | Medium | Stretch |
| `similar_history` | Past reports for the same fingerprint family | Medium | Stretch |
| `get_remediation_attempt` | A prior attempt and its verification result (re-investigation only) | Cheap | Yes |

**Termination.** The loop ends when the agent emits a final report, or when it hits a budget limit: max tool calls per investigation, max tokens, or wall-clock. On budget exhaustion it abstains and escalates with the partial evidence bundle, marked `budget_truncated`.

**Tier proposal.** The agent proposes a tier and action from the catalog. SafetyGate re-derives eligibility from deterministic facts and may downgrade to escalation. The agent's confidence is recorded but never sufficient on its own for Tier 0 or Tier 2.

**Evidence integrity.** Every evidence item must cite the `source` id of a tool result from that run. Uncited claims are dropped in code before the report is emitted, not by prompting.

### PR reviewer agent

Runs only for Tier 3. It receives the diff, the target files, and the hypothesis, but not the investigator's reasoning trace, so it judges the change on its merits. It can call `read_repo_file` only.

Verdicts: approve (PR opens), revise (feedback goes back to the investigator, max 1 revision round), or reject (escalate). The reviewer's verdict and comments go into the PR description alongside the evidence bundle.

## Remediation catalog

Tiered by blast radius, with a different gate per tier. Tier 0 and Tier 3 execute against real GitHub Actions; Tier 1 and 2 are gated, dry-run, and audited with stubbed execution.

| Tier | Examples | Gate | Real or stubbed | MVP |
| --- | --- | --- | --- | --- |
| 0: idempotent | Re-run workflow, re-run failed job, clear stale cache | Auto. Deterministic signature match AND (flake precedent OR a Tier 0 known-transient rule). Max 2 per fingerprint per hour | Real | Yes |
| 1: bounded platform action | Recycle runner, refresh connector token, reschedule pod, bump pool capacity | Single approval, 15-min timeout, dry-run shown first | Stubbed | No |
| 2: state-touching | Force-unlock a state lock whose holder is provably dead | Two-person approval, mandatory dry-run with holder-death proof, hard refusal if holder is running. Model confidence never qualifies | Stubbed (simulated lock table) | No |
| 3: code change | Regenerate lockfile, pin a broken provider, revert a template bump, fix an undefined variable, fix a secret scope | Reviewer agent approves, then the PR is the gate. Agent never merges | Real | Yes |

**Tier 0 eligibility paths.** The precedent path requires fail-then-pass history for the fingerprint. The known-transient path requires a signature rule authored as Tier 0 (e.g. throttling), so a first occurrence isn't permanently ineligible. A second failure after a Tier 0 retry hard-escalates.

**Tier 3 is the centerpiece.** It's where the agent does the most reasoning (reading files, writing a diff, explaining it) and it produces an artifact a human can judge. It is safe by construction.

## Safety constraints

### Remediation safety

- **Abort ceiling.** Blast radius over N executions, or a remediation touching more than one shared resource, stops and escalates. Fleet-wide problems are not for an agent to fix.
- **Additive before subtractive.** A partial failure leaves a harmless duplicate, never a silent orphan.
- **Deterministic evidence for Tier 0 and Tier 2+.** Eligibility gates on verifiable facts, never on model confidence alone.
- **Per-fingerprint rate limit.** Two remediation attempts per fingerprint per hour, then hard escalate.
- **Kill switch.** Global and per-tier, read on every gate decision.
- **Read-only agents.** No agent tool has side effects; only SafetyGate-approved actions reach RemediationTarget.
- **Full audit trail** on every proposal, including rejected, expired, failed, and budget-truncated ones.

### LLM budget and rate limiting

Every Anthropic API call goes through a single gateway module. No other module imports the SDK, and a test enforces that.

- **Rate limits:** token buckets for requests/min and tokens/min, set below the account's tier limits.
- **Concurrency cap** on in-flight requests.
- **Per-investigation budget:** max tool calls, max tokens, max re-investigation rounds. Exhaustion means abstain and escalate.
- **Daily spend cap,** persisted (SQLite) from each response's `usage`, surviving restarts. When hit, the agent stops and failures escalate with only the deterministic pre-filter result.
- **Retries** on 429/529 with exponential backoff and jitter, honoring `retry-after`.
- **Prompt caching** on the static system prompt and tool definitions.
- **Key isolation:** DriftGate's key lives in its own Console workspace with a monthly spend limit, in a gitignored `.env`. Development and tests run on record/replay fixtures and never need the key.
- **Per-call logging** of tokens, estimated cost, latency, and limiter wait, rolled up per investigation for the eval.

## Verification and re-investigation

Executing a remediation doesn't end the loop. After acting, the system observes the next execution of the same pipeline and checks whether the fingerprint stopped recurring.

- **Verified:** the remediation is marked successful and the case closes.
- **Not verified:** the remediation is marked failed and rolled back if reversible. The failed attempt, including its verification result, is handed back to the investigator as new evidence via `get_remediation_attempt`. The agent must explain why the first hypothesis was wrong before proposing again.
- **Retry budget:** at most 1 re-investigation round in the MVP. A second failure, or any failure after a Tier 0 retry on the same fingerprint, escalates with the original evidence, both attempts, and the agent's revised reasoning.
- **Tier 3:** verification is a CI run on the PR branch. A red run on the PR counts as an unverified remediation and triggers re-investigation; a green run marks the PR "verified by CI" in its description.

This produces the two headline metrics: remediation success rate, and recovery rate after a failed first attempt.

## Failure taxonomy

Layer maps onto ownership and onto which remediations are eligible. The agent assigns a layer in its report; `classify_signature` gives it a deterministic starting point it can confirm or overturn with evidence.

| Layer | Meaning | Examples | MVP coverage |
| --- | --- | --- | --- |
| L0 | Never started | No eligible runner, infra provisioning failed, trigger filtered, required input missing | Escalate only |
| L1 | Step infrastructure | Image pull backoff, OOMKilled / exit 137, unschedulable or evicted pod, step timeout | Tier 0 where transient |
| L2 | Identity and secrets | Expired credential, secret at wrong scope, AssumeRole denied, registry 401 vs 429 | Tier 3 for secret scope; Tier 0 for 429 |
| L3 | Pipeline definition | Invalid YAML, template not found, template bump breaking callers, expression resolving null, policy denial | Tier 3 |
| L4 | The actual work | tofu init/plan/apply errors, provider drift, lockfile mismatch, state lock held, throttling/quota, IAM denied | Tier 3 for lockfile/provider; Tier 0 for throttling |
| L5 | Governance | Approval rejected or expired: normal terminal states, never remediated | Pre-filter closes |
| L6 | Post-execution | Stalled executions past p99 with no step transitions; status monitoring is blind to these | Stretch: needs its own detector |

## Architecture and output contract

Python only. The agents never talk to a CI provider directly; they talk to a normalized domain model behind two protocols.

```python
class ExecutionSource(Protocol):
    def get_execution(self, execution_id) -> Execution: ...
    def get_failed_leaf_nodes(self, execution_id) -> list[Node]: ...
    def get_step_logs(self, execution_id, node_id, budget: int) -> LogChunk: ...
    def list_executions(self, window, filter) -> list[ExecutionSummary]: ...
    def read_file(self, repo, path, ref, max_bytes: int) -> FileContent: ...

class RemediationTarget(Protocol):
    def dry_run(self, action: Remediation) -> DryRunResult: ...
    def execute(self, action: Remediation) -> ExecutionResult: ...
    def verify(self, action: Remediation) -> VerificationResult: ...
```

Implementations: `GitHubActionsSource`/`Target` (real) and `SyntheticSource`/`Target` (simulated). The node graph is a tree; useful information lives in the deepest failed leaf. Executions carry refs for connector, template version, runner pool, and infra definition, which are the join keys for fleet correlation.

**Modules:** `domain` (types, contract) · `prefilter` · `tools/` (one module per agent tool, each a thin wrapper over a deterministic function) · `agents/investigator`, `agents/reviewer` · `llm/gateway` · `gates` (SafetyGate, kill switch, abort ceiling, rate limit) · `audit` · `verify` · `adapters/github_actions`, `adapters/synthetic` · `baseline` (deterministic classifier used for eval) · `eval/`.

### Output contract

The September contract, plus fields for the agent run:

```json
{
  "execution_id": "str",
  "fingerprint": "str",
  "duplicate_of": "str | None",
  "classification": "platform|user|transient|governance|unknown",
  "layer": "L0..L6",
  "confidence": 0.0,
  "blast_radius": {"executions_affected": 0, "shared_dimension": "str"},
  "hypothesis": "str",
  "evidence": [{"source": "tool_call_id", "finding": "str", "supports": "str"}],
  "suspected_change": {"kind": "str", "ref": "str", "at": "str", "basis": "str"},
  "remediation": {
    "tier": 0,
    "action": "str",
    "rationale": "str",
    "reversible": true,
    "gate": "auto|single_approval|dual_approval|pull_request",
    "gate_decision": "allowed|downgraded|refused",
    "dry_run": {}
  },
  "review": {"verdict": "approve|revise|reject", "comments": "str"},
  "prior_attempts": [],
  "abstained": false,
  "budget_truncated": false,
  "escalation_reason": "str",
  "run": {"tool_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "latency_s": 0.0}
}
```

Every evidence `source` must match a tool call id from that run, enforced in code.

## Real vs. simulated

Built solo: no Harness account, no corporate cloud, no Kubernetes cluster, no production CI traffic.

**Real, at zero cost.** GitHub Actions on a public repo: real executions, a real job/step graph, real logs. Tier 0 re-runs and Tier 3 branches and PRs execute against it for real. A canary workflow fails on attempt 1 and passes on re-run. Public failed workflow logs from other repos form a hand-labeled holdout.

**Necessarily simulated.** A fleet (fleet correlation needs many concurrent pipelines), history (flake detection needs weeks of executions), Harness-specific structure, and Tier 1/2 actions.

### Synthetic population

A generator produces about 30 days of backdated executions across about 12 pipelines:

- Base rates of ~80% success; failures ~60% user, ~25% platform, ~15% transient.
- 3–4 correlated bursts sharing a connector or template version inside a 20-minute window.
- 2 genuinely flaky pipelines that fail-then-pass on retry.
- A change timeline of backdated git commits and an events table, including decoy changes with no causal link.
- **New for the agent:** a synthetic repo per pipeline with real files (workflow YAML, lockfiles, configs) containing the injected fault, so `read_repo_file` and Tier 3 diffs have something real to reason about.
- Every failure carries its ground-truth label and its correct remediation. Ground truth is reachable only by the eval harness, never by agent tools.

**Error text is never synthesized.** Exact real strings only (e.g. Terraform's `Error acquiring the state lock`, `OOMKilled`/exit 137, `ImagePullBackOff`, `ThrottlingException`), with Terraform's multi-line `Error:` block shape preserved.

### Log handling

The `get_step_logs` tool fetches only the failed step and enforces a character budget at the tool boundary. It also strips ANSI codes, collapses repeated spinner/retry lines, and redacts credential patterns before anything reaches a model. It returns all error blocks ranked, not just the first, since cascading errors make the first one frequently not the operative one.

## Playground environments

MVP v1 uses two environments and needs no cloud account: a synthetic sandbox for all autonomous development and eval, and a dedicated public GitHub repo for the live proof.

### Synthetic sandbox (local, offline, free)

All Claude Code autonomous runs, `make e2e`, and `make eval` run here. Nothing in it can mutate anything real.

- `SyntheticSource` / `SyntheticTarget` in place of a CI provider, fed by the seeded generator.
- Synthetic repos per pipeline with real workflow YAML, lockfiles, and configs containing the injected fault.
- Record/replay LLM fixtures, so runs are deterministic and never need the API key.
- Ground truth readable only by the eval harness.

### Live playground: `drift-gate-playground`

A separate public repo, distinct from the project repo, where real Tier 0 re-runs and Tier 3 PRs land. Agent PRs and deliberately broken workflows stay out of the repo interviewers read, and Actions minutes are free on public repos.

**MVP v1 scenarios.** Each lives on its own branch, is triggered by `workflow_dispatch`, and uses only GitHub-hosted runners.

| Scenario | Branch | Injected fault | Expected outcome |
| --- | --- | --- | --- |
| Flaky canary | `scenario/flaky` | Fails on attempt 1, passes on re-run | Tier 0 re-run via flake precedent, verified green |
| Throttling | `scenario/throttle` | First attempt fails with a throttling error line copied verbatim from a real log | Tier 0 re-run via known-transient rule, verified green |
| Lockfile mismatch | `scenario/lockfile` | `tofu init` with a lockfile that doesn't match the pinned provider (null provider, no cloud) | Tier 3 PR regenerating the lockfile, PR CI green |
| Broken provider pin | `scenario/provider-pin` | Version constraint resolves to an incompatible provider version | Tier 3 PR pinning a working version, PR CI green |
| Undefined variable | `scenario/undefined-var` | Workflow expression references an undefined input | Tier 3 PR fixing the expression, PR CI green |
| Governance | `scenario/approval-rejected` | Job gated on an environment approval that is rejected | Pre-filter closes it, no model call |

**Reset and repeatability.** A tagged `baseline` holds every scenario's broken state. `make playground-reset` force-resets each scenario branch to the tag, closes open agent PRs, and deletes agent branches. It mutates the playground, so it is human-run only.

**Access and guardrails.**

- Fine-grained PAT scoped to `drift-gate-playground` only: Actions, Contents, and Pull requests read/write, plus Workflows write (Tier 3 fixes edit workflow files). Stored only in the gitignored `.env`.
- The GitHub adapter refuses any repo other than the configured playground.
- Agent branches are prefixed `driftgate/`; `main` has branch protection requiring a review, so nothing can merge without a human even if the agent misbehaves.
- No repository secrets in the playground, so a leaked log can't expose anything.
- The 2-per-hour fingerprint limit applies to live re-runs exactly as in the sandbox.

**`make e2e-live`** (human-approved each time): reset, dispatch each scenario, wait for the failure, run DriftGate on it, then assert the expected outcome and print every run ID and PR URL it touched.

**Deferred past v1:** LocalStack or AWS (only needed for a real state-lock error on the Tier 2 path), secret-scope scenarios (need repository secrets), and Tier 1/2 actions.

## Evaluation

The headline result is agent vs. deterministic baseline on the same labeled scenarios. The baseline is the cheapest-first pipeline from the September design (status gate → signature → fleet correlation → flake check), run with no model calls.

| Metric | What it answers | Weight |
| --- | --- | --- |
| False remediation rate | Acted when it should not have | Heaviest; a wrong action is far worse than an abstention |
| Remediation success rate | Did the action resolve the failure | Headline |
| Recovery rate | After a failed first attempt, did re-investigation fix it | Headline for the agentic loop |
| Escalation precision | When it declined to act, was declining correct | High |
| Tier 3 PR quality | Does the diff match the injected fault's correct fix; did the reviewer catch bad diffs | High |
| Classification accuracy | Layer and classification vs. ground truth | Diagnostic |
| Cost and latency per case | Tokens, USD, seconds, tool calls per investigation | Reported for every run |
| Tool efficiency | Share of cases resolved with only cheap tools | Diagnostic |

**Held-out sets.** 8–10 synthetic scenarios are never examined during development. A separate holdout of real scraped GitHub Actions failures tests whether the agent generalizes beyond the generator's templates.

**Determinism.** Development and CI evals run on record/replay LLM fixtures recorded from real API runs. A small live eval (`eval-live`) runs against the API with a fixed spend cap and produces the reported numbers.

## MVP scope and build plan

Ship an MVP as fast as possible using Claude Code for an autonomous, multi-agent build, then iterate. Scope is the variable; the never-cut list is not.

**Never cut (the MVP):** pre-filter, investigator agent with the MVP tools, SafetyGate with kill switch, abort ceiling and rate limit, LLM gateway with budgets, real Tier 0 and Tier 3 on GitHub Actions, the PR reviewer agent, the verification and re-investigation loop, and the agent-vs-baseline eval on synthetic data.

**Cut order after the MVP (first cut first):** historical similarity → Tier 1/2 stubbed actions → change correlation → L6 stall detector → real-log holdout.

### Milestones

Each milestone is one Claude Code work unit on its own branch. Done means its acceptance command passes, the full test suite passes, and the reviewer subagent signs off.

| # | Milestone | Done when | Can run alongside |
| --- | --- | --- | --- |
| M0 | Repo skeleton: CLAUDE.md, Makefile, guardrails (settings, hooks), CI, `domain` types, JSON Schema contracts | `make test` green; hook test script passes | None |
| M1 | Synthetic generator + synthetic repos + ground truth | `make data` produces a seeded dataset; schema validation passes | M2 |
| M2 | LLM gateway: rate limits, budgets, daily cap, retries, caching, record/replay | Gateway tests pass with a fake clock; no SDK imports outside the gateway | M1 |
| M3 | Deterministic tools + baseline + pre-filter + log extraction | `make baseline` prints scores vs. ground truth | M4 |
| M4 | SafetyGate, audit log, SyntheticTarget | Gate tests: kill switch, abort ceiling, rate limit, Tier 0 eligibility paths, downgrade-only | M3 |
| M5 | Investigator agent loop + evidence integrity | `make e2e` runs every scenario end to end on replay fixtures | None |
| M6 | Tier 3 path + PR reviewer agent | E2E Tier 3 scenarios produce diffs; reviewer catches seeded bad diffs | M7 |
| M7 | Verification + re-investigation loop | E2E scenarios with a wrong first fix recover or escalate correctly | M6 |
| M8 | GitHub Actions adapter (real Tier 0 + Tier 3), drift-gate-playground repo with the 6 scenario branches, baseline tag, and reset script | `make e2e-live` (human-approved) passes all 6 playground scenarios: Tier 0 re-runs verified green, Tier 3 PRs open unmerged with green CI, governance closed without a model call | None |
| M9 | Eval harness + report + README | `make eval` prints the metrics table; `eval-live` numbers recorded | None |

**Human checkpoints:** you approve M0's CLAUDE.md and contracts before anything parallel starts; you record the first replay fixtures (needs the API key); you run `e2e-live` and `eval-live` yourself; you read each milestone's design note before merging.

**Learning safeguard:** each milestone ends with a short design note (what, why, what was tested, open doubts) and two questions from the reviewer agent that you answer before merging. The goal is that you can explain every module in an interview.

## Honest framing and non-goals

**Framing.** Built without production CI access, using a fault-injection harness that generates labeled incidents. The same engine runs unchanged against real GitHub Actions, where re-run and pull-request remediations execute for real. Tier 1 and 2 actions are gated, dry-run, and audited with stubbed execution. No claim of production traffic, real incident volume, or team adoption.

**Non-goals.**

- Exhaustive root-causing; the agent narrows enough to act or escalate.
- Acting above a tier's deterministic eligibility, whatever the model's confidence.
- Remediating fleet-wide incidents; those escalate by design.
- Paging on governance or transient classifications.
- Irreversible action without verifiable deterministic evidence.
- Merging PRs. A human always merges.

**Open questions.**

- [ ] Target ship date for the MVP.
- [ ] Budget for live evals and the daily spend cap (USD).
- [ ] Which model(s) for the investigator vs. the reviewer.
- [ ] Keep "DriftGate" as the name, given the project is no longer about drift?