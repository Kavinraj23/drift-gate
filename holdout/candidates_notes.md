# Holdout candidate selection notes (N1)

> **LABEL-ADJACENT MATERIAL. Only `eval/` and the human may read this file (CLAUDE.md invariant 14).**
> It must not be read by agents, tools, prompts, fixtures or tests, and must not be quoted into any model context.
>
> **These are PRELIMINARY expectations by the selector, NOT labels.** They were guessed from run metadata and the
> short check-run annotation only (no job log was read). The human decides the true layer, classification, correct
> action and the dev/test split (10/20). After the agent has run on a case, nothing in this file may be used to
> influence or edit that case's test-split label. No split has been assigned here.

Selection date: 2026-10-12. Source: public GitHub run pages (annotations and job list only). The GitHub REST API was
not usable from the session (a tool hook blocks api.github.com), so no REST call was made; discovery used public
`/actions` list pages and `/actions/runs/{id}` pages via WebFetch (about 150 page fetches, none authenticated).

Caveats that apply to every row:
- "Step" is the step number from the annotation anchor. Step names are not shown on those pages; the fetcher reports them.
- "Completed" is the date shown on the run page (the page showed some times as Oct 13, a timezone offset). All runs are
  within roughly 6 days of selection, so they are well inside the 90 day log retention.
- Basis text is quoted verbatim from the annotation (shortened to a prefix where long). Many CI failures only say
  `Process completed with exit code N.`; for those the expectation rests on the workflow and job names and is marked low confidence.
- Confidence: H = message is explicit, M = message plus workflow context, L = guess from workflow and job name.
- Escalate rows: "(nearest L3)" means policy or approval denial, which the PRD table places in L3; the expected action is escalate.

## Primary candidates (30, in `candidates.json` order)

| # | Repo | Run id | Job (step) | Completed | Exp. layer | Exp. action | Conf | Basis (annotation, verbatim) |
|---|------|--------|------------|-----------|-----------|-------------|------|------|
| 1 | grafana/grafana | [38303496768](https://github.com/grafana/grafana/actions/runs/38303496768) | verify-i18n (9) | 2026-10-12 | L4 | pr_fix | H | "Translation extraction has not been committed. Please run 'yarn i18n-extract', commit the changes and push again." plus "Process completed with exit code 1." (easy first case) |
| 2 | hashicorp/terraform-provider-aws | [38299873800](https://github.com/hashicorp/terraform-provider-aws/actions/runs/38299873800) | GenerateChangelog (2) | 2026-10-12 | L2 | escalate | M | "The 'client-id' (or deprecated 'app-id') input must be set to a non-empty string." / "If using a secret or variable, ensure it is available in this workflow context." CAVEAT: PR from a fork, so this is the secrets-unavailable-to-forks case you wanted avoided; drop it if you want zero fork-secret cases and use spare list |
| 3 | mastodon/mastodon | [38248209100](https://github.com/mastodon/mastodon/actions/runs/38248209100) | label-rebase-needed (2) | 2026-10-12 | L1 | rerun | M | "HttpError: <!DOCTYPE html> <!-- Hello future GitHubber! I bet you're here to..." (GitHub's own error page returned to a scheduled job) |
| 4 | cli/cli | [37915254425](https://github.com/cli/cli/actions/runs/37915254425) | govulncheck (4) | 2026-10-09 | L4 | pr_fix | H | "download.destinationWriter.ensureRoot calls sync.Once.Do, which eventually calls http2.Framer.WriteHeaders" (govulncheck finding in golang.org/x/net http2; fix is a dependency bump) |
| 5 | huggingface/transformers | [38302891268](https://github.com/huggingface/transformers/actions/runs/38302891268) | Get PR commit SHA / Get PR commit SHA better (2) | 2026-10-12 | L3 | pr_fix | M | "Unhandled error: TypeError: Cannot read properties of undefined (reading '0')" (script in workflow) |
| 6 | facebook/docusaurus | [37991235685](https://github.com/facebook/docusaurus/actions/runs/37991235685) | dependency-review (3) | 2026-10-09 | escalate (nearest L3) | escalate | H | "Dependency review detected vulnerable packages." (policy gate on a Dependabot bump) |
| 7 | supabase/supabase | [38268636509](https://github.com/supabase/supabase/actions/runs/38268636509) | trigger-nimbus-sync (2) | 2026-10-12 | L2 | escalate | H | "Bad credentials - https://docs.github.com/rest" (token must be rotated by a human; same failure on many consecutive pushes) |
| 8 | electron/electron | [38249118359](https://github.com/electron/electron/actions/runs/38249118359) | Add merged PR to release project board (3) | 2026-10-12 | L1 | rerun | H | "non-200 OK status code: 504 Gateway Timeout" |
| 9 | helm/helm | [38326113875](https://github.com/helm/helm/actions/runs/38326113875) | govulncheck (5) | 2026-10-12 | L4 | pr_fix | H | "kube.joinedErrors.Error calls http2.ConnectionError.Error" (govulncheck, http2 finding; scheduled run on main) |
| 10 | langchain-ai/langchain | [38148644755](https://github.com/langchain-ai/langchain/actions/runs/38148644755) | label-by-package (2) | 2026-10-11 | L3 | pr_fix | M | "Could not find "## Package" section in issue #41204 body. The issue template may have changed — update the regex in this workflow." |
| 11 | nodejs/node | [38300564833](https://github.com/nodejs/node/actions/runs/38300564833) | lint-commit-message (3) | 2026-10-12 | escalate (nearest L3) | escalate | H | "not ok 6 signed-off-by: Commit must have a "Signed-off-by" trailer ()" |
| 12 | rails/rails | [37919481101](https://github.com/rails/rails/actions/runs/37919481101) | Devcontainer smoke test (6) | 2026-10-09 | L1 | rerun | L | "Command failed: docker pull ghcr.io/rails/devcontainer/images/ruby:3.4.11" (image pull; the same workflow on 8-0-stable failed on several earlier runs, so a pr_fix is possible) |
| 13 | argoproj/argo-cd | [38224977336](https://github.com/argoproj/argo-cd/actions/runs/38224977336) | build-only / Build and publish image (17) | 2026-10-12 | L4 | pr_fix | H | "cannot use typeSchema.Types (variable of type" ... "as []\"sigs.k8s.io/structured-merge-diff/v6/schema\".TypeDef value in struct literal" (dependabot kustomize/api bump; v6 vs v7 type mismatch) |
| 14 | pydantic/pydantic | [38161828860](https://github.com/pydantic/pydantic/actions/runs/38161828860) | Send tweet (3) | 2026-10-11 | L2 | escalate | L | "Process completed with exit code 2." (only the "Send tweet" job failed on a release tag run; likely missing or invalid social credential) |
| 15 | apache/airflow | [38211705554](https://github.com/apache/airflow/actions/runs/38211705554) | workflow-status (v3-4-test, ci-amd.yml) (8) | 2026-10-12 | L3 | pr_fix | M | "Invalid input! Failed to parse contents of the provided payload" (notification step payload) |
| 16 | astral-sh/ruff | [38246412614](https://github.com/astral-sh/ruff/actions/runs/38246412614) | build (5) | 2026-10-12 | L4 | rerun | M | "Unexpected HTTP response: 504" |
| 17 | pypa/pip | [38203075276](https://github.com/pypa/pip/actions/runs/38203075276) | Check PR template (3) | 2026-10-12 | escalate (nearest L3) | escalate | H | "Could not find the pip PR checklist in the pull request body." (needs the author to edit the PR) |
| 18 | facebook/docusaurus | [37991289257](https://github.com/facebook/docusaurus/actions/runs/37991289257) | Lint (7) | 2026-10-09 | L4 | pr_fix | H | "Property 'value' does not exist on type 'ObjectLiteralElement'." (type errors after a typescript-eslint bump) |
| 19 | hashicorp/terraform-provider-azurerm | [38307501830](https://github.com/hashicorp/terraform-provider-azurerm/actions/runs/38307501830) | add-waiting-response-on-fail (2) | 2026-10-12 | L3 | pr_fix | L | "no artifacts found" (workflow_run follow-up workflow expects an artifact) |
| 20 | tailwindlabs/tailwindcss | [38272664961](https://github.com/tailwindlabs/tailwindcss/actions/runs/38272664961) | Build x86_64-unknown-freebsd (OXIDE) (3) | 2026-10-12 | L1 | rerun | L | "The process '/usr/bin/ssh' failed with exit code 1" (FreeBSD VM build job; the run's other jobs passed; earlier Release runs also failed) |
| 21 | hashicorp/terraform | [38285481399](https://github.com/hashicorp/terraform/actions/runs/38285481399) | Check Changelog Entry (4) | 2026-10-12 | escalate (nearest L3) | escalate | H | "Please add a changelog entry for in the .changes/v1.18 folder," (author or reviewer decision) |
| 22 | mastodon/mastodon | [38111497622](https://github.com/mastodon/mastodon/actions/runs/38111497622) | check-i18n (3) | 2026-10-11 | L4 | pr_fix | M | "The process '/opt/hostedtoolcache/Ruby/4.0.7/x64/bin/bundle' failed with exit code 16" (renovate gem bump; bundle step; likely lockfile or dependency resolution) |
| 23 | storybookjs/storybook | [38294365265](https://github.com/storybookjs/storybook/actions/runs/38294365265) | trigger-circle-ci-workflow (2) | 2026-10-12 | L2 | escalate | L | "Process completed with exit code 22." (curl style HTTP 4xx from CircleCI trigger on pull_request_target; token or permission likely) |
| 24 | goreleaser/goreleaser | [38289756200](https://github.com/goreleaser/goreleaser/actions/runs/38289756200) | scan-source (3) | 2026-10-12 | L4 | pr_fix | M | "Failed minimum severity level. Found vulnerabilities with level 'critical' or higher" (could also be escalate if no fixed version exists) |
| 25 | flutter/flutter | [38209747393](https://github.com/flutter/flutter/actions/runs/38209747393) | noResponse (3) | 2026-10-12 | L4 | rerun | M | "Unhandled error: HttpError" (scheduled stale/no-response bot) |
| 26 | python-poetry/poetry | [38057432062](https://github.com/python-poetry/poetry/actions/runs/38057432062) | Ubuntu (Python 3.11) / pytest (5) | 2026-10-10 | L4 | escalate | M | "test_extractall_sdist_no_symlink_path_traversal_via_directory_symlink Failed: DID NOT RAISE OutsideDestinationError" (real test failure on a feature PR). Several jobs failed, so `job` is set; the exact job name is copied from the annotation, verify it |
| 27 | kubernetes-sigs/kustomize | [38145310097](https://github.com/kubernetes-sigs/kustomize/actions/runs/38145310097) | check-synced-go-modules (5) | 2026-10-11 | L4 | pr_fix | L | "Process completed with exit code 1." (dependabot k8s.io/cli-runtime bump; module sync check) |
| 28 | python/cpython | [38326366941](https://github.com/python/cpython/actions/runs/38326366941) | Unresolved review (4) | 2026-10-12 | escalate (nearest L3) | escalate | H | "This PR is labelled type-feature. All features require approval from a core team member before merge." |
| 29 | prisma/prisma | [38200033863](https://github.com/prisma/prisma/actions/runs/38200033863) | Init Journey (nightly, main) | 2026-10-12 | L4 | pr_fix | M | "The requested module '@prisma/cli-engine' does not provide an export named 'configSchema'" (scheduled run on main) |
| 30 | dotnet/runtime | [38319956846](https://github.com/dotnet/runtime/actions/runs/38319956846) | check-labels (2) | 2026-10-12 | escalate (nearest L3) | escalate | H | "'Servicing-approved' label not applied to the PR yet." |

## Spares (10, ranked: use in this order to replace a primary that the fetcher refuses)

| S | Repo | Run id | Job (step) | Completed | Exp. layer | Exp. action | Conf | Basis (annotation, verbatim) |
|---|------|--------|------------|-----------|-----------|-------------|------|------|
| S1 | prometheus/prometheus | [38274943053](https://github.com/prometheus/prometheus/actions/runs/38274943053) | Run govulncheck (3) | 2026-10-12 | L4 | pr_fix | H | "notifier.Manager.Stop calls sync.Once.Do, which eventually calls http2.Framer.WriteGoAway" |
| S2 | grafana/loki | [38325719152](https://github.com/grafana/loki/actions/runs/38325719152) | govulncheck (6) | 2026-10-13 | L4 | pr_fix | H | "querytee.Start calls http.Server.Serve, which eventually calls http2.Server.ServeConn" |
| S3 | fastapi/fastapi | [38028455507](https://github.com/fastapi/fastapi/actions/runs/38028455507) | smokeshow (7) | 2026-10-10 | L3 | rerun | L | "Unable to download artifact(s): Artifact not found for name: coverage-html" (artifact missing or expired; see also pydantic/pydantic 38144202658 "no matching workflow run found with any artifacts?") |
| S4 | moby/buildkit | [38192319899](https://github.com/moby/buildkit/actions/runs/38192319899) | test-freebsd-amd64 (6) | 2026-10-12 | L1 | rerun | L | "The action 'Smoke test' has timed out after 1 minutes." (scheduled run 38130889386 failed identically, so possibly persistent) |
| S5 | prometheus/prometheus | [38243014908](https://github.com/prometheus/prometheus/actions/runs/38243014908) | Go tests with previous Go version (1.26) | 2026-10-12 | L1 | rerun | H | "The hosted runner lost communication with the server." (job-level annotation; the log may have no ##[error] line so the fetcher may refuse it) |
| S6 | gohugoio/hugo | [38045424086](https://github.com/gohugoio/hugo/actions/runs/38045424086) | build (6) | 2026-10-10 | L4 | escalate | L | "buildx failed with: ERROR: failed to build: failed to solve: process" ... "did not complete successfully: exit code: 1" (PR build error, likely a real code bug) |
| S7 | grafana/grafana | [38328790314](https://github.com/grafana/grafana/actions/runs/38328790314) | matrix job, e.g. "@grafana/dashboards-squad" (7 and 11) | 2026-10-13 | L3 | pr_fix | L | "The template is not valid. .github/workflows/report-frontend-coverage-to-bench.yaml (Line: 106, Col: 27)" (4 matrix jobs failed, so `job` is needed; two failed steps, the fetcher may refuse) |
| S8 | jenkinsci/jenkins | [38190824729](https://github.com/jenkinsci/jenkins/actions/runs/38190824729) | label (2) | 2026-10-12 | escalate (nearest L3) | escalate | H | "Missing required label for changelog." |
| S9 | n8n-io/n8n | [38273358519](https://github.com/n8n-io/n8n/actions/runs/38273358519) | check-pr-title (2) | 2026-10-12 | escalate (nearest L3) | escalate | H | "First char of subject must be uppercase" |
| S10 | cli/cli | [37862519786](https://github.com/cli/cli/actions/runs/37862519786) | govulncheck (5) | 2026-10-09 | L4 | pr_fix | M | "Unable to upload "gh.sarif" as it is not valid SARIF:" (failed step is an upload action, not a run: step) |

## Quota tally (primary 30 only, by my expected layer)

| Quota | Required | Got | Detail |
|---|---|---|---|
| L1 | >= 4 | 4 | #3, #8, #12, #20 |
| L2 | >= 4 | 4 | #2 (fork PR caveat), #7, #14 (low conf.), #23 (low conf.) |
| L3 | >= 4 | 4 pure | #5, #10, #15, #19; plus 6 policy or approval denials tagged "escalate (nearest L3)": #6, #11, #17, #21, #28, #30 |
| L4 | >= 4 | 12 | #1, #4, #9, #13, #16, #18, #22, #24, #25, #26, #27, #29 |
| escalate action | >= 3 | 11 | #2, #6, #7, #11, #14, #17, #21, #23, #26, #28, #30 |

By action: rerun 6 (#3, #8, #12, #16, #20, #25), pr_fix 13 (#1, #4, #5, #9, #10, #13, #15, #18, #19, #22, #24, #27, #29), escalate 11. (Target was 6 / 14 / 10.)

Repos: 28 distinct, at most 2 per repo (mastodon/mastodon: #3, #22; facebook/docusaurus: #6, #18).
Ecosystems: Go (cli, helm, argo-cd, goreleaser, kustomize), Python (pydantic, transformers, poetry, pip, cpython, airflow), JS/TS (grafana, docusaurus, supabase, electron, tailwind, storybook, prisma, node), Ruby (mastodon x2, rails), Rust (ruff), Terraform and HCL projects (terraform, provider-aws, provider-azurerm), C# (dotnet/runtime), Dart (flutter), LLM tooling (langchain).

Not met or weak:
- Java: no Java run was confirmed (spring-boot, okhttp, quarkus, camel failures only showed `Process completed with exit code 1.` with no distinguishing message). Docker/buildx appears only as spare S6 and in #13.
- L2 is the weakest quota: #7 is explicit; #2 is a fork-PR secrets case; #14 and #23 are low confidence guesses from job names and exit codes.
- L1 has explicit messages for #3, #8 and (spare) S5 only; #12 and #20 are medium or low.
- Several expected actions are ambiguous by nature (#12 rerun vs pr_fix, #24 pr_fix vs escalate, #27 and #14 low). The human should decide from the logs.
- No run is a Terraform/OpenTofu plan or apply failure; the Terraform repos contribute policy gates and bot workflows.
