"""N5 fidelity: the page never claims more than the log shows (outcomes, citations, names, caveat, CLI safety)."""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import pytest
from report_helpers import SAMPLE, inspect

from driftgate.audit import KINDS, AuditEntry
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


def _call_name(node: ast.Call) -> str:
    f = node.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _const_str(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def written_kind_stage_pairs(source: str) -> set[tuple[str, str]]:
    """(kind, stage) pairs the module writes, read from the syntax tree. Handled call shapes:
    `x.append("kind", ..., stage="s")` (kind = first positional string, stage = keyword `stage`; no `stage` keyword and
    no `**` splat means stage ""), `x._escalate(out, fp, reason, "s")` and `x._evaluate(o, a, r, clock, "s")` (stage
    positional or keyword; kinds `rejected` and `gate_decision`), and `record_decision(..., stage="s")` (kind
    `gate_decision`, stage "" when absent). Only string literals count."""
    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        splat = any(k.arg is None for k in node.keywords)
        pos = node.args
        if name == "append" and pos and (kind := _const_str(pos[0])) in KINDS:
            stage = _const_str(kw.get("stage"))
            if stage is not None:
                pairs.add((kind, stage))
            elif "stage" not in kw and not splat:
                pairs.add((kind, ""))
        elif name == "_escalate":
            stage = _const_str(kw["stage"] if "stage" in kw else pos[3] if len(pos) > 3 else None)
            if stage is not None:  # None (the literal) means "write nothing"
                pairs.add(("rejected", stage))
        elif name == "_evaluate":
            stage = _const_str(kw["stage"] if "stage" in kw else pos[4] if len(pos) > 4 else None)
            if stage is not None:
                pairs.add(("gate_decision", stage))
        elif name == "record_decision":
            stage = _const_str(kw.get("stage")) if "stage" in kw else ("" if not splat else None)
            if stage is not None:
                pairs.add(("gate_decision", stage))
    return pairs


def test_every_kind_and_stage_the_code_base_writes_is_classified() -> None:
    """Parses every module under src/driftgate with `ast` and collects (kind, stage) pairs from the audit writer calls
    (`append`, `_escalate`, `_evaluate`, `record_decision`), from keyword AND positional arguments. Fails if any pair
    is missing from STAGE_EFFECT, so a new stage must be classified on purpose. It does NOT guard: stages or kinds
    that are not string literals at the call (a variable, an f-string, a value passed through another wrapper), calls
    reached only through helpers not named above, whether a pair is classified as terminal or informational
    correctly (that is covered by the behaviour tests), or what `derive_outcome` does with a terminal pair's payload."""
    found: set[tuple[str, str]] = set()
    for path in sorted((ROOT / "src" / "driftgate").rglob("*.py")):
        if path.parts[-3:-1] == ("driftgate", "report"):
            continue  # the reader, not a writer
        found |= written_kind_stage_pairs(path.read_text("utf-8"))
    assert len(found) > 20  # the scan is not vacuous
    assert {("rejected", "reinvestigation"), ("gate_decision", "approval_recheck")} <= found  # positional shapes seen
    assert found <= set(STAGE_EFFECT), sorted(found - set(STAGE_EFFECT))


def test_the_stage_scan_sees_keyword_and_positional_shapes() -> None:
    src = (
        'a.append("failed", fp, eid, stage="k")\n'
        'self._escalate(out, fp, r, "p")\n'
        "self._escalate(out, fp, r, None)\n"
        'self._escalate(out, fp, r, stage="q")\n'
        '_evaluate(o, a, r, c, "e", gate_factory=f)\n'
        'record_decision(a, d, fp, eid, stage="rd")\n'
        "record_decision(a, d, fp, eid)\n"
        'a.append("proposal", fp, eid, stage=s)\n'
        'lst.append("not a kind")\n'
    )
    assert written_kind_stage_pairs(src) == {
        ("failed", "k"),
        ("rejected", "p"),
        ("rejected", "q"),
        ("gate_decision", "e"),
        ("gate_decision", "rd"),
        ("gate_decision", ""),
    }


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


# -- 3: a refusal that CANCELLED the request is a state change; one that left it alone is not -----------------------
CANCELLED_LABEL = "Approval cancelled before execution (not executed)"


def test_a_refusal_that_cancelled_an_approved_request_is_terminal_not_awaiting_execution(make: Make) -> None:
    entries = [requested(make), approved(make), refused_execute(make, "cancelled")]
    out = derive_outcome(group_traces(entries)[0])
    assert out.kind == "approval_cancelled" and out.label == CANCELLED_LABEL
    assert "Approved, awaiting execution" not in render_trace(group_traces(entries)[0])


def test_the_cancelled_outcome_comes_from_the_status_field_not_the_message(make: Make) -> None:
    kept = make("rejected", "approval_execute_refused", request_id=1, status="approved", reason="request is cancelled")
    assert outcome_of([requested(make), approved(make), kept]) == "approved"
    odd = make("rejected", "approval_execute_refused", request_id=1, status="Cancelled", reason="x")
    assert outcome_of([requested(make), approved(make), odd]) == "approved"
    no_status = make("rejected", "approval_execute_refused", request_id=1, reason="cancelled")
    assert outcome_of([requested(make), approved(make), no_status]) == "approved"


def test_a_cancelled_request_is_not_escalated_or_executed(make: Make) -> None:
    out = derive_outcome(group_traces([requested(make), approved(make), refused_execute(make, "cancelled")])[0])
    assert out.kind not in ("escalated", "executed", "verified")
    assert "Escalated" not in out.label and "Executed" not in out.label


def test_a_dry_run_failure_then_cancel_reads_as_cancelled(make: Make) -> None:
    entries = [
        requested(make),
        approved(make),
        make("execution", "dry_run", request_id=1, ok=False, description="boom"),
        make("failed", "dry_run", request_id=1, description="boom"),
        refused_execute(make, "cancelled"),
    ]
    out = derive_outcome(group_traces(entries)[0])
    assert out.kind == "approval_cancelled" and out.label == CANCELLED_LABEL
    assert "target dry run failed" in out.detail and "boom" not in out.label


def test_a_gate_refusal_detail_says_refused_at_execution(make: Make) -> None:
    out = derive_outcome(group_traces([requested(make), approved(make), refused_execute(make, "cancelled")])[0])
    assert "request 1: refused at execution" in out.detail and "dry run" not in out.detail


def _second_request(make: Make) -> AuditEntry:
    return make("proposal", "approval_requested", request_id=2, requested_by="alice", expires_at=1_700_009_000.0)


def _old_cancel(make: Make) -> AuditEntry:
    return make("rejected", "approval_execute_refused", request_id=1, status="cancelled", reason="late")


def test_a_late_cancellation_of_an_old_request_does_not_flip_a_case_with_a_newer_request(make: Make) -> None:
    entries = [requested(make), approved(make), _second_request(make), _old_cancel(make)]
    assert outcome_of(entries) == "awaiting_approval"


def test_a_cancellation_of_the_latest_request_still_applies(make: Make) -> None:
    # entries are created in the order they are logged (the fixture numbers them as it goes)
    first, first_cancel = requested(make), _old_cancel(make)
    second = _second_request(make)
    assert outcome_of([first, first_cancel, second]) == "awaiting_approval"
    second_cancel = make("rejected", "approval_execute_refused", request_id=2, status="cancelled", reason="y")
    assert outcome_of([first, first_cancel, second, second_cancel]) == "approval_cancelled"


def test_a_cancellation_without_a_request_id_does_not_apply_once_a_request_is_known(make: Make) -> None:
    anonymous = make("rejected", "approval_execute_refused", status="cancelled", reason="x")
    assert outcome_of([requested(make), approved(make), anonymous]) == "approved"


def test_the_index_and_the_page_show_the_cancelled_label(make: Make) -> None:
    entries = [requested(make), approved(make), refused_execute(make, "cancelled")]
    pages = build_pages(entries)
    assert CANCELLED_LABEL in inspect(pages["index.html"]).text
    page = next(v for k, v in pages.items() if k != "index.html")
    assert CANCELLED_LABEL in "".join(inspect(page).text)
    assert 'class="badge approval_cancelled"' in page


def test_a_cancelled_refusal_after_executed_is_not_possible_but_executed_refusals_stay_informational(
    make: Make,
) -> None:
    entries = [
        requested(make),
        approved(make),
        make("execution", "execute", request_id=1, ok=True, description="done"),
        refused_execute(make, "executed"),
    ]
    assert outcome_of(entries) == "executed"
