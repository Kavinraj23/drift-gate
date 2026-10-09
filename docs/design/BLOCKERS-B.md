# Blockers pass B (2026-10-08)

Built, in four parallel branches merged into `mvp1/blockers`:

- **Flaky relabel (eval).** Flaky-test cases (sc-05, sc-07, sc-13) are labelled escalate: their only signature is the generic exit code, and invariant 3 needs a deterministic signature match for Tier 0. All 33 scenarios now match with no label conflicts; the counted/excluded report tables are one table.
- **Redaction (core).** `tools/redaction.py` with vendor token shapes, PEM/SSH/PGP blocks, URL credentials, auth headers, CLI flags, key:value forms and an entropy check; 79 fake-secret cases and 22 benign lines in `tests/tools/redaction_corpus.json` (generator kept in `tests/tools/gen_redaction_corpus.py`). Abort ceiling N now comes from `config/policy.json`, fails closed, and is recorded in gate audit entries.
- **Prices, record, eval-live (agents).** Sourced price table, replay fixtures fingerprinted (stale fixtures raise `FixtureStale`), `eval/live.py` behind `tasks.py record` / `eval-live` (human-only, refuse under `.autonomous`, need `--yes-spend`, cumulative cap, `--estimate` loads no key).
- **Research.** Real error samples with sources in `docs/research/real_error_samples.md`; not yet added to the catalog.

Open doubts: run loops of record/eval-live are untested against a stub SDK client; price rows are taken as verified on 2026-10-08 without re-check; redaction is regex plus entropy, so unknown secret shapes still pass.
