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
- [x] **Baseline accuracy measured** on synthetic population, before any LLM involved
      (`scripts/measure_baseline.py`, run against full 186-execution ground truth ->
      `data/synthetic/baseline_report.json`. Resolves 51/186 (27%) with zero LLM calls,
      0 wrong actions among those 51 - i.e. 100% precision on what auto-executes.
      Escalates the remaining 73%. Running this surfaced two real bugs in
      `classifier.py`, both fixed: (1) `policy_denial`/governance cases crashed on
      `rule.tier.value` since that rule has `tier=None`; (2) flake-resolved results
      never attached a `rule`, so `to_report` emitted `remediation=None,
      abstained=True` for cases that should auto-retry - flakes now carry a Tier 0
      retry rule. Regression tests added in `tests/test_classifier.py`.)

## Phase 2 — Real GitHub Actions adapter (§11 "what is genuinely real")
- [x] `GitHubActionsSource`: `get_execution`, `get_failed_leaf_nodes`, `get_step_logs`
      (budgeted), `list_executions` (`drift_gate/github_actions/source.py`, unit
      tests in `tests/test_github_actions.py` against a fake client; also run live
      against a real execution via `scripts/run_github_demo.py`. Field mapping for
      connector/template/runner_pool documented in the module docstring since GH
      Actions has no native equivalents. GH's per-step timestamps are only
      second-granular, which collapses distinct steps on a sub-second-fast job -
      log slicing uses the log's own `##[group]Run ...` markers instead, see
      `_slice_step`'s docstring)
- [x] Log extraction: failed step only, char budget at the tool boundary, ANSI strip
      (§11 "Log handling"). **Not done**: collapse repeated lines, credential
      redaction, rank all error blocks when a step has more than one - fine for this
      project's demo workflow (one small, non-secret error block), would need doing
      before pointing this at a real production repo's logs
- [x] `GitHubActionsTarget`: Tier 0 retry — real re-run via API (§4)
      (`drift_gate/github_actions/target.py`; `scripts/run_github_demo.py` runs the
      real loop end to end: dispatch a run that fails on attempt 1 -> real source
      pulls it -> deterministic classifier resolves it as Tier 0 -> real
      `rerun-failed-jobs` call -> real verify() confirms success)
- [x] `GitHubActionsTarget`: Tier 3 PR — real branch + diff + PR via API, evidence
      bundle as PR description (§4) (`drift_gate/github_actions/target.py`,
      `_execute_pr`: Git Data API blob -> tree -> commit -> ref -> pull, no local
      git needed. Unit-tested against a fake client; also run live - opened a real
      PR (github.com/Kavinraj23/drift-gate/pull/1) fixing `demo/lockfile-fixture.txt`.
      verify() deliberately never reports resolved=True for this tier - a human
      merges it, that's the tier's actual safety property. **Integration gap**: the
      agent has no tool yet to read a target repo's real files, so it can't itself
      discover which file to fix - context must already carry
      `file_path`/`new_content` from elsewhere. Wiring that is follow-up work, not
      done tonight.)

## Phase 3 — Gates, verification, audit (§5, §6)
`drift_gate/gates.py` (`SafetyGate`) + `drift_gate/audit.py` (`AuditLog`) built this
session and wired into `scripts/run_github_demo.py`, which now calls the gate instead
of the target directly. Unit-tested (`tests/test_gates.py`) and confirmed live: ran
the real demo 3x in a row against the real repo - executions 1 and 2 succeeded for
real, the 3rd was correctly rejected by the rate limiter with no real GitHub mutation,
all three outcomes recorded in `data/audit_log.jsonl`.
- [ ] Tier 0 gate: confidence ≥ 0.9 AND signature match AND fail-then-pass history, max
      2/fingerprint/hour, no human. **Partially real, one flagged spec gap**: max
      2/fingerprint/hour is real and gate-enforced (see above); confidence≥0.9 is
      trivially true today since `to_report` hardcodes confidence=0.95 for every
      resolved case, not a dynamic check; "no human" is true by construction (AUTO
      gate never prompts). **Open question, not resolved**: PRD.md §4 says Tier 0
      eligibility requires "a fail-then-pass history for this fingerprint" - but
      `classifier.py`'s RULES-matched Tier 0 resolutions (e.g. `cloud_throttling`,
      `registry_429`) auto-execute on a signature's FIRST occurrence, with no prior
      fail-then-pass precedent required for that specific fingerprint (only the
      separate `is_flake` path checks real fail-then-pass history). Whether
      RULES-authored "known-safe" signatures should be allowed to bypass that
      per-fingerprint precedent, or whether PRD.md §4 needs updating to reflect that
      distinction, hasn't been decided.
- [ ] Tier 3 gate: PR-is-the-gate, no direct mutation - true by construction in
      `_execute_pr` (opens a PR, never merges), not separately gate-enforced yet
- [ ] Verification loop: observe next execution, check fingerprint recurrence, mark
      failed remediations, escalate with original + failed evidence attached (§6).
      `RemediationTarget.verify()` exists and was proven live (both for Tier 0 retry
      and Tier 3 PR status) but only via manual/scripted calls - there's no automated
      recurring process that watches for the next execution on its own and
      auto-escalates a failed remediation
- [x] Audit trail on every proposal incl. rejected/expired/failed: who/what/why/approver
      /outcome/timestamps (§5) (`drift_gate/audit.py`; `approver` is always `None`
      today - real human-approval workflows for single/dual-approval gates aren't
      wired, so there's nothing to attribute yet)
- [x] Kill switch, global + per-tier (§5) (`drift_gate/gates.py:KillSwitch`,
      JSON-file-backed at `config/kill_switch.json`; unit-tested, not yet exercised
      live since flipping it would have blocked tonight's real demo runs)
- [x] Rate limiting: 2 attempts per fingerprint, then hard escalate (§5)
      (`SafetyGate._rate_limited`; confirmed LIVE against the real repo - see above)

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
