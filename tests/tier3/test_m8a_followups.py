"""M6 follow-ups applied in M8a: fail closed without a reviewer or on a missing verdict, protected declared paths,
committing the checked contents, and 'opened' versus 'proposed'."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_flow import report_model, tier3

from driftgate.adapters.synthetic import SyntheticSource, SyntheticTarget
from driftgate.domain import FileContent, Remediation, Review, SourceError
from driftgate.eval.ground_truth import FailureLabel
from driftgate.llm.budget import InvestigationBudget
from driftgate.orchestrator import ESCALATED, PR_PROPOSED
from driftgate.tier3 import PullRequest, PullRequestRecord, ReviewerHook

RunT3 = Callable[..., Any]


class RealishTarget(SyntheticTarget):
    """Pretends to be a provider that really opens PRs (non-simulated records)."""

    def open_pull_request(self, pr: PullRequest) -> PullRequestRecord:
        rec = super().open_pull_request(pr)
        return PullRequestRecord(
            rec.number, rec.branch, "https://example.invalid/pull/1", merged=False, simulated=False
        )


def test_without_a_reviewer_tier3_escalates_by_default(
    make_orch: Callable[..., Any], t3_labels: list[FailureLabel]
) -> None:
    label = t3_labels[0]
    target = SyntheticTarget()
    out = make_orch(label, target=target, allow_unreviewed_tier3=False).handle(label.execution_id)
    assert out.kind == ESCALATED and "no independent Tier 3 reviewer" in out.reason
    assert target.pull_requests == [] and out.pull_request is None and not out.pr_opened


def test_the_explicit_opt_out_keeps_the_sandbox_behaviour(
    make_orch: Callable[..., Any], t3_labels: list[FailureLabel]
) -> None:
    label = t3_labels[0]
    out = make_orch(label, allow_unreviewed_tier3=True).handle(label.execution_id)
    assert out.kind == PR_PROPOSED and out.pull_request is None and not out.pr_opened


def test_a_reviewer_hook_returning_none_is_a_rejection(
    make_orch: Callable[..., Any], t3_labels: list[FailureLabel]
) -> None:
    label = t3_labels[0]
    target = SyntheticTarget()
    orch = make_orch(label, target=target, tier3_review=lambda inv, rem: None)
    out = orch.handle(label.execution_id)
    assert out.kind == ESCALATED and "reviewer verdict reject" in out.reason
    assert out.report.review == Review("reject", "the reviewer returned no verdict")
    assert target.pull_requests == []
    audited = [e for e in orch.audit.read() if e.payload.get("stage") == "tier3_review"]
    assert [e.payload["verdict"] for e in audited] == ["reject"]


@pytest.mark.parametrize(
    "extra", [".env", "secrets/prod.tfvars", "deploy/id_rsa", ".git/config", "../outside.tf", "/abs.tf"]
)
def test_protected_or_unsafe_declared_paths_are_rejected_before_review(
    make_orch: Callable[..., Any], t3_labels: list[FailureLabel], extra: str
) -> None:
    label = t3_labels[0]
    reviewed: list[int] = []
    orch = make_orch(
        label,
        model_factory=lambda e, b: report_model(label, tier3(label, paths=[*label.fix_paths, extra])),
        tier3_review=lambda inv, rem: reviewed.append(1) or Review("approve", "ok"),
    )
    out = orch.handle(label.execution_id)
    assert out.kind == ESCALATED and reviewed == []
    assert "protected" in out.reason or "relative paths" in out.reason


def test_the_reviewer_hook_never_reads_protected_or_unsafe_paths(
    dataset_dir: Path, t3_labels: list[FailureLabel]
) -> None:
    label = t3_labels[0]
    reads: list[str] = []
    real = SyntheticSource(dataset_dir)

    class Spy:
        def get_execution(self, eid: str) -> Any:
            return real.get_execution(eid)

        def read_file(self, repo: str, path: str, ref: str, max_bytes: int) -> FileContent:
            reads.append(path)
            if path == ".env":
                raise SourceError("never reached")
            return real.read_file(repo, path, ref, max_bytes)

    class Stop(Exception):
        pass

    def factory(*_: Any) -> Any:
        raise Stop

    class Inv:
        budget = InvestigationBudget()  # the reviewer is charged to the case's shared budget (M7)

        class report:  # noqa: N801
            execution_id = label.execution_id
            hypothesis = "h"

    rem = Remediation(
        3,
        label.correct_action,
        "r",
        True,
        "pull_request",
        dry_run={"paths": [*label.fix_paths, ".env", "../x", "secrets/a"], "diff_text": label.fix_diff},
    )
    with pytest.raises(Stop):
        ReviewerHook(Spy(), factory)(Inv(), rem)  # type: ignore[arg-type]
    assert reads == list(label.fix_paths)


def test_a_real_target_gets_the_checked_contents_and_the_outcome_says_opened(
    run_t3: RunT3, t3_labels: list[FailureLabel], dataset_dir: Path
) -> None:
    from driftgate.diffs import apply_diff, parse_diff

    label = t3_labels[0]
    target = RealishTarget()
    run = run_t3(label, target=target)
    out = run.outcome
    assert out.kind == PR_PROPOSED and out.pr_opened and "opened on" in out.reason
    pr = target.pull_requests[0]
    src = SyntheticSource(dataset_dir)
    ref = src.get_execution(label.execution_id).refs.get("commit", "main")
    repo = src.get_execution(label.execution_id).pipeline
    expected = apply_diff(parse_diff(label.fix_diff), lambda p: src.read_file(repo, p, ref, 200_000).content)
    assert dict(pr.files) == expected and tuple(p for p, _ in pr.files) == pr.paths
    assert pr.base == "main" and pr.base_sha == ref  # the failing commit, so the branch starts from it
    last = [e for e in run.audit if e.payload.get("stage") == "pr_proposed"][-1]
    assert last.payload["opened"] is True and last.payload["merged"] is False


def test_a_simulated_pull_request_is_only_proposed(run_t3: RunT3, t3_labels: list[FailureLabel]) -> None:
    run = run_t3(t3_labels[0])
    assert run.outcome.kind == PR_PROPOSED and not run.outcome.pr_opened and "proposed on" in run.outcome.reason
    assert run.target.pull_requests[0].files  # the simulated target also receives the checked contents


def test_tier0_receives_the_execution_id_from_the_orchestrator(
    make_orch: Callable[..., Any], pick: Callable[..., FailureLabel]
) -> None:
    label = pick("transient_throttling", fleet=False)
    target = SyntheticTarget()
    make_orch(label, target=target).handle(label.execution_id)
    assert target.executed and target.executed[0].dry_run["execution_id"] == label.execution_id
