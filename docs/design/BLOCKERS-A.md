# BLOCKERS-A: abort ceiling N and log redaction

## Abort ceiling N (`config/policy.json`)

In plain language: N is how many failing executions can share a cause before DriftGate stops trying to fix it and hands
the whole thing to a human. A blast radius **over N** executions is "fleet-wide" and is always escalated, whatever the
agent proposed. The gate can only downgrade, so a larger N never lets the agent do something the other gates forbid; it
only decides how big an incident the agent may still act on.

- **N = 2 (shipped):** any incident hitting 3 or more executions escalates. Matches the PRD's "fleet-wide problems are
  not for an agent to fix" and the 3+ definition in `fleet_correlate`. Safest, but a modest 3-run incident with an
  obviously safe Tier 0 fix (e.g. a throttled runner pool) goes to a human.
- **N = 10:** the agent may act on incidents up to 10 executions. Fewer pages for humans, but a wrong action then affects
  up to 10 pipelines before anyone sees it.
- The PRD gives no value; the human decides. Edit `abort_ceiling` in `config/policy.json` (integer 0..1000). Missing,
  unreadable or invalid file fails closed to 0 (escalate any correlated failure). The value in force is written to every
  `gate_decision` audit entry as `abort_ceiling`. `SafetyGate`'s own default (10) is unchanged; the orchestrator builders
  read the file (path injectable via `policy_path`; an explicit `abort_ceiling=` argument still wins, used by tests).

## Redaction hardening (A4)

See the commit messages and `tests/tools/test_redaction_corpus.py`. Summary is in the milestone report.
