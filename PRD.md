# CI Pipeline Failure Triage & Remediation Agent — PRD

*Full rewrite, September 2026. Supersedes the earlier DriftGate/harness-triage draft —
this version is more specific, more grounded in real internship incidents, and scoped to
what's actually buildable solo in ~40 hours. Where the two drafts conflict, this one
wins: architecture, remediation model, and eval design all changed.*

---

## 1. Background

CS student (Texas A&M '28), just off a SWE internship on a developer platform team. The
main project there was a governed self-service platform for OpenTofu/Terraform state
operations — Go backend, Next.js console, Harness CI/CD pipeline, human approval gates,
immutable audit log. That work is where this project's domain knowledge and its safety
model both come from.

Three incidents from that internship directly shape the design:

- **Cross-workspace move, partial import.** Removing a resource from the source state
  before importing it to the destination — a partway import failure orphaned a live
  production resource. Fixed by reordering to import-first: any failure leaves the
  source untouched and retryable, so the worst case becomes a harmless duplicate instead
  of a silent orphan.
- **Three variable-wipe incidents** during a migration operation, root-caused to a shell
  counting bug and a missing-map contract. Recovered via snapshot rollback. Closed out
  with a hard abort on empty proposed sets and a mass-wipe ceiling that refuses to delete
  100% of a resource set.
- **A stalled UI**, traced to runner webhook callbacks being firewalled — a failure whose
  symptom appeared several layers away from its cause, and which never produced a failed
  status at all.

Those three lessons — **additive before subtractive**, **abort ceilings**, and
**symptoms that don't match layers** — are the safety spine of this project.

This is a flagship portfolio project for Summer 2027 SWE internship applications,
targeting big-tech infrastructure/platform roles.

## 2. What it is

An agent that investigates failed CI pipeline executions, forms an evidence-backed
hypothesis, and proposes or executes a bounded remediation behind an approval gate
appropriate to the risk of the action.

The audience is the platform on-call engineer, not an outside requester. **The
deliverable is a fix or a fix proposal, not a routing decision.** Classification is not
the product — it's the gate that determines which remediations are eligible.

## 3. The loop

```
investigate → hypothesize (evidence-backed) → select bounded remediation
  → dry-run → approval gate → execute → verify → audit
```

Same four-stage safety contract as the original state-ops platform (dry-run, approval,
execute, audit), applied to CI failures instead of state operations.

## 4. Remediation catalog

Tiered by blast radius, with a different gate per tier.

**Tier 0 — idempotent, auto-executable.** Retry the execution, re-run a single failed
step, clear a stale cache.
Gate: confidence ≥ 0.9 AND a deterministic signature match AND a fail-then-pass history
for this fingerprint. Max 2 retries per fingerprint per hour. No human. Cost of a wrong
action is a wasted build minute.

**Tier 1 — bounded platform actions.** Recycle an unresponsive runner, refresh a
connector token where the credential source is still valid, reschedule a stuck pod,
temporarily bump pool capacity.
Gate: single approval, 15-minute timeout, dry-run output shown first.

**Tier 2 — state-touching, high risk.** Force-unlock a Terraform/OpenTofu state lock
whose holder is provably dead (the execution that acquired the lock is in a terminal
state and its ID matches the lock holder).
Gate: two-person approval, mandatory dry-run showing lock metadata and holder-death
proof, hard refusal if the holder execution is still running. **Model confidence alone
never qualifies for this tier.**

**Tier 3 — code changes as pull requests.** Regenerate a lockfile after a provider
mismatch, pin a provider version that broke on upgrade, revert a template version bump
that broke callers, fix an expression referencing an undefined variable, correct a
secret reference at the wrong scope.
Gate: the PR is the gate. The agent opens a branch, writes the diff, opens the PR with
the evidence bundle as the description, and stops. No direct mutation, fully reviewable,
zero execution risk.

**Tier 3 is the highest-value tier for this project**: it produces an artifact a human
can read and judge, it's genuinely useful, and it's safe by construction.

## 5. Safety constraints

- **Abort ceiling.** Blast radius exceeding N executions, or a remediation touching more
  than one shared resource, stops and escalates. Fleet-wide problems are not for an
  agent to fix.
- **Additive before subtractive.** Never remove before adding — a partial failure should
  leave a harmless duplicate, not a silent orphan.
- **Deterministic evidence required for Tier 2+.** Eligibility gates on verifiable
  facts, never on model confidence alone.
- **Rate limiting per fingerprint.** Two attempts, then hard escalate — prevents
  remediation storms.
- **Full audit trail** on every proposal, including rejected, expired, and failed ones:
  who, what, why, approver, outcome, timestamps.
- **Kill switch**, global and per-tier.

## 6. Verification

Executing a remediation isn't the end of the loop. After acting: observe the next
execution, check whether the fingerprint stopped recurring, and if it didn't, mark the
remediation failed, roll back if reversible, and escalate with both the original
evidence and the failed attempt attached.

This also produces the headline metric — **remediation success rate** — which is more
compelling than classification accuracy alone.

## 7. Failure taxonomy

Layer maps closely onto ownership and onto which remediations are eligible.

| Layer | Meaning | Examples |
|---|---|---|
| L0 | never started | no eligible runner, infra provisioning failed, trigger filtered, required input missing |
| L1 | step infrastructure | image pull backoff, OOMKilled, unschedulable/evicted pod, step timeout |
| L2 | identity and secrets | expired credential, secret at wrong scope, AssumeRole denied, registry 401 vs 429 |
| L3 | pipeline definition | invalid YAML, template not found, template version bump breaking callers, expression resolving null, policy denial |
| L4 | the actual work | tofu init/plan/apply errors, provider version drift, lockfile mismatch, state lock held, cloud throttling/quota, IAM denied |
| L5 | governance | approval rejected or expired — normal terminal states, not incidents, never remediated |
| L6 | post-execution | stalled executions running past p99 with no step transitions. Status-based monitoring is blind to this class; needs its own detector. |

## 8. Investigation procedure

Cheapest first, early exit:

1. **Status gate** — governance outcomes and user aborts close immediately
2. **Structured classification** — step type × failure type × message against known
   signatures
3. **Fleet correlation** — same connector, template version, or runner pool failing
   elsewhere in the last 30 minutes. Flips ownership user→platform and is also the
   abort-ceiling input.
4. **Flake check** — has this fingerprint failed-then-passed recently
5. **Log extraction** — only now, only the failed step
6. **Change correlation** — what changed in the window before first failure
7. **Historical similarity** — has this fingerprint family appeared before
8. **LLM reasoning loop** — on the residual only, with all of the above as its evidence
   bundle

Steps 1–4 resolve most volume with zero model calls — an accuracy decision as much as a
cost one.

## 9. Architecture

The agent never talks to a specific CI provider. It talks to a normalized domain model
behind two interfaces:

```python
class ExecutionSource(Protocol):
    def get_execution(self, execution_id) -> Execution: ...
    def get_failed_leaf_nodes(self, execution_id) -> list[Node]: ...
    def get_step_logs(self, execution_id, node_id, budget: int) -> LogChunk: ...
    def list_executions(self, window, filter) -> list[ExecutionSummary]: ...

class RemediationTarget(Protocol):
    def dry_run(self, action: Remediation) -> DryRunResult: ...
    def execute(self, action: Remediation) -> ExecutionResult: ...
    def verify(self, action: Remediation) -> VerificationResult: ...
```

Implementations: `GitHubActionsSource`/`Target` (real), `SyntheticSource`/`Target`
(simulated), `HarnessSource`/`Target` (stub, only if time allows).

The node graph is modeled as a tree with parent/child edges — the useful information is
always in the deepest failed leaf, not the stage that reports "failed." Executions carry
refs for connector, template (with version), runner pool, and infra definition — those
are the join keys correlation depends on.

## 10. Output contract

```json
{
  "execution_id": "str",
  "fingerprint": "str",
  "duplicate_of": "str | None",
  "classification": "platform|user|transient|governance|unknown",
  "layer": "L0..L6",
  "confidence": 0.0,
  "blast_radius": {"executions_affected": 0, "shared_dimension": "str"},
  "evidence": [{"source": "str", "finding": "str", "supports": "str"}],
  "suspected_change": {"kind": "str", "ref": "str", "at": "str", "basis": "str"},
  "remediation": {
    "tier": 0,
    "action": "str",
    "rationale": "str",
    "reversible": true,
    "gate": "auto|single_approval|dual_approval|pull_request",
    "dry_run": {}
  },
  "abstained": false,
  "escalation_reason": "str"
}
```

**Every evidence item's `source` must correspond to an actual tool result from that
run** — a claim that can't cite one doesn't get emitted. Enforced in code, not by
prompting.

## 11. Development constraints — solo, no enterprise tooling

Built independently: no Harness account, no corporate cloud account, no Kubernetes
cluster, no production CI traffic, no team, no on-call rotation, no budget. That
constraint shapes the whole build.

### What is genuinely real, at zero cost

**GitHub Actions** is the real substrate — free and unlimited on public repos, full REST
API, real executions, a real job/step graph, real logs, and real remediation surfaces:

- **Tier 0 retry is real** — re-run a workflow or a single failed job via the API
- **Tier 3 PRs are real** — actual branches and pull requests with actual diffs, via the
  GitHub API

The two most important tiers execute against real infrastructure on real failures. That
closes the "is any of this real" objection that otherwise sinks portfolio agent
projects.

Also free:
- **Public failed workflow logs** from other repos — a hand-labeled holdout set not
  self-authored
- **LocalStack** for real OpenTofu against a fake AWS, if a real state lock and real
  provider errors are wanted
- **A local git repo** as the change timeline, with real backdated commits

### What is necessarily simulated

- Harness-specific structure (delegates, connectors, IACM workspaces)
- Tier 1 platform actions (runner recycling, token refresh, pod rescheduling) — gates,
  dry-run, and audit fully implemented; the underlying action is stubbed
- Tier 2 state force-unlock — implemented against a simulated lock table, with
  holder-death proof logic real and tested
- **A fleet.** Fleet correlation is one of the strongest signals in the system and needs
  a population of concurrent executions across teams, which a solo developer can't
  produce naturally.
- **History.** Flake detection and historical similarity need weeks of prior executions.

### The synthetic population

A generator (~200 lines) produces roughly 30 days of backdated executions across ~12
pipelines:

- Realistic base rates: ~80% success; of failures, roughly 60% user / 25% platform / 15%
  transient. Priors matter more than volume.
- 3–4 correlated bursts: multiple pipelines sharing a connector or template version,
  failing inside a 20-minute window
- 2 genuinely flaky pipelines that fail-then-pass on retry
- A change timeline of real backdated git commits plus an events table for template
  bumps and credential rotations, including **decoy changes with no causal link** — so
  recency heuristics don't get credit they haven't earned
- Every generated failure carries its ground-truth label **and** its correct
  remediation, since the fault was injected deliberately

### Error text is not synthesized

Terraform, Kubernetes, and cloud provider error strings are extremely specific — a
classifier trained on invented or paraphrased strings matches nothing real. Exact real
strings only: Terraform's `Error acquiring the state lock` and `Error: Inconsistent
dependency lock file`, `OOMKilled`/exit 137, `ImagePullBackOff`, `ThrottlingException` —
with Terraform's multi-line `Error:` block shape preserved exactly, since line-based
parsing mangles it. Collected from real sources, not generated.

### Log handling

Log volume is the practical killer for this class of system. The extractor:
- fetches only the failed step
- enforces a character budget **at the tool boundary**, not by asking the model to be
  brief
- strips ANSI codes
- collapses repeated lines from spinners and retry loops
- redacts credential patterns before anything reaches a model
- collects **all** error blocks for ranking rather than taking the first — cascading
  errors mean the first is frequently not the operative one

## 12. Evaluation

Ground truth comes free from the generator, since the fault and its correct fix are both
known at injection time.

Metrics:
- **Remediation success rate** — did the action resolve the failure
- **False remediation rate** — acted when it should not have. Weighted heaviest; a wrong
  action is far worse than an abstention.
- **Escalation precision** — when it declined to act, was declining correct
- **Classification accuracy** — deterministic baseline vs. agent
- **Time-to-resolution** vs. a human baseline

8–10 scenarios held out, not examined during development. A separate holdout of real
scraped GitHub Actions failures tests whether the signatures generalize beyond the
generator's templates.

## 13. Technical choices

- **Python only.** A second language costs glue time and demonstrates nothing the
  project needs.
- **The agent tool-use loop is written directly against the Anthropic SDK.** No
  LangGraph, CrewAI, or agent framework — the loop is small and custom orchestration is
  the defensible choice.
- **The deterministic classifier is built and measured before the agent layer**, so
  there's an honest baseline for the agent to beat.

## 14. Honest framing

Built without production CI access, using a fault-injection harness that generates
labeled incidents plus adapters that run the same engine unchanged against real GitHub
Actions executions, where retry and pull-request remediations execute for real. Tier 1
and Tier 2 actions are gated, dry-run, and audited, with the underlying execution
stubbed.

No claim of production traffic, real incident volume, or team adoption.

## 15. Time budget

~40 hours across 14 days during the fall semester, alongside coursework, a
peer-teaching commitment, and undergraduate research. **Ship date is fixed; scope is the
variable.**

**Cut order when behind:** historical similarity → Tier 1/2 simulated actions → change
correlation → the LLM loop itself.

**Never cut:** a deterministic classifier with fingerprinting and dedup, a real GitHub
Actions adapter, working Tier 0 retry and Tier 3 PR remediation, and a measured success
rate. That combination alone is a complete and honestly describable project. The agent
reasoning layer is the upgrade, not the deliverable.

## 16. Non-goals

- Does not root-cause exhaustively; it narrows enough to act or escalate
- Does not act on anything above its confidence floor for the tier
- Does not remediate fleet-wide incidents — those escalate by design
- Does not page on governance or transient classifications
- Does not take irreversible action without verifiable deterministic evidence
