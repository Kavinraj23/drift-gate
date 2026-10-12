Sample audit log for `python tasks.py trace-report`

Produced by `python -m driftgate.eval.trace_sample --data data --out fixtures/trace_sample` (the real orchestrator,
scripted model, synthetic dataset, no network). Scenarios: sc-06 (Tier 0 re-run, verified), sc-01 (Tier 3 pull request),
sc-15 (escalation), sc-11 (a Tier 0 re-run the gate downgrades), sc-17 (drill d3: wrong first PR, CI red,
one re-investigation, revised PR green), sc-10 (Tier 1: self-approval refused, second
person approves, executed through the approval flow), sc-12 (Tier 1: denied).

Each scenario ran with its own audit log; they are concatenated here with `seq` renumbered across the file and
timestamps (and `expires_at`, on the same clock) shifted per scenario. Entries are otherwise untouched.
`reports.json` maps execution id to `Report.to_dict()`, because the audit log keeps evidence counts, not evidence
items. Error text comes from the synthetic dataset's real-source catalog; nothing is synthesized here.
