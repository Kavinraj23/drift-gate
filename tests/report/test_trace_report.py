"""N5: the static trace report builds offline from the sample audit log and shows what the log really holds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from report_helpers import SAMPLE, inspect

from driftgate.audit import AuditEntry
from driftgate.report import build_pages, group_traces, parse_audit, render_trace
from driftgate.report.__main__ import main as cli_main
from driftgate.report.render import SECTIONS, file_name, fmt_ts
from driftgate.report.trace import derive_outcome

APPROVED_EXEC, DENIED_EXEC, TIER0_EXEC, PR_EXEC, ESCALATED_EXEC, REINVESTIGATED_EXEC = (
    "ex-000582",
    "ex-000638",
    "ex-000384",
    "ex-000112",
    "ex-000744",
    "ex-000863",
)


def page(pages: dict[str, str], execution_id: str) -> str:
    return pages[file_name(execution_id)]


def test_sample_builds_an_index_and_one_page_per_investigation(sample_pages: dict[str, str]) -> None:
    assert "index.html" in sample_pages and len(sample_pages) == 8
    index = inspect(sample_pages["index.html"])
    linked = {h for h in index.hrefs}
    assert linked == {n for n in sample_pages if n != "index.html"}


def test_the_sample_covers_every_kind_of_case_the_report_must_show(sample_entries: list[AuditEntry]) -> None:
    traces = {t.execution_id: derive_outcome(t).kind for t in group_traces(sample_entries)}
    assert traces[TIER0_EXEC] == "verified"
    assert traces[PR_EXEC] == "pr_verified"
    assert traces[ESCALATED_EXEC] == "escalated"
    assert traces[DENIED_EXEC] == "denied"
    assert traces[APPROVED_EXEC] == "verified"
    assert traces[REINVESTIGATED_EXEC] == "pr_verified"
    assert any(e.payload.get("stage") == "reinvestigation_start" for e in sample_entries)


def test_every_sample_page_has_every_section(sample_pages: dict[str, str]) -> None:
    for name, text in sample_pages.items():
        if name == "index.html":
            continue
        ids = inspect(text).ids
        for sid, _label in SECTIONS:
            assert sid in ids, f"{name} lacks section {sid}"


def test_every_evidence_item_links_to_an_existing_tool_call(
    sample_pages: dict[str, str], sample_reports: dict[str, Any]
) -> None:
    checked = 0
    for eid, report in sample_reports.items():
        text = page(sample_pages, eid)
        insp = inspect(text)
        anchors = [h for h in insp.hrefs if h.startswith("#tc-")]
        for h in anchors:
            assert h[1:] in insp.ids, f"{eid}: link {h} has no target"
        for item in report["evidence"]:
            assert f">{item['source']}</a>" in text, f"{eid}: evidence {item['source']} is not linked"
            checked += 1
    assert checked >= 8
    assert "NOT CITED" not in "".join(sample_pages.values())  # real sample evidence is all cited


def test_uncited_evidence_is_flagged_as_dropped(make) -> None:  # type: ignore[no-untyped-def]
    entries = [
        make("proposal", "agent", tool_call_ids=["c0"], tools=["get_execution"], evidence_kept=1, evidence_dropped=2),
    ]
    report = {
        "evidence": [
            {"source": "c0", "finding": "real", "supports": "hypothesis"},
            {"source": "c9", "finding": "made up", "supports": "hypothesis"},
            {"source": "", "finding": "no source", "supports": "hypothesis"},
        ]
    }
    text = render_trace(group_traces(entries, {"ex-1": report})[0])
    assert text.count("NOT CITED") == 2
    assert 'href="#tc-1-1"' in text and 'id="tc-1-1"' in text
    assert "dropped in code (uncited or invalid)</dt><dd>2" in text


def test_evidence_text_is_not_recorded_without_the_saved_report(sample_entries: list[AuditEntry]) -> None:
    trace = group_traces(sample_entries)[0]
    assert trace.report is None
    text = render_trace(trace)
    assert "not recorded: the audit log keeps only counts" in text
    assert "Evidence kept by the code (cited)" in text


def test_approvals_show_who_and_when_as_recorded(
    sample_pages: dict[str, str], sample_entries: list[AuditEntry]
) -> None:
    text = page(sample_pages, APPROVED_EXEC)
    requested = next(
        e for e in sample_entries if e.execution_id == APPROVED_EXEC and e.payload.get("stage") == "approval_requested"
    )
    decision = next(
        e for e in sample_entries if e.execution_id == APPROVED_EXEC and e.payload.get("stage") == "approval_decision"
    )
    refused = next(
        e for e in sample_entries if e.execution_id == APPROVED_EXEC and e.payload.get("stage") == "approval_refused"
    )
    approvals = text[text.index('id="approvals"') : text.index('id="execution"')]
    assert "trigger.user" in approvals and "SelfApproval" in approvals and "refused (approve)" in approvals
    assert "alice.oncall" in approvals and "1/1 approvals" in approvals
    for entry in (requested, decision, refused):
        assert fmt_ts(entry.ts) in approvals
    denied = page(sample_pages, DENIED_EXEC)
    assert "bob.reviewer" in denied and "request closed (denied)" in denied


def test_execution_verification_and_gate_reasons_are_shown(sample_pages: dict[str, str]) -> None:
    text = page(sample_pages, APPROVED_EXEC)
    assert "simulated execution of bump_pool_capacity" in text and "simulated verification" in text
    assert "approval_recheck" in text and "approval_precheck" in text  # the gate ran again at approve and execute time
    esc = page(sample_pages, ESCALATED_EXEC)
    assert "downgraded" in esc or "refused" in esc or "Escalated to a human" in esc


def test_reinvestigation_shows_both_rounds(sample_pages: dict[str, str]) -> None:
    text = page(sample_pages, REINVESTIGATED_EXEC)
    assert "Re-investigation started" in text and "Round 2" in text
    assert 'id="tc-2-1"' in text and 'id="tc-1-1"' in text
    assert "Investigation rounds recorded: 2" in text


def test_budget_and_cost_summary_from_the_saved_report(sample_pages: dict[str, str]) -> None:
    text = page(sample_pages, TIER0_EXEC)
    assert "cost_usd" in text and "input_tokens" in text


def test_dark_and_light_themes_and_no_external_resources(sample_pages: dict[str, str]) -> None:
    for name, text in sample_pages.items():
        assert "prefers-color-scheme:dark" in text
        assert (
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            in text
        )
        insp = inspect(text)
        assert not {
            "script",
            "link",
            "img",
            "iframe",
            "object",
            "embed",
            "form",
            "base",
            "svg",
            "audio",
            "video",
        } & set(insp.tags), name
        assert "<script" not in text.lower() and "@import" not in text and "url(" not in text
        assert "http://" not in text and "https://" not in text, name
        for tag, attr, value in insp.attrs:
            assert not attr.startswith("on"), (name, tag, attr)
            if attr in ("href", "src", "action"):
                assert value.startswith("#") or (value.startswith("trace-") and value.endswith(".html")), (name, value)


def test_output_is_deterministic_and_independent_of_input_order(
    sample_entries: list[AuditEntry], sample_reports: dict[str, Any]
) -> None:
    first = build_pages(sample_entries, sample_reports)
    second = build_pages(list(reversed(sample_entries)), dict(reversed(list(sample_reports.items()))))
    assert first == second
    assert all(a == b for a, b in zip(first.values(), second.values(), strict=True))


def test_timestamps_come_from_the_log_in_utc() -> None:
    assert fmt_ts(1_700_000_000.0) == "2023-11-14 22:13:20 UTC"
    assert fmt_ts("x") == "not recorded" and fmt_ts(None) == "not recorded" and fmt_ts(1e300) == "not recorded"


# -- empty and partial logs ----------------------------------------------------------------------------------------


def test_an_empty_log_renders_an_index_only() -> None:
    pages = build_pages([])
    assert list(pages) == ["index.html"] and "0 investigation(s)" in pages["index.html"]


def test_a_report_without_audit_entries_still_renders() -> None:
    pages = build_pages([], {"ex-9": {"execution_id": "ex-9", "hypothesis": "h", "evidence": [], "run": {}}})
    text = page(pages, "ex-9")
    assert "not recorded" in text and "<blockquote>h</blockquote>" in text


@pytest.mark.parametrize(
    "kind,stage",
    [
        ("gate_decision", ""),
        ("rejected", "prefilter"),
        ("proposal", "agent"),
        ("proposal", "approval_requested"),
        ("execution", "execute"),
        ("verification", "result"),
        ("expired", "approval"),
        ("failed", "dry_run"),
        ("budget_truncated", ""),
    ],
)
def test_a_log_with_one_partial_entry_renders(make, kind: str, stage: str) -> None:  # type: ignore[no-untyped-def]
    text = build_pages([make(kind, stage)])[file_name("ex-1")]
    assert 'id="audit-trail"' in text and "not recorded" in text


def test_prefilter_close_and_expiry_outcomes(make) -> None:  # type: ignore[no-untyped-def]
    closed = group_traces([make("rejected", "prefilter", reason="governance: approval rejected")])[0]
    assert derive_outcome(closed).kind == "closed"
    expired = group_traces(
        [
            make("proposal", "approval_requested", request_id=1, requested_by="t", expires_at=1_700_000_900.0),
            make("expired", "approval", request_id=1, expires_at=1_700_000_900.0, reason="window closed to a human"),
        ]
    )[0]
    out = derive_outcome(expired)
    assert out.kind == "expired"
    text = render_trace(expired)
    assert "2023-11-14 22:28:20 UTC" in text and "expired" in text


def test_damaged_lines_and_odd_types_are_skipped_or_coerced() -> None:
    good = json.dumps({"seq": 0, "ts": 1.0, "kind": "proposal", "fingerprint": "f", "execution_id": "e", "payload": {}})
    odd = json.dumps({"seq": "x", "ts": "later", "kind": 7, "payload": "not a dict"})
    entries, skipped = parse_audit("\n".join([good, "{not json", "[1,2]", "", odd, "null"]))
    assert len(entries) == 2 and skipped == 3
    text = build_pages(entries, skipped_lines=skipped)["index.html"]
    assert "3 unreadable audit line(s)" in text
    for e in entries:
        render_trace(group_traces([e])[0])


def test_deeply_nested_payload_does_not_crash(make) -> None:  # type: ignore[no-untyped-def]
    deep: Any = "x"
    for _ in range(5000):
        deep = [deep]
    text = build_pages([make("proposal", "gate_facts", facts={"a": deep}, extra=deep)])[file_name("ex-1")]
    assert 'id="audit-trail"' in text


# -- CLI and sample ------------------------------------------------------------------------------------------------


def test_cli_builds_from_the_sample_into_a_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["--audit", str(SAMPLE / "audit.jsonl"), "--out", str(tmp_path / "out")]) == 0
    written = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert written[0] == "index.html" and len(written) == 8
    assert "index.html" in capsys.readouterr().out


def test_cli_reports_a_missing_audit_log(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["--audit", str(tmp_path / "nope.jsonl"), "--out", str(tmp_path)]) == 1
    assert "not found" in capsys.readouterr().err


def test_cli_rejects_unknown_options() -> None:
    with pytest.raises(SystemExit) as e:
        cli_main(["--serve"])
    assert e.value.code == 2


def test_the_committed_sample_is_what_the_generator_produces(dataset_dir: Path, tmp_path: Path) -> None:
    from driftgate.eval.trace_sample import generate

    generate(dataset_dir, tmp_path)
    for name in ("audit.jsonl", "reports.json", "README.txt"):
        assert (tmp_path / name).read_bytes() == (SAMPLE / name).read_bytes(), name
