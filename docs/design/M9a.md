# M9a: eval harness, offline report, README, demo

## What was built

- `src/driftgate/eval/report.py`: runs the deterministic baseline and the agent pipeline (orchestrator on the scripted model, or gateway replay fixtures where recorded; `--mode auto` like `e2e`) over the same labelled scenarios, scores both against ground truth, and writes `data/eval/report.md` and `report.json`. Dev (24) and held-out (9) are separate columns. Output has no timestamps, so it is deterministic for a given dataset.
- `python tasks.py eval` (offline, exit 0). `eval-live` stays unimplemented and human-only.
- `README.md` (with the offline table embedded and a test that keeps it in sync), `docs/DEMO.md` (offline script, plus clearly marked human-only live steps).
- `tests/eval/test_report.py`.

## Metric definitions (the decisions)

- *Acted* = a remediation reached the target (Tier 0 executed or Tier 3 PR opened), and a first attempt that later failed verification still counts as acted (taken from `Outcome.first_round`). Approval-gated Tier 1/2 proposals count as declined.
- *False remediation* = acted without it being the label's correct action (label says escalate/close, or a different action). Headline rate is over all cases; the split by cause (acted where the label says not to, acted with the wrong action) sits beside it with its own denominator.
- *Remediation success*: agent only, `eval/metrics.remediation_success_rate` (verified / attempted). The baseline has no verifier, so its column shows the label-correct share of actions as a clearly named proxy.
- *Recovery rate*: `metrics.recovery_breakdown` over scenarios plus the five loop drills; only a verified fix counts; correct escalations after a failed attempt are listed separately. The drills are dev-only, so held-out has no recovery figure.
- *Escalation precision*: of declined cases, the share where the label says declining was right. *Recall*: label-remediate cases fixed with the correct action (added so the label-conflict cases show up).
- *Tier 3 PR quality*: `score_tier3` / `tier3_quality` (diff matches the injected fault's correct fix by resulting-file hash), plus a seeded-bad-diff drill: one dev scenario per fix action x every applicable `BAD_KINDS`, run through the real Tier 3 flow with the strict scripted reviewer.
- *Cost*: tokens from the run stats, USD from `llm/config.DEFAULT_PRICES` (priced as the default haiku model; the scripted model spends nothing). *Latency* is 0 offline and labelled not meaningful.
- *Tool efficiency*: of cases resolved correctly, the share using no `get_step_logs` / `read_repo_file` (the two tools whose descriptions say "Medium").
- *Label conflicts* (sc-05, sc-07, sc-13): flagged by the e2e judge; shown in both tables (counted as misses, excluded) and in a footnote.

## Why

The PRD wants agent vs. baseline on the same labelled scenarios with the honest framing intact. Offline the agent column is scripted, so the report states that in the header, in a dedicated block, in the JSON, and in the README, and flags every n < 10.

## Tested

Metric definitions on hand-built cases (false remediation, precision, recall, success, tool efficiency, accuracy, cost math, empty sets, small-n flag); rendering on a hand-built report (held-out separated, conflicts both ways, honesty text); the real run (33 scenarios, same ids for both systems, conflicts exactly sc-05/07/13, populated recovery and Tier 3 sections, no missed bad diffs); `main` writes artifacts to an injected dir and matches the in-process render (determinism); README table in sync; `tasks.py eval` registered and `eval-live` still human-only; README and DEMO reference only existing targets; the report module imports no SDK or network code (the suite also blocks sockets).

Existing assertion changed: `tests/test_tasks.py::test_not_implemented_target_exits_1` used `eval` as its not-implemented example; `eval` is now implemented, so it uses `eval-live` (still not implemented, M9b). The assertion's meaning is unchanged.

## Open doubts

- The agent column is correct by construction, so its 0 false remediation, 100% success and high recall prove nothing about a model. Only `record` + `eval-live` can.
- The baseline is also 0 false remediation here; the headline "agent beats baseline" is only a recall gap that the script creates.
- Tool efficiency for the scripted agent is low because the script always reads logs; the metric is meaningful only for a real model.
- The bad-diff drill uses one scenario per fix action, not all nine, to keep runtime down; the reviewer is scripted, so the catch rate is a pipeline check.
- `tasks.py` `IMPLEMENTED_MILESTONES` and `NOT_IMPLEMENTED` will conflict textually with M8a if it edits the same lines; resolve by keeping both additions.
- `python tasks.py eval` takes about 30 seconds (it runs the full e2e twice over: scenarios, then drills and bad-diff runs). The test module takes about a minute.
