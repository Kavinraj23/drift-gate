# M9b: live numbers (`record`, `eval-live`)

## Built
No new feature. M9b is two human-run spending passes plus the fixes they exposed.
- `python tasks.py record --all` recorded replay fixtures for all 31 model-using scenarios (Haiku 4.5 for both roles). `python tasks.py e2e` and `python tasks.py eval` replay them for free.
- `python tasks.py eval-live` ran all 33 labelled scenarios again, held-out first, and wrote `data/eval/report-live.md`.
- Gateway: optional `DRIFTGATE_DAILY_CAP_USD` and `DRIFTGATE_LIFETIME_CAP_USD` (env or `.env`). Defaults unchanged ($1.00 and $2.50); malformed or non-positive values raise; the lifetime override is refused above $10; the daily cap stays clamped to the lifetime cap.
- Generator: `write_dataset` no longer deletes `data/spend.sqlite` or `data/audit_live.jsonl`.
- Report: the offline report no longer calls replayed real-model recordings "scripted" (`offline_honesty`).
- Redaction hardening (21 corpus cases, 9 false-positive guards) landed before the first `record`, see PROGRESS.md.

## Results
See `MVP_REPORT.md`, "Real-model results" and "Second, independent live pass". In short: 27/31 then 26/31 model scenarios matched their label, 0 false remediations in both, the same four lockfile or provider-pin scenarios missed both times, classification worse than the baseline (79% against 96% on dev), mean cost about $0.029 and mean model latency about 19 s per case.

## Why
The PRD requires the agent-vs-baseline eval to be backed by real numbers, not only a scripted model. Two independent samples were affordable and show which results repeat.

## What was tested
Lint and the full suite (1282 tests at the last full run, plus the tests added since) offline; the config overrides, the ledger-preserving writer and the report disclaimer each have a test. The spending passes themselves were run by the user.

## Incidents, kept honest
- `mvp-check` and `tasks.py data` wiped the spend ledger and live audit log (fixed, ledger restored with a labelled carry-over row; the M8b live audit trail is lost).
- `record --all` was submitted twice; the duplicate cost about $0.31 and overwrote sc-01's fixture with a passing recording (disclosed in the report).
- The lifetime budget was exceeded by $0.30 ($2.7995 against $2.50) by the owner's explicit decision.

## Open doubts
- Two samples per scenario on one model is not a success rate; the held-out set has 9 cases.
- Strict byte-for-byte scoring of Tier 3 diffs counts alternative valid fixes as misses, and synthetic "verified" is a file-path simulation.
- The recorded fixtures are keyed on the current prompts and tool schemas; any change to them means paying for `record` again.
- Haiku 4.5 is the only model measured; Sonnet 5.5 fixed the lockfile case once live but was not measured here.
- `CLAUDE.md` and the code default still say $2.50.
