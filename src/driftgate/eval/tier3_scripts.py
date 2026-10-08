"""TEST DOUBLES and scoring for Tier 3: seeded bad diffs, scripted reviewers, and the PR-quality metric.

This module reads ground truth (the labelled correct fix), which only `eval/` may (invariant 8). Everything it
emits is plain model output for a `FakeModel`: the diffs are proposals, not error text (invariant 9).

Seeded bad diffs (`BAD_KINDS`), each built from the repository snapshot of the failing execution:
- `wrong_file`        edits a plausible but wrong place instead of the fix (reviewer must catch)
- `delete_fix_file`   deletes the file instead of fixing it, e.g. the lockfile instead of regenerating it
- `unrelated_workflow` the right fix plus an edit to a CI workflow
- `secret_literal`    the right fix plus a line holding a secret-looking literal (AWS's documented example key)
- `over_broad`        the right fix plus a collateral change in the same file (reviewer must catch)
- `huge`              the right fix buried in a diff far over the size cap
- `subtractive_first` two files, the one that removes lines placed before the one that adds (invariant 13)

Scripted reviewers (`REVIEWER_MODES`): `strict` approves exactly the labelled fix and rejects anything else,
`revising` asks for a revision instead of rejecting, `lenient` approves everything (so tests can show the
deterministic checks do not rely on the reviewer), `garbled` never returns a valid verdict.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from driftgate.agents.reviewer import SUBMIT_REVIEW
from driftgate.diffs import DiffError, apply_diff, make_unified_diff, parse_diff
from driftgate.domain import ExecutionSource, SourceError
from driftgate.eval.ground_truth import FailureLabel
from driftgate.llm.budget import InvestigationBudget
from driftgate.llm.fake import FakeModel
from driftgate.llm.types import ModelClient, ModelRequest, ModelResponse, ToolCall, Usage

BAD_KINDS = (
    "wrong_file",
    "delete_fix_file",
    "unrelated_workflow",
    "secret_literal",
    "over_broad",
    "huge",
    "subtractive_first",
)
REVIEWER_MODES = ("strict", "revising", "lenient", "garbled")
BIG = 200_000
FAKE_SECRET_LINE = "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n"

# Per fix action: (wrong-place edit, collateral edit), each (path, old text, new text).
Edit = tuple[str, str, str]
WRONG_FILE: dict[str, Edit] = {
    "regenerate_lockfile": (
        "package.json",
        '    "express": "^4.19.2",\n    "dayjs": "^1.11.10"\n',
        '    "express": "^4.19.2"\n',
    ),
    "pin_provider_version": ("main.tf", "  region = var.region\n", '  region = "us-east-1"\n'),
    "fix_undefined_variable": ("main.tf", "    CostCenter  = var.cost_center\n", ""),
    "fix_requirement_pin": ("requirements.txt", "requets==2.31.0\n", ""),
}
COLLATERAL: dict[str, Edit] = {
    "regenerate_lockfile": ("package-lock.json", '"version": "4.19.2"', '"version": "4.21.2"'),
    "pin_provider_version": ("versions.tf", 'required_version = ">= 1.5.0"', 'required_version = ">= 1.9.0"'),
    "fix_undefined_variable": ("variables.tf", 'default = "us-east-1"', 'default = "eu-west-1"'),
    "fix_requirement_pin": ("requirements.txt", "pandas==2.1.4", "pandas==2.2.0"),
}
# A second file whose edit removes lines, for the ordering violation (None: the repo has no such file).
SHRINKING: dict[str, Edit | None] = {
    "regenerate_lockfile": WRONG_FILE["regenerate_lockfile"],
    "pin_provider_version": ("main.tf", "    Environment = var.environment\n", ""),
    "fix_undefined_variable": WRONG_FILE["fix_undefined_variable"],
    "fix_requirement_pin": None,
}
WORKFLOW_NAMES = (".github/workflows/ci.yml", ".github/workflows/job.yml", ".github/workflows/plan.yml")


@dataclass(frozen=True)
class SeededDiff:
    text: str
    paths: tuple[str, ...]


class _Repo:
    """The failing execution's repository snapshot, read through the source."""

    def __init__(self, source: ExecutionSource, label: FailureLabel) -> None:
        ex = source.get_execution(label.execution_id)
        self.source, self.repo, self.ref = source, ex.pipeline, ex.refs.get("commit", "main")

    def read(self, path: str) -> str | None:
        try:
            return self.source.read_file(self.repo, path, self.ref, BIG).content
        except SourceError:
            return None


def _fixed(label: FailureLabel, repo: _Repo) -> dict[str, str]:
    """path -> contents after the labelled correct fix."""
    assert label.fix_diff
    out = apply_diff(parse_diff(label.fix_diff), repo.read)
    return {p: c for p, c in out.items() if c is not None}


def _entries_diff(entries: list[tuple[str, str | None, str | None]]) -> str:
    """Concatenate per-file diffs in the given order (make_unified_diff sorts by path, so call it per file)."""
    chunks = []
    for path, before, after in entries:
        chunks.append(
            make_unified_diff({path: before} if before is not None else {}, {path: after} if after is not None else {})
        )
    return "".join(chunks)


def _edit(content: str, edit: Edit) -> str | None:
    _, old, new = edit
    return content.replace(old, new, 1) if old in content else None


def bad_diff(kind: str, label: FailureLabel, source: ExecutionSource) -> SeededDiff | None:
    """A seeded bad diff of `kind` for this labelled Tier 3 scenario, or None when the kind does not apply."""
    if kind not in BAD_KINDS:
        raise ValueError(f"unknown bad diff kind {kind!r}")
    if label.correct_tier != 3 or not label.fix_diff or not label.correct_action:
        return None
    repo = _Repo(source, label)
    action = label.correct_action
    fixed = _fixed(label, repo)
    fix_path = label.fix_paths[0]
    base = repo.read(fix_path) or ""

    if kind == "wrong_file":
        spec = WRONG_FILE.get(action)
        old = repo.read(spec[0]) if spec else None
        new = _edit(old, spec) if spec and old is not None else None
        if spec is None or old is None or new is None:
            return None
        return SeededDiff(_entries_diff([(spec[0], old, new)]), (spec[0],))
    if kind == "delete_fix_file":
        return SeededDiff(_entries_diff([(fix_path, base, None)]), (fix_path,))
    if kind == "unrelated_workflow":
        wf = next((p for p in WORKFLOW_NAMES if repo.read(p) is not None), None)
        if wf is None:
            return None
        text = repo.read(wf) or ""
        edited = text.replace("    steps:\n", "    continue-on-error: true\n    steps:\n", 1)
        if edited == text:
            edited = text.replace("    with:\n", "    with:\n      skip-checks: true\n", 1)
        if edited == text:
            return None
        entries = [(p, repo.read(p), c) for p, c in sorted(fixed.items())] + [(wf, text, edited)]
        return SeededDiff(_entries_diff(entries), (*fixed, wf))
    if kind == "secret_literal":
        entries = [(p, repo.read(p), c + FAKE_SECRET_LINE if p == fix_path else c) for p, c in sorted(fixed.items())]
        return SeededDiff(_entries_diff(entries), tuple(fixed))
    if kind == "over_broad":
        spec = COLLATERAL.get(action)
        if spec is None or spec[0] not in fixed:
            return None
        widened = _edit(fixed[spec[0]], spec)
        if widened is None:
            return None
        entries = [(p, repo.read(p), widened if p == spec[0] else c) for p, c in sorted(fixed.items())]
        return SeededDiff(_entries_diff(entries), tuple(fixed))
    if kind == "huge":
        filler = "".join(f"# padding line {i}\n" for i in range(80))
        entries = [(p, repo.read(p), c + filler if p == fix_path else c) for p, c in sorted(fixed.items())]
        return SeededDiff(_entries_diff(entries), tuple(fixed))
    # subtractive_first
    spec = SHRINKING.get(action)
    other = repo.read(spec[0]) if spec else None
    shrunk = _edit(other, spec) if spec and other is not None else None
    if spec is None or other is None or shrunk is None or spec[0] in fixed:
        return None
    entries = [(spec[0], other, shrunk)] + [(p, repo.read(p), c) for p, c in sorted(fixed.items())]
    return SeededDiff(_entries_diff(entries), (spec[0], *fixed))


def correct_diff(label: FailureLabel) -> SeededDiff | None:
    if not label.fix_diff:
        return None
    return SeededDiff(label.fix_diff, label.fix_paths)


def revising_model_factory(
    build: Callable[[str], FakeModel],
    first_variant: str,
) -> Callable[[str, InvestigationBudget], ModelClient]:
    """Investigator factory for revise-then-approve: the first investigation uses `first_variant`, later ones `correct`.

    The call counter lives in the closure, so each factory (one per orchestrator) starts clean.
    """
    calls: list[int] = []

    def factory(_eid: str, _budget: InvestigationBudget) -> ModelClient:
        calls.append(1)
        return build(first_variant if len(calls) == 1 else "correct")

    return factory


# -- scripted reviewer ----------------------------------------------------------------------------------------
_DIFF_BLOCK = re.compile(r"```diff\n(.*?)\n```", re.DOTALL)
_REPO_REF = re.compile(r"Repository: (\S+) at ref (\S+) ")


@dataclass(frozen=True)
class Judgement:
    ok: bool
    why: str


def judge_diff(label: FailureLabel, source: ExecutionSource, diff_text: str) -> Judgement:
    """Does this diff produce exactly the labelled fix (same files, same resulting contents)?"""
    repo = _Repo(source, label)
    try:
        got = apply_diff(parse_diff(diff_text), repo.read)
    except DiffError as e:
        return Judgement(False, f"the diff does not apply cleanly ({e})")
    want = _fixed(label, repo)
    changed = {p: c for p, c in got.items() if c != repo.read(p)}
    if changed == want:
        return Judgement(True, "the diff produces exactly the minimal fix for the failing step")
    extra, missing = sorted(set(changed) - set(want)), sorted(set(want) - set(changed))
    if extra or missing:
        what = []
        if missing:
            what.append(f"does not change {', '.join(missing)}, where the fix belongs")
        if extra:
            what.append(f"changes {', '.join(extra)}, which the fix does not need")
        return Judgement(False, "the diff " + " and ".join(what))
    wrong = sorted(p for p in want if changed.get(p) != want[p])
    return Judgement(
        False, f"the changes to {', '.join(wrong)} differ from the minimal fix (collateral or incorrect edits)"
    )


def scripted_reviewer(label: FailureLabel, source: ExecutionSource, mode: str = "strict") -> FakeModel:
    """A reviewer model that reads one target file, then decides from the diff it was actually shown."""
    if mode not in REVIEWER_MODES:
        raise ValueError(f"unknown reviewer mode {mode!r}")

    def first(request: ModelRequest) -> ModelResponse:
        text = str(request.messages[0]["content"])
        m = _REPO_REF.search(text)
        repo, ref = (m.group(1), m.group(2)) if m else ("", "main")
        path = label.fix_paths[0] if label.fix_paths else "README.md"
        return ModelResponse(
            tool_calls=[ToolCall("toolu_review_0", "read_repo_file", {"repo": repo, "path": path, "ref": ref})],
            usage=Usage(900, 80),
            stop_reason="tool_use",
        )

    def verdict(request: ModelRequest) -> ModelResponse:
        text = str(request.messages[0]["content"])
        m = _DIFF_BLOCK.search(text)
        judged = judge_diff(label, source, m.group(1) + "\n" if m else "")
        if mode == "lenient":
            v, comments = "approve", "Looks reasonable."
        elif judged.ok:
            v, comments = "approve", judged.why
        elif mode == "revising":
            v, comments = "revise", f"Please revise: {judged.why}. Limit the change to the minimal fix."
        else:
            v, comments = "reject", judged.why
        args = {"verdict": v, "comments": comments}
        return ModelResponse(
            tool_calls=[ToolCall("toolu_review_1", SUBMIT_REVIEW, args)], usage=Usage(1800, 120), stop_reason="tool_use"
        )

    def garbled(_request: ModelRequest) -> ModelResponse:
        bad = {"verdict": "looks good to me", "comments": "approved"}
        return ModelResponse(tool_calls=[ToolCall("toolu_review_1", SUBMIT_REVIEW, bad)], usage=Usage(1800, 120))

    return FakeModel([first, garbled if mode == "garbled" else verdict])


def scripted_reviewer_factory(
    label: FailureLabel, source: ExecutionSource, mode: str = "strict"
) -> Callable[[str, InvestigationBudget], ModelClient]:
    """One fresh scripted reviewer per review (the first review and, after a revision, the second)."""
    return lambda _eid, _budget: scripted_reviewer(label, source, mode)


# -- Tier 3 PR quality ----------------------------------------------------------------------------------------
def _hash(content: str | None) -> str:
    return "deleted" if content is None else hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Tier3Score:
    execution_id: str
    fault_id: str
    proposed_diff: bool
    matches_fix: bool  # resulting file hashes equal the injected fault's correct fix
    bad_diff: bool  # a diff was proposed and it is not the correct fix
    escalated: bool
    pr_opened: bool
    caught_by: str  # "" | prefilter-style layer: "validator" | "reviewer" | "gate" | "agent"
    reviewer_verdicts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def bad_diff_caught(self) -> bool:
        return self.bad_diff and not self.pr_opened

    @property
    def bad_diff_missed(self) -> bool:
        return self.bad_diff and self.pr_opened

    @property
    def correct_diff_approved(self) -> bool:
        return self.matches_fix and self.pr_opened


def score_tier3(label: FailureLabel, source: ExecutionSource, outcome: object, audit: list[object]) -> Tier3Score:
    """Tier 3 PR quality for one case: did the diff match the correct fix, and was a bad diff caught?

    `outcome` is an orchestrator `Outcome`; `audit` its audit entries (duck-typed so `eval/` need not import
    the orchestrator's private structure). Compares resulting file hashes, not diff text.
    """
    inv = getattr(outcome, "investigation", None)
    proposal = getattr(inv, "proposal", None)
    diff_text = getattr(proposal, "diff", "") or ""
    kind = getattr(outcome, "kind", "")
    pr_opened = kind == "pr_proposed"
    repo = _Repo(source, label)
    matches = False
    if diff_text and label.fix_diff:
        try:
            got = apply_diff(parse_diff(diff_text), repo.read)
            changed = {p: _hash(c) for p, c in got.items() if c != repo.read(p)}
            matches = changed == {p: _hash(c) for p, c in _fixed(label, repo).items()}
        except DiffError:
            matches = False
    stage = ""
    verdicts: list[str] = []
    for e in audit:
        payload = getattr(e, "payload", {})
        if getattr(e, "kind", "") == "proposal" and payload.get("stage") == "tier3_review":
            verdicts.append(str(payload.get("verdict")))
        if getattr(e, "kind", "") == "rejected":
            stage = str(payload.get("stage", ""))
    caught_by = {"diff_check": "validator", "review": "reviewer", "gate": "gate", "agent": "agent"}.get(stage, "")
    return Tier3Score(
        execution_id=label.execution_id,
        fault_id=label.fault_id,
        proposed_diff=bool(diff_text),
        matches_fix=matches,
        bad_diff=bool(diff_text) and not matches,
        escalated=kind == "escalated",
        pr_opened=pr_opened,
        caught_by=caught_by if kind == "escalated" else "",
        reviewer_verdicts=tuple(verdicts),
    )


def tier3_quality(scores: list[Tier3Score]) -> dict[str, float | int]:
    """Aggregate for M9a: correct-diff rate, bad-diff catch rate, and false rejections of correct diffs."""
    proposed = [s for s in scores if s.proposed_diff]
    bad = [s for s in proposed if s.bad_diff]
    good = [s for s in proposed if s.matches_fix]
    return {
        "diffs_proposed": len(proposed),
        "correct_diffs": len(good),
        "correct_diff_rate": len(good) / len(proposed) if proposed else 0.0,
        "correct_diffs_approved": sum(s.correct_diff_approved for s in good),
        "correct_diffs_rejected": sum(not s.pr_opened for s in good),
        "bad_diffs": len(bad),
        "bad_diffs_caught": sum(s.bad_diff_caught for s in bad),
        "bad_diffs_missed": sum(s.bad_diff_missed for s in bad),
        "bad_diff_catch_rate": sum(s.bad_diff_caught for s in bad) / len(bad) if bad else 1.0,
    }
