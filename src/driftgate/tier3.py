"""Tier 3 (code change) support: deterministic diff checks, the pull request record, and the reviewer hook.

Everything here is deterministic code that sits between the agent's proposed diff and the pull request:

- `check_diff` is a gate-like validator. It can only produce violations, and a violation escalates; it never
  raises a tier and never edits the diff. It checks, in order, that the diff parses, only names repository-relative
  paths, never touches secrets/credentials/kill-switch configuration, only touches files that were declared in the
  proposal and are plausible for the proposed action, stays under a size cap, applies cleanly to the files as they
  are at the failing ref, adds no secret-looking literal, does not delete files, and is additive before
  subtractive when it spans several files (invariant 13).
- `PullRequest` / `PullRequestRecord` describe the artifact a target opens. There is no merge operation anywhere
  (invariant 4); branches are prefixed `driftgate/`.
- `ReviewerHook` runs the independent reviewer agent for the orchestrator's `tier3_review` extension point.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Protocol, runtime_checkable

from driftgate.agents.investigator import Investigation
from driftgate.agents.reviewer import Reviewer, ReviewRequest, ReviewResult
from driftgate.diffs import DiffError, StructuredDiff, apply_diff, parse_diff
from driftgate.domain import Evidence, ExecutionSource, Remediation, Review, SourceError
from driftgate.llm.budget import BudgetView, InvestigationBudget
from driftgate.llm.config import InvestigationLimits
from driftgate.llm.types import ModelClient
from driftgate.redaction import redact
from driftgate.tools import ToolContext

BRANCH_PREFIX = "driftgate/"
PR_BASE_BRANCH = "main"
MAX_BASE_BYTES = 200_000

LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock")
WORKFLOWS = (".github/workflows/*.yml", ".github/workflows/*.yaml")

#: Files an action may plausibly change (patterns without "/" match the file name in any directory).
ACTION_PATHS: dict[str, tuple[str, ...]] = {
    "regenerate_lockfile": (*LOCKFILES, "package.json", "requirements*.txt", "pyproject.toml", ".terraform.lock.hcl"),
    "pin_provider_version": ("*.tf", ".terraform.lock.hcl"),
    "revert_template_bump": WORKFLOWS,
    "fix_undefined_variable": ("*.tf", "*.tfvars", *WORKFLOWS),
    "fix_secret_scope": WORKFLOWS,
    "fix_requirement_pin": ("requirements*.txt", "constraints*.txt", "pyproject.toml"),
}

#: Never changeable by an agent whatever the action: secrets, credentials, kill-switch config, VCS and tool state.
PROTECTED_PATTERNS: tuple[str, ...] = (
    r"(^|/)\.env($|\.)",
    r"(^|/)[^/]*\.(pem|key|p12|pfx|jks|tfstate)$",
    r"(^|/)id_(rsa|dsa|ecdsa|ed25519)",
    r"(^|/)credentials?($|[._/-])",
    r"(^|/)secrets?(/|$)",
    r"(^|/)kill[_-]?switch",
    r"(^|/)\.git(/|$)",
    r"(^|/)\.driftgate(/|$)",
    r"(^|/)\.claude(/|$)",
)

SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("aws access key id", r"\bAKIA[0-9A-Z]{16}\b"),
    ("github token", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    ("anthropic or provider api key", r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    ("private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    (
        "hard-coded credential",
        r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key)\b\s*[:=]\s*[\"']?(?!\$\{|\$\(|var\.|secrets\.)[A-Za-z0-9/+_.-]{12,}",
    ),
)


@dataclass(frozen=True)
class Tier3Limits:
    max_files: int = 3
    max_changed_lines: int = 60  # added + removed


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    path: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass
class DiffCheck:
    violations: list[Violation] = field(default_factory=list)
    diff: StructuredDiff | None = None
    new_contents: dict[str, str | None] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def reason(self) -> str:
        return "; ".join(str(v) for v in self.violations)

    @property
    def codes(self) -> list[str]:
        return [v.code for v in self.violations]


def _unsafe_path(path: str) -> bool:
    p = PurePosixPath(path.replace("\\", "/"))
    drive = len(path) > 1 and path[1] == ":" and path[0].isalpha()
    return not path or p.is_absolute() or ".." in p.parts or drive or "\\" in path


def is_protected(path: str) -> bool:
    low = path.replace("\\", "/").lower()
    return any(re.search(p, low) for p in PROTECTED_PATTERNS)


def path_allowed_for(action: str, path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatchcase(path if "/" in pat else name, pat) for pat in ACTION_PATHS.get(action, ()))


def find_secret_literals(added_lines: Iterable[str]) -> list[str]:
    """Names of the secret patterns that match, never the matching text."""
    hits: list[str] = []
    for line in added_lines:
        for name, pattern in SECRET_PATTERNS:
            if re.search(pattern, line) and name not in hits:
                hits.append(name)
    return hits


def check_diff(
    diff_text: str,
    *,
    action: str,
    declared_paths: Iterable[str],
    source: ExecutionSource,
    repo: str,
    ref: str,
    limits: Tier3Limits | None = None,
) -> DiffCheck:
    """Run every deterministic pre-check on a proposed diff. Never raises on bad input; never edits the diff."""
    lim = limits or Tier3Limits()
    out = DiffCheck()
    declared = set(declared_paths)
    if not diff_text or not diff_text.strip():
        out.violations.append(Violation("no_diff", "a Tier 3 proposal must carry a unified diff"))
        return out
    try:
        diff = parse_diff(diff_text)
    except DiffError as e:
        out.violations.append(Violation("parse_error", str(e)))
        return out
    out.diff = diff

    def bad(code: str, message: str, path: str = "") -> None:
        out.violations.append(Violation(code, message, path))

    for fd in diff.files:
        if fd.is_rename:
            bad("rename", f"renaming {fd.old_path} to {fd.new_path} is not allowed", fd.path)
        if fd.is_deleted:
            bad("deletes_file", f"deleting {fd.path} is not allowed; fix the file instead", fd.path)
        for p in {fd.old_path, fd.new_path} - {None}:
            assert p is not None
            if _unsafe_path(p):
                bad("unsafe_path", f"{p!r} is not a repository-relative path", p)
            elif is_protected(p):
                bad("protected_path", f"{p} holds secrets, credentials or guard configuration", p)
    paths = [fd.path for fd in diff.files]
    for p in paths:
        if p in declared or _unsafe_path(p):
            pass
        else:
            bad("undeclared_path", f"{p} was not among the files the proposal declared", p)
        if not _unsafe_path(p) and not is_protected(p) and not path_allowed_for(action, p):
            bad("unrelated_path", f"{p} is not a file the {action} action changes", p)

    if len(diff.files) > lim.max_files:
        bad("too_many_files", f"{len(diff.files)} files changed; the limit is {lim.max_files}")
    changed = diff.added + diff.removed
    if changed > lim.max_changed_lines:
        bad("too_large", f"{changed} changed lines; the limit is {lim.max_changed_lines}")

    added = [line[1:] for fd in diff.files for h in fd.hunks for line in h.lines if line.startswith("+")]
    for name in find_secret_literals(added):
        bad("secret_literal", f"an added line looks like a credential ({name})")

    # Additive before subtractive (invariant 13): a file that shrinks may not precede one that grows or stays level.
    shrinking: str | None = None
    for fd in diff.files:
        if fd.net < 0:
            shrinking = shrinking or fd.path
        elif shrinking is not None and not fd.is_deleted:
            bad("subtractive_before_additive", f"{shrinking} removes lines before {fd.path} adds its change", fd.path)
            break

    if any(v.code in ("unsafe_path", "protected_path", "rename") for v in out.violations):
        return out  # do not read files we have just decided are off limits

    def read(path: str) -> str | None:
        try:
            f = source.read_file(repo, path, ref, MAX_BASE_BYTES)
        except SourceError:
            return None
        if f.truncated:
            raise DiffError(f"{path} is too large to verify")
        return f.content

    try:
        out.new_contents = apply_diff(diff, read)
    except DiffError as e:
        bad("does_not_apply", str(e))
        return out
    for p, new in out.new_contents.items():
        old = read(p)
        if new is not None and new == old:
            bad("no_op", f"the diff leaves {p} unchanged", p)
    return out


# -- pull request ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PullRequest:
    branch: str
    base: str
    title: str
    body: str
    diff_text: str
    paths: tuple[str, ...]
    #: The CHECKED new file contents (path -> text, in the diff's additive-first order), produced by `check_diff`.
    #: A real target commits exactly these; it never re-parses `diff_text`, which is the model's raw text.
    files: tuple[tuple[str, str], ...] = ()
    base_sha: str = ""  # the failing commit the branch is created from ("" = tip of `base`)


@dataclass(frozen=True)
class PullRequestRecord:
    number: int
    branch: str
    url: str
    merged: bool = False  # always False: nothing in DriftGate can merge
    simulated: bool = True  # False only for a pull request that really exists on a provider


def redact_pull_request(pr: PullRequest) -> PullRequest:
    """The single choke point for outgoing Tier 3 free text: the title and body only.

    The diff and file contents are deliberately NOT redacted: redaction is lossy on code (`token: ${{ secrets.X }}`),
    and the committed files must equal what `check_diff` verified and the reviewer approved. Code is verified, not
    rewritten: `check_diff` rejects any added line that looks like a credential before a PR is ever built.
    The commit message is derived from a validated path by the target, so it carries no free text.
    """
    return replace(pr, title=redact(pr.title), body=redact(pr.body))


@runtime_checkable
class PullRequestTarget(Protocol):
    """A target that can open a pull request. It has no merge operation by design."""

    def open_pull_request(self, pr: PullRequest) -> PullRequestRecord: ...


def build_pr_description(
    *,
    execution_id: str,
    fingerprint: str,
    action: str,
    hypothesis: str,
    rationale: str,
    evidence: Iterable[Evidence],
    diff: StructuredDiff,
    review: Review | None,
    revision_rounds: int,
    branch: str,
) -> str:
    lines = [
        f"## DriftGate proposed fix: {action}",
        "",
        f"Failed execution: `{execution_id}`  |  fingerprint `{fingerprint}`  |  branch `{branch}`",
        "",
        "This pull request was opened by an automated agent. It has not been merged and will not be; "
        "a human reviews and merges it.",
        "",
        "### Hypothesis",
        hypothesis,
        "",
        "### Proposed change",
        rationale,
        "",
    ]
    for f in diff.files:
        lines.append(f"- `{f.path}`: +{f.added} -{f.removed}")
    lines += ["", "### Evidence bundle"]
    ev = list(evidence)
    lines += [f"- `{e.source}`: {e.finding} (supports: {e.supports})" for e in ev] or ["- none"]
    lines += ["", "### Independent review"]
    if review is None:
        lines.append("No review was recorded.")
    else:
        rounds = f" after {revision_rounds} revision round" if revision_rounds else ""
        lines += [f"Verdict: **{review.verdict}**{rounds}", "", review.comments or "(no comments)"]
    return "\n".join(lines) + "\n"


# -- reviewer hook --------------------------------------------------------------------------------------------
#: The reviewer's own loop limit; it also draws on the case's shared budget.
REVIEW_MAX_TOOL_CALLS = 3
ReviewerModelFactory = Callable[[str, InvestigationBudget], ModelClient]


class ReviewerHook:
    """The orchestrator's `tier3_review`: builds the reviewer's restricted view of a proposal and runs the reviewer.

    The reviewer sees the diff, the target files as they are at the failing ref, and the hypothesis. The
    investigator's tool results, evidence, reasoning and confidence are never passed in.
    """

    def __init__(
        self,
        source: ExecutionSource,
        model_factory: ReviewerModelFactory,
        *,
        limits: InvestigationLimits | None = None,
        model: str = "",
    ) -> None:
        self._source = source
        self._factory = model_factory
        self._limits = limits or InvestigationLimits(max_tool_calls=REVIEW_MAX_TOOL_CALLS)
        self._model = model
        self._round = 0
        self.results: list[ReviewResult] = []

    def __call__(self, inv: Investigation, rem: Remediation) -> Review:
        report, dry_run = inv.report, rem.dry_run
        eid = report.execution_id
        ex = self._source.get_execution(eid)
        repo, ref = ex.pipeline, ex.refs.get("commit", "main")
        diff_text = str(dry_run.get("diff_text", ""))
        files: dict[str, str] = {}
        for p in dry_run.get("paths", []):
            if _unsafe_path(p) or is_protected(p):  # never read off-limits files into the reviewer prompt
                continue
            try:
                files[p] = self._source.read_file(repo, p, ref, MAX_BASE_BYTES).content
            except SourceError:
                continue
        # The reviewer has its own small allowance, charged to the case's shared budget (investigator, revision,
        # reviewer and any re-investigation together stay inside the per-investigation caps).
        budget = BudgetView(self._limits, parent=inv.budget)
        reviewer = Reviewer(self._factory(eid, budget), ToolContext(self._source), budget, model=self._model)
        result = reviewer.review(ReviewRequest(repo, ref, report.hypothesis, diff_text, files, min(self._round, 1)))
        self._round += 1
        self.results.append(result)
        return result.review

    def reset_round(self) -> None:
        """Called by the orchestrator at the start of a case's first Tier 3 review (no revision yet)."""
        self._round = 0
