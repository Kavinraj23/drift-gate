# GitHub REST fixtures: hand-written stubs

Every file here was **written by hand** to match the documented shapes of the GitHub REST API (workflow runs, run
attempts, jobs and steps, contents, git refs, pull requests, issue comments, rate-limit errors). None was recorded
from the live API. They exist so `tests/github` can run offline.

M8b (human-run) should record real responses from `drift-gate-playground` and either replace these files or add a
test that checks the stubs have the same keys and types as the recorded ones. Until then the adapter's behaviour
against the live API is **unverified** (see `docs/design/M8a.md`, "Open doubts").

Error text: `job_9001.log` contains only the runner's standard markers (`##[group]`, `##[endgroup]`, and
`##[error]Process completed with exit code 1.`); it is a stand-in for a recorded log, not a captured one. No tool
error text (Terraform, npm, AWS) is written into any fixture.

Ids, shas, names and URLs are placeholders (`octo-org/drift-gate-playground`, run 7001, job 9001).
