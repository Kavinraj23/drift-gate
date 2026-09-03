# CI Pipeline Failure Triage & Remediation Agent — Task Tracker

Derived from `PRD.md` §15's cut order (build in reverse of the cut order — highest
priority first). Each task cites the PRD section it implements or verifies.

**Rule:** check a box only when the linked section is both implemented *and* still
accurate. If building something reveals the spec was wrong or incomplete, edit
`PRD.md` first, then check the box. ~40 hours total, alongside coursework — phases are
priority-ordered, not day-numbered, since time per day will vary.

## Phase 0 — Baseline data (nothing else is measurable without this)
- [ ] `ExecutionSource` / `RemediationTarget` protocols defined (§9)
- [ ] Real error strings collected from real sources — Terraform/K8s/cloud, multi-line
      `Error:` blocks preserved exactly (§11 "Error text is not synthesized")
- [ ] Synthetic population generator (~200 lines): 30 days, ~12 pipelines, ~80% success
      base rate, 60/25/15 user/platform/transient split among failures (§11)
- [ ] Generator: 3–4 correlated bursts (shared connector/template, 20-min window)
- [ ] Generator: 2 flaky pipelines (fail-then-pass on retry)
- [ ] Generator: change timeline — real backdated git commits + events table with decoys
- [ ] Every generated failure carries ground-truth label + correct remediation
- [ ] `SyntheticSource`/`Target` implemented against the generator's population

## Phase 1 — Deterministic classifier (measured before the agent layer, §13)
- [ ] Status gate — step 1 of §8, governance/user-abort close immediately
- [ ] Structured classification — step 2, step-type × failure-type × message signatures
- [ ] Fingerprinting scheme defined and applied
- [ ] Fleet correlation — step 3, uses generator's correlated bursts
- [ ] Flake check — step 4, uses generator's flaky pipelines
- [ ] Output contract (§10) emitted for every classified execution
- [ ] Evidence-source citation enforced in code (no claim without a backing tool result)
- [ ] **Baseline accuracy measured** on synthetic population, before any LLM involved

## Phase 2 — Real GitHub Actions adapter (§11 "what is genuinely real")
- [ ] `GitHubActionsSource`: `get_execution`, `get_failed_leaf_nodes`, `get_step_logs`
      (budgeted), `list_executions`
- [ ] Log extraction: failed step only, char budget at the tool boundary, ANSI strip,
      collapse repeated lines, credential redaction, rank all error blocks (§11 "Log
      handling")
- [ ] `GitHubActionsTarget`: Tier 0 retry — real re-run via API (§4)
- [ ] `GitHubActionsTarget`: Tier 3 PR — real branch + diff + PR via API, evidence
      bundle as PR description (§4)

## Phase 3 — Gates, verification, audit (§5, §6)
- [ ] Tier 0 gate: confidence ≥ 0.9 AND signature match AND fail-then-pass history, max
      2/fingerprint/hour, no human
- [ ] Tier 3 gate: PR-is-the-gate, no direct mutation
- [ ] Verification loop: observe next execution, check fingerprint recurrence, mark
      failed remediations, escalate with original + failed evidence attached (§6)
- [ ] Audit trail on every proposal incl. rejected/expired/failed: who/what/why/approver
      /outcome/timestamps (§5)
- [ ] Kill switch, global + per-tier (§5)
- [ ] Rate limiting: 2 attempts per fingerprint, then hard escalate (§5)

## Phase 4 — LLM reasoning loop (§8 step 8, §13)
- [ ] Log extraction (step 5) wired into evidence bundle
- [ ] Direct Anthropic SDK tool-use loop, custom orchestration — no framework
- [ ] Evidence bundle assembled from steps 1–7, LLM reasons on the residual only
- [ ] Evidence-source citation enforced end to end (§10)

## CHECKPOINT — deterministic baseline vs. agent (§13, §12)
- [ ] Deterministic-only accuracy measured standalone (from Phase 1)
- [ ] Agent-layer accuracy measured on the same holdout
- [ ] Comparison documented — this is the "honest baseline" the project's credibility
      depends on

## Phase 5 — Cuttable, in the PRD's stated cut order (§15)
Build in this order; if time runs out, whatever's left here is what gets cut first —
starting from the bottom of this list, not the top.
- [ ] Change correlation — step 6 of §8: git log + events table, decoys included
- [ ] Historical similarity — step 7 of §8: fingerprint-family matching
- [ ] Tier 1 simulated actions — gate/dry-run/audit real, underlying action stubbed (§4)
- [ ] Tier 2 simulated force-unlock — holder-death proof logic real, against a
      simulated lock table, dual-approval gate (§4)
- [ ] `HarnessSource`/`Target` stub — only if time allows (§9)

## Phase 6 — Evaluation (§12)
- [ ] 8–10 held-out scenarios, untouched during development
- [ ] Second holdout: scraped real GitHub Actions failures (generalization test)
- [ ] Remediation success rate
- [ ] False remediation rate (weighted heaviest)
- [ ] Escalation precision
- [ ] Classification accuracy: deterministic baseline vs. agent
- [ ] Time-to-resolution vs. human baseline

## Phase 7 — Writeup
- [ ] README with honest framing (§14) — what's real (GH Actions retry + PR) vs.
      stubbed (Tier 1/2 actions, Harness) stated plainly, no prod-traffic/adoption claims
- [ ] Architecture note / demo

## Never cut (§15)
Deterministic classifier w/ fingerprinting + dedup, real GitHub Actions adapter, working
Tier 0 retry, working Tier 3 PR remediation, measured success rate. That combination
alone is a complete, honestly describable project even if everything in Phase 5–7 slips.
