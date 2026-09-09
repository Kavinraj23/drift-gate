# CI Pipeline Failure Triage & Remediation Agent — Task Tracker

Derived from `PRD.md` §15's cut order (build in reverse of the cut order — highest
priority first). Each task cites the PRD section it implements or verifies.

**Rule:** check a box only when the linked section is both implemented *and* still
accurate. If building something reveals the spec was wrong or incomplete, edit
`PRD.md` first, then check the box. ~40 hours total, alongside coursework — phases are
priority-ordered, not day-numbered, since time per day will vary.

## Phase 0 — Baseline data (nothing else is measurable without this)
- [x] `ExecutionSource` / `RemediationTarget` protocols defined (§9)
- [x] Real error strings collected from real sources — Terraform/K8s/cloud, multi-line
      `Error:` blocks preserved exactly (§11 "Error text is not synthesized")
- [x] Synthetic population generator (~200 lines): 30 days, ~12 pipelines, ~80% success
      base rate, 60/25/15 user/platform/transient split among failures (§11)
- [x] Generator: 3–4 correlated bursts (shared connector/template, 20-min window)
- [x] Generator: 2 flaky pipelines (fail-then-pass on retry)
- [x] Generator: change timeline — real backdated git commits + events table with decoys
- [x] Every generated failure carries ground-truth label + correct remediation
- [x] `SyntheticSource`/`Target` implemented against the generator's population

## Phase 1 — Deterministic classifier (measured before the agent layer, §13)
- [x] Status gate — step 1 of §8, governance/user-abort close immediately
      (`drift_gate/classifier.py:status_gate`; synthetic generator never emits
      REJECTED/ABORTED today, so this path is untested against real data - noted in
      the module docstring as a known gap, not hidden)
- [x] Structured classification — step 2, step-type × failure-type × message signatures
      (`classifier.py:match_signature` + `RULES`, authored independently of
      `generator/faults.py` - see module docstring)
- [x] Fingerprinting scheme defined and applied (`classifier.py:fingerprint`,
      `template:step_type:fault_id`)
- [x] Fleet correlation — step 3, uses generator's correlated bursts
      (`classifier.py:fleet_correlate`)
- [x] Flake check — step 4, uses generator's flaky pipelines
      (`classifier.py:flake_check` - next-execution-in-window, not "any success nearby")
- [x] Output contract (§10) emitted for every classified execution
      (`classifier.py:to_report` for resolved cases; `agent/loop.py:run_agent` for
      escalated ones - both produce the same `Report` type)
- [x] Evidence-source citation enforced in code (no claim without a backing tool result)
      (`agent/loop.py:run_agent` rejects any `submit_report` evidence item whose
      `source` isn't a tool actually called this run)
- [ ] **Baseline accuracy measured** on synthetic population, before any LLM involved
      (only 4 hand-picked demo cases run so far, not the full 186-execution ground
      truth set - still open)

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
- [x] Log extraction (step 5) wired into evidence bundle (`get_step_logs` tool, agent
      calls it itself when it needs raw log text beyond the signature match)
- [x] Direct Anthropic SDK tool-use loop, custom orchestration — no framework
      (`drift_gate/agent/loop.py`, `drift_gate/agent/tools.py`)
- [ ] Evidence bundle assembled from steps 1–7, LLM reasons on the residual only
      (steps 1-4 done and handed in; step 6 change correlation and step 7 historical
      similarity are Phase 5 and not wired in yet, so the agent only reasons on 1-5)
- [x] Evidence-source citation enforced end to end (§10) (rejected in-loop by
      `run_agent`, not by prompting alone)

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
