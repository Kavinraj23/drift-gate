"""N5 fidelity: the page never claims more than the log shows (outcomes, citations, names, caveat, CLI safety)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import pytest
from report_helpers import SAMPLE, inspect

from driftgate.audit import AuditEntry
from driftgate.report import build_pages, group_traces, render_trace
from driftgate.report.__main__ import main
from driftgate.report.render import CAVEAT, MARKER, file_name
from driftgate.report.trace import INFORMATIONAL, STAGE_EFFECT, TERMINAL, derive_outcome

Make = Callable[..., AuditEntry]
ROOT = Path(__file__).resolve().parents[2]


def outcome_of(entries: list[AuditEntry]) -> str:
    return derive_outcome(group_traces(entries)[0]).kind


def requested(make: Make) -> AuditEntry:
    return make("proposal", "approval_requested", request_id=1, requested_by="alice", expires_at=1_700_009_000.0)


def refused_execute(make: Make, status: str = "pending") -> AuditEntry:
    return make("rejected", "approval_execute_refused", request_id=1, status=status, reason="request is " + status)


def approved(make: Make) -> AuditEntry:
    return make("proposal", "approval_decision", request_id=1, approver="bob", decision="approve", status="approved")


# -- 1: refused, no-op and duplicate events never change the outcome ---------------------------------------------------
def test_execute_on_a_pending_request_stays_awaiting_approval(make: Make) -> None:
    assert outcome_of([requested(make), refused_execute(make, "pending")]) == "awaiting_approval"


def test_a_duplicate_execute_after_executed_and_verified_stays_verified(make: Make) -> None:
    entries = [
        requested(make),
        approved(make),
        make("execution", "execute", request_id=1, ok=True, description="done"),
        make("verification", "result", request_id=1, verified=True, description="ok"),
        make("verification", "closed", request_id=1),
        refused_execute(make, "executed"),
        refused_execute(make, "executed"),
    ]
    trace = group_traces(entries)[0]
    assert derive_outcome(trace).kind == "verified"
    assert derive_outcome(trace).label == "Executed and verified"
    assert "approval_execute_refused" in render_trace(trace)  # still shown in the audit trail and approvals


def test_a_refused_approval_and_prechecks_do_not_change_the_outcome(make: Make) -> None:
    entries = [
        requested(make),
        make("rejected", "approval_refused", request_id=1, decision="approve", error="SelfApproval", reason="no"),
        make("gate_decision", "approval_precheck", decision="refused", reason="kill switch", action="a"),
        make("gate_decision", "approval_recheck", decision="refused", reason="kill switch", action="a"),
        make("proposal", "history_skipped", reason="no history"),
        make("proposal", "approval_decision", request_id=1, approver="bob", decision="approve", status="pending"),
    ]
    assert outcome_of(entries) == "awaiting_approval"


def test_an_approved_request_whose_execute_is_refused_stays_approved(make: Make) -> None:
    assert outcome_of([requested(make), approved(make), refused_execute(make, "approved")]) == "approved"


def test_a_request_that_expires_still_shows_expired(make: Make) -> None:
    entries = [
        requested(make),
        make("expired", "approval", request_id=1, expires_at=1_700_009_000.0, reason="window closed to a human"),
        refused_execute(make, "expired"),
    ]
    assert outcome_of(entries) == "expired"


def test_a_denied_request_still_shows_denied_after_a_refused_execute(make: Make) -> None:
    entries = [
        requested(make),
        make("proposal", "approval_decision", request_id=1, approver="bob", decision="deny", status="denied"),
        make("rejected", "approval_denied", request_id=1, reason="denied by an approver"),
        refused_execute(make, "denied"),
    ]
    assert outcome_of(entries) == "denied"


def test_an_unknown_stage_cannot_change_the_outcome(make: Make) -> None:
    for kind in ("rejected", "failed", "expired", "execution", "verification", "proposal", "gate_decision"):
        entries = [
            requested(make),
            make(kind, "a_stage_added_later", ok=True, verified=False, status="approved", reason="x"),
        ]
        assert outcome_of(entries) == "awaiting_approval", kind
    assert outcome_of([make("rejected", reason="no stage at all")]) == "unknown"


def test_refusals_and_no_ops_are_classified_informational() -> None:
    assert set(STAGE_EFFECT.values()) == {TERMINAL, INFORMATIONAL}
    for key in (
        ("rejected", "approval_refused"),
        ("rejected", "approval_execute_refused"),
        ("gate_decision", "approval_precheck"),
        ("gate_decision", "approval_recheck"),
        ("proposal", "history_skipped"),
        ("verification", "rollback"),
        ("execution", "dry_run"),
    ):
        assert STAGE_EFFECT[key] == INFORMATIONAL, key


def test_every_stage_the_code_base_writes_is_classified() -> None:
    """A new audit stage must be classified on purpose: scan the writers for `stage="..."` literals."""
    stages: set[str] = set()
    for name in ("orchestrator.py", "approval_flow.py", "verify.py"):
        stages |= set(re.findall(r'stage="([a-z0-9_]+)"', (ROOT / "src" / "driftgate" / name).read_text("utf-8")))
    stages |= {"reinvestigation", "facts", "validation", "gate", "review", "diff_check", "agent"}  # _escalate(..., s)
    classified = {stage for _, stage in STAGE_EFFECT}
    assert stages <= classified, sorted(stages - classified)


# -- 2: a Tier 3 PR whose CI failed is not "executed" ----------
def test_a_tier3_pr_whose_ci_failed_reads_as_a_pr_awaiting_a_human(make: Make) -> None:
    entries = [
        make("execution", "pr_proposed", branch="driftgate/x", merged=False, opened=False),
        make("verification", "result", attempt_id="a1", verified=False, description="CI failed"),
    ]
    out = derive_outcome(group_traces(entries)[0])
    assert out.kind == "pr_ci_failed"
    assert out.label == "Pull request proposed, CI failed (awaiting a human)"
    assert "Executed" not in out.label


def test_a_tier0_action_that_executed_and_failed_verification_keeps_the_executed_label(make: Make) -> None:
    entries = [
        make("execution", "execute", ok=True, description="rerun"),
        make("verification", "result", attempt_id="a1", verified=False, description="recurred"),
    ]
    assert derive_outcome(group_traces(entries)[0]).label == "Executed, verification failed"


def test_a_failed_pr_then_an_executed_second_round_reads_from_the_last_event(make: Make) -> None:
    entries = [
        make("execution", "pr_proposed", branch="driftgate/x"),
        make("verification", "result", verified=False),
        make("execution", "execute", ok=True, description="rerun"),
        make("verification", "result", verified=False),
    ]
    assert outcome_of(entries) == "verification_failed"


# -- 3: three citation states ----------
def test_citation_states_cited_not_cited_and_cannot_verify(make: Make) -> None:
    item = {"source": "c0", "finding": "f", "supports": "s"}
    agent = make("proposal", "agent", tool_call_ids=["c0"], tools=["t"])
    cited = render_trace(group_traces([agent], {"ex-1": {"evidence": [item]}})[0])
    assert '<span class="ok">cited</span>' in cited
    assert "NOT CITED" not in cited and "cannot verify" not in cited

    other = {"source": "c9", "finding": "f", "supports": "s"}
    not_cited = render_trace(group_traces([agent], {"ex-1": {"evidence": [other]}})[0])
    assert '<span class="bad">NOT CITED' in not_cited and "cannot verify" not in not_cited

    # no tool-call record at all: no investigation entry in the log, or one with no ids
    for entries in ([make("rejected", "prefilter", reason="r")], [make("proposal", "agent", tool_call_ids=[])]):
        unknown = render_trace(group_traces(entries, {"ex-1": {"evidence": [item]}})[0])
        assert "cannot verify citation: no tool-call record in the log" in unknown
        assert '<span class="warn">cannot verify' in unknown
        assert "NOT CITED" not in unknown


# -- 4: caveat on every page ----------
def test_every_page_carries_the_caveat_and_the_marker(sample_pages: dict[str, str]) -> None:
    assert "reading of the audit log" in CAVEAT and "stubbed" in CAVEAT and "simulated" in CAVEAT
    for name, text in sample_pages.items():
        assert 'class="muted caveat"' in text and "reading of the audit log" in text, name
        assert f"<!-- {MARKER} -->" in text, name


# -- 5: file names ----------
def test_ids_differing_by_case_or_sanitisation_never_share_a_file_name_case_insensitively() -> None:
    ids = ["Ex-1", "ex-1", "EX-1", "a/b", "a_b", "a b", "a:b", "x" * 80, "X" * 80, "ex-1 ", "é", "É"]
    names = [file_name(i) for i in ids]
    assert len({n.lower() for n in names}) == len(ids)
    assert all(n == n.lower() for n in names)
    assert file_name("Ex-1") == file_name("Ex-1")


def test_pages_and_index_links_for_case_colliding_ids(make: Make) -> None:
    entries = [make("proposal", "agent", execution_id=i, tool_call_ids=[]) for i in ("Ex-1", "ex-1")]
    pages = build_pages(entries)
    assert len(pages) == 3 and len({n.lower() for n in pages}) == 3
    hrefs = inspect(pages["index.html"]).hrefs
    assert sorted(hrefs) == sorted(n for n in pages if n != "index.html")


# -- 6: CLI output safety ----------
def test_cli_refuses_an_output_path_that_is_a_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "out"
    target.write_text("keep me", encoding="utf-8")
    assert main(["--audit", str(SAMPLE / "audit.jsonl"), "--out", str(target)]) == 1
    assert "refused:" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "keep me"


def test_cli_refuses_an_output_path_below_a_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "f").write_text("x", encoding="utf-8")
    assert main(["--audit", str(SAMPLE / "audit.jsonl"), "--out", str(tmp_path / "f" / "sub")]) == 1
    assert "refused:" in capsys.readouterr().err


def test_cli_refuses_a_directory_where_a_page_should_go(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "index.html").mkdir()
    assert main(["--audit", str(SAMPLE / "audit.jsonl"), "--out", str(tmp_path)]) == 1
    assert "refused:" in capsys.readouterr().err
    assert (tmp_path / "index.html").is_dir()


def _audit_for(tmp_path: Path, make: Make, eid: str) -> Path:
    audit = tmp_path / "in" / "audit.jsonl"
    audit.parent.mkdir(exist_ok=True)
    entry = make("rejected", "prefilter", execution_id=eid, reason="r")
    audit.write_text(json.dumps(asdict(entry)) + "\n", encoding="utf-8")
    return audit


def test_cli_removes_only_its_own_stale_pages(tmp_path: Path, make: Make) -> None:
    out = tmp_path / "out"
    assert main(["--audit", str(_audit_for(tmp_path, make, "old-1")), "--out", str(out)]) == 0
    old = out / file_name("old-1")
    assert old.exists()
    foreign = out / "trace-notes.html"
    foreign.write_text("<p>my own page, no marker</p>", encoding="utf-8")
    other = out / "notes.txt"
    other.write_text("keep", encoding="utf-8")
    assert main(["--audit", str(_audit_for(tmp_path, make, "new-1")), "--out", str(out)]) == 0
    assert not old.exists() and (out / file_name("new-1")).exists()
    assert foreign.exists() and other.exists() and (out / "index.html").exists()


# -- 8: the fixture stays LF ----------
def test_the_sample_is_pinned_to_lf_endings() -> None:
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "fixtures/trace_sample/* text eol=lf" in attrs
    assert b"\r\n" not in (SAMPLE / "audit.jsonl").read_bytes()
