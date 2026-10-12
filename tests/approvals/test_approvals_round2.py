"""N4 round 2: pinned CLI inputs, tier-derived quorum, strict identities, gated approvals, audit, proof."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from driftgate.adapters.synthetic import LockEntry, LockTable
from driftgate.approval_flow import GateRefused, execute_approved
from driftgate.approvals import (
    ApprovalStore,
    IdentityError,
    NotOpen,
    SelfApproval,
    normalize_identity,
)
from driftgate.approvals_cli import ROOT, CliInputs, main
from driftgate.domain import DryRunResult, Execution, SourceError
from driftgate.eval.ground_truth import FailureLabel
from driftgate.eval.tier12_cases import Holder, World
from driftgate.gates import DEFAULT_KILL_SWITCH_PATH
from driftgate.lock_proof import derive_holder_proof
from driftgate.policy import DEFAULT_POLICY_PATH

MakeWorld = Callable[..., World]
DEAD = Holder()
PERMISSIVE = {"global": True, "tiers": {"0": True, "1": True, "2": True, "3": True}}


def new_request(store: ApprovalStore, tier: int = 1, by: str = "trigger", eid: str = "ex-1") -> int:
    return store.create(
        execution_id=eid, fingerprint="fp", tier=tier, action="a", requested_by=by, payload={"k": "v"}
    ).id


def raw(store: ApprovalStore) -> sqlite3.Connection:
    return sqlite3.connect(store.path)


def switch_off(world: World, **tiers: bool) -> None:
    state: dict[str, Any] = json.loads(json.dumps(PERMISSIVE))
    state["global"] = tiers.pop("global_", True)
    for k, v in tiers.items():
        state["tiers"][k.lstrip("t")] = v
    world.kill_switch.write_text(json.dumps(state))


# -- 1. the CLI pins the gate inputs ----------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--data", "--kill-switch", "--policy", "--audit", "--locks"])
def test_removed_cli_flags_are_rejected_before_or_after_the_subcommand(
    flag: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for argv in ([flag, str(tmp_path), "list"], ["list", flag, str(tmp_path)], ["execute", "1", flag, str(tmp_path)]):
        with pytest.raises(SystemExit) as e:
            main(["--state-dir", str(tmp_path / "s"), *argv])
        assert e.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("abbrev", ["--kill", "--stat", "--pol", "--da", "--loc", "--aud"])
def test_abbreviations_are_not_accepted_either(abbrev: str, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as e:
        main([abbrev, str(tmp_path), "list"])
    assert e.value.code == 2


def test_a_permissive_kill_switch_elsewhere_cannot_be_used_from_the_cli(
    make_world: MakeWorld, tier1_label: FailureLabel, tmp_path: Path
) -> None:
    w = make_world(tier1_label)
    w.service.approve(w.request_id, "alice")  # type: ignore[arg-type]
    other = tmp_path / "permissive.json"
    other.write_text(json.dumps(PERMISSIVE))
    state = w.kill_switch.parent
    with pytest.raises(SystemExit):
        main(["--state-dir", str(state), "--kill-switch", str(other), "execute", str(w.request_id)], clock=w.clock)
    assert w.orch.target.executed == []  # type: ignore[attr-defined]


def test_state_dir_selects_only_the_approvals_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock_factory: Callable[[], Any]
) -> None:
    import driftgate.approvals_cli as cli

    seen: list[CliInputs] = []

    def fake_build(store: ApprovalStore, pinned: CliInputs, clock: Any) -> Any:
        seen.append(pinned)
        raise ValueError("stop here")

    monkeypatch.setattr(cli, "_build", fake_build)
    assert main(["--state-dir", str(tmp_path / "elsewhere"), "expire-check"], clock=clock_factory(), out=None) == 1
    assert (tmp_path / "elsewhere" / "approvals.sqlite").exists()
    assert seen == [CliInputs()]
    pinned = CliInputs()
    assert pinned.kill_switch_path == DEFAULT_KILL_SWITCH_PATH and pinned.policy_path == DEFAULT_POLICY_PATH
    assert pinned.data_dir == ROOT / "data"
    assert pinned.audit_path == ROOT / "state" / "audit.jsonl" and pinned.locks_path == ROOT / "state" / "locks.json"


@pytest.fixture
def clock_factory() -> Callable[[], Any]:
    return lambda: lambda: 1_700_000_000.0


def test_tasks_approvals_injects_nothing_and_forwards_only_what_the_operator_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location("tasks_n4r2", ROOT / "tasks.py")
    tasks = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(tasks)  # type: ignore[union-attr]
    calls: list[list[str]] = []
    monkeypatch.setattr(
        tasks.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0})()
    )
    tasks.approvals(["list"])
    cmd = calls[0]
    assert cmd[-1] == "list" and cmd[-3:-1] == ["-m", "driftgate.approvals_cli"]
    assert not {"--data", "--kill-switch", "--policy", "--audit", "--locks"} & set(cmd)


# -- 2. quorum comes from the tier, status needs its decisions -------------------------------------------------


def test_lowering_required_on_a_tier2_row_is_refused_and_one_approver_cannot_execute(store: ApprovalStore) -> None:
    rid = new_request(store, tier=2)
    store.approve(rid, "alice")
    db = raw(store)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE requests SET required = 1 WHERE id = ?", (rid,))
        with pytest.raises(sqlite3.IntegrityError, match="justify"):
            db.execute("UPDATE requests SET status = 'approved' WHERE id = ?", (rid,))
    finally:
        db.close()
    assert store.get(rid).required == 2 and store.get(rid).status == "pending"
    with pytest.raises(NotOpen):
        store.claim_for_execution(rid)


def test_a_tier2_row_inserted_directly_with_required_1_is_refused(store: ApprovalStore) -> None:
    db = raw(store)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            db.execute(
                "INSERT INTO requests (execution_id, fingerprint, tier, action, requested_by, requested_at,"
                " expires_at, status, required, payload) VALUES ('e','f',2,'a','t',0,100,'pending',1,'{}')"
            )
    finally:
        db.close()


@pytest.mark.parametrize("status", ["approved", "executed", "denied", "expired", "cancelled"])
def test_a_request_cannot_be_inserted_in_any_status_but_pending(store: ApprovalStore, status: str) -> None:
    db = raw(store)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="created pending"):
            db.execute(
                "INSERT INTO requests (execution_id, fingerprint, tier, action, requested_by, requested_at,"
                " expires_at, status, required, payload) VALUES ('e','f',2,'a','t',0,100,?,2,'{}')",
                (status,),
            )
    finally:
        db.close()
    assert store.list() == []


@pytest.mark.parametrize("column,value", [("expires_at", 1e12), ("requested_at", 0), ("fingerprint", "x")])
def test_deadline_request_time_and_fingerprint_are_frozen(store: ApprovalStore, column: str, value: object) -> None:
    rid = new_request(store, tier=2)
    before = store.get(rid)
    db = raw(store)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(f"UPDATE requests SET {column} = ? WHERE id = ?", (value, rid))
    finally:
        db.close()
    assert store.get(rid) == before


def test_an_over_long_deadline_cannot_be_inserted(store: ApprovalStore) -> None:
    db = raw(store)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            db.execute(
                "INSERT INTO requests (execution_id, fingerprint, tier, action, requested_by, requested_at,"
                " expires_at, status, required, payload) VALUES ('e','f',2,'a','t',0,999999,'pending',2,'{}')"
            )
    finally:
        db.close()


def test_status_cannot_be_forced_to_approved_or_executed_without_the_decisions(store: ApprovalStore) -> None:
    t1 = new_request(store, tier=1)
    denied_first = new_request(store, tier=1, eid="ex-2")
    store.approve(denied_first, "alice")  # now approved legitimately
    other = new_request(store, tier=1, eid="ex-3")
    store.deny(other, "bob")
    db = raw(store)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="justify"):
            db.execute("UPDATE requests SET status = 'approved' WHERE id = ?", (t1,))
        with pytest.raises(sqlite3.IntegrityError, match="illegal"):
            db.execute("UPDATE requests SET status = 'approved' WHERE id = ?", (other,))
    finally:
        db.close()
    assert store.get(t1).status == "pending"


def test_the_requester_is_not_counted_even_if_a_decision_row_for_them_existed(store: ApprovalStore) -> None:
    rid = new_request(store, tier=1, by="trigger")
    db = raw(store)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="triggered the run"):
            db.execute(
                "INSERT INTO decisions (request_id, approver, decision, at) VALUES (?, 'trigger', 'approve', 0)",
                (rid,),
            )
        db.execute(
            "INSERT INTO decisions (request_id, approver, decision, at) VALUES (?, 'trigger', 'deny', 0)", (rid,)
        )
        with pytest.raises(sqlite3.IntegrityError, match="justify"):
            db.execute("UPDATE requests SET status = 'approved' WHERE id = ?", (rid,))
    finally:
        db.close()
    assert store.approvers(rid) == []


# -- 3. identities --------------------------------------------------------------------------------------------


INVISIBLE = [
    "alice​",  # zero width space
    "alice‍",  # zero width joiner
    "al⁠ice",  # word joiner
    "alice﻿",  # BOM / zero width no-break space
    "ali­ce",  # soft hyphen
    "alice‮",  # right-to-left override
    "alice⁦",  # bidi isolate
    "alice️",  # variation selector-16
    "alice\U000e0100",  # variation selector supplement
    "alice ",  # line separator
    "alice ",  # paragraph separator
    "ali\x00ce",
    "alice\x7f",
    "alice\x1b[0m",
    "alice\tbob",
    "aliceㅤ",  # hangul filler
    "alice\U000e0041",  # tag character
]


@pytest.mark.parametrize("raw_name", INVISIBLE)
def test_identities_with_invisible_characters_are_refused_everywhere(store: ApprovalStore, raw_name: str) -> None:
    with pytest.raises(IdentityError):
        normalize_identity(raw_name)
    rid = new_request(store, tier=2)
    store.approve(rid, "alice")
    for act in (store.approve, store.deny):
        with pytest.raises(IdentityError):
            act(rid, raw_name)
    with pytest.raises(IdentityError):
        new_request(store, by=raw_name, eid="ex-2")
    assert store.approvers(rid) == ["alice"] and len(store.decisions(rid)) == 1


def test_the_trigger_cannot_approve_as_trigger_plus_a_zero_width_character(store: ApprovalStore) -> None:
    rid = new_request(store, tier=1, by="trigger")
    with pytest.raises(IdentityError):
        store.approve(rid, "trigger​")
    assert store.get(rid).status == "pending"


@pytest.mark.parametrize(
    "raw_name,expected",
    [
        ("ＡＬＩＣＥ", "alice"),  # fullwidth
        ("ａlice", "alice"),
        ("ALICE", "alice"),
        ("  Alice   Smith ", "alice smith"),  # NBSP is whitespace after NFKC
        ("Ⅰ", "i"),  # roman numeral one -> I -> i
        ("ﬁle", "file"),  # ligature fi
        ("straẞ", "strass"),  # capital sharp s casefolds to ss
    ],
)
def test_nfkc_variants_fold_to_one_identity(raw_name: str, expected: str) -> None:
    assert normalize_identity(raw_name) == expected


@pytest.mark.parametrize("raw_name", ["аlice", "alicé", "alicé", "الن", "alice☃"])
def test_identities_that_are_not_printable_ascii_after_normalisation_are_refused(raw_name: str) -> None:
    with pytest.raises(IdentityError):
        normalize_identity(raw_name)


def test_fullwidth_trigger_name_is_still_the_trigger(store: ApprovalStore) -> None:
    rid = new_request(store, tier=1, by="trigger")
    with pytest.raises(SelfApproval):
        store.approve(rid, "ＴＲＩＧＧＥＲ")
    assert store.approvers(rid) == []


@pytest.mark.parametrize("name", ["Alice", "ALICE", "alice ", " alice", "al  ice", "alice​", "аlice", "ali\tce"])
def test_direct_inserts_must_already_be_in_normalised_form(store: ApprovalStore, name: str) -> None:
    rid = new_request(store, tier=2)
    store.approve(rid, "alice")
    db = raw(store)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO decisions (request_id, approver, decision, at) VALUES (?, ?, 'approve', 0)", (rid, name)
            )
    finally:
        db.close()
    assert store.approvers(rid) == ["alice"]


def test_a_direct_insert_cannot_make_one_person_count_twice(store: ApprovalStore) -> None:
    rid = new_request(store, tier=2)
    store.approve(rid, "alice")
    db = raw(store)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO decisions (request_id, approver, decision, at) VALUES (?, 'Alice', 'approve', 0)", (rid,)
            )
    finally:
        db.close()
    assert store.get(rid).status == "pending" and store.approvers(rid) == ["alice"]


def test_a_requester_cannot_be_inserted_into_requests_in_unnormalised_form(store: ApprovalStore) -> None:
    db = raw(store)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            db.execute(
                "INSERT INTO requests (execution_id, fingerprint, tier, action, requested_by, requested_at,"
                " expires_at, status, required, payload) VALUES ('e','f',1,'a','Trigger',0,100,'pending',1,'{}')"
            )
    finally:
        db.close()


# -- 5. approvals pass through the gate ------------------------------------------------------------------------


def audit_rows(world: World, kind: str, stage: str) -> list[dict[str, Any]]:
    return [dict(e.payload) for e in world.orch.audit.read(kind) if e.payload.get("stage") == stage]


def test_approve_with_the_kill_switch_off_is_refused_audited_and_leaves_the_request_pending(
    make_world: MakeWorld, tier1_label: FailureLabel
) -> None:
    w = make_world(tier1_label)
    rid = w.request_id
    switch_off(w, global_=False)
    with pytest.raises(GateRefused, match="kill switch"):
        w.service.approve(rid, "alice")  # type: ignore[arg-type]
    req = w.store.get(rid)  # type: ignore[arg-type]
    assert req.status == "pending" and w.store.decisions(rid) == []  # type: ignore[arg-type]
    refused = audit_rows(w, "rejected", "approval_refused")
    assert len(refused) == 1 and refused[0]["error"] == "GateRefused" and "kill switch" in refused[0]["reason"]
    pre = audit_rows(w, "gate_decision", "approval_precheck")
    assert len(pre) == 1 and pre[0]["decision"] == "refused"
    # the refusal consumed nothing: once the switch is back on the same request can be approved and executed
    switch_off(w)
    w.service.approve(rid, "alice")  # type: ignore[arg-type]
    assert execute_approved(w.orch, w.store, rid, clock=w.clock).kind == "executed"  # type: ignore[arg-type]


def test_a_per_tier_kill_switch_also_blocks_approval(make_world: MakeWorld, tier1_label: FailureLabel) -> None:
    w = make_world(tier1_label)
    switch_off(w, t1=False)
    with pytest.raises(GateRefused):
        w.service.approve(w.request_id, "alice")  # type: ignore[arg-type]
    assert w.store.get(w.request_id).status == "pending"  # type: ignore[arg-type]


def test_a_malformed_kill_switch_blocks_approval(make_world: MakeWorld, tier1_label: FailureLabel) -> None:
    w = make_world(tier1_label)
    w.kill_switch.write_text("{not json")
    with pytest.raises(GateRefused):
        w.service.approve(w.request_id, "alice")  # type: ignore[arg-type]
    assert w.store.decisions(w.request_id) == []  # type: ignore[arg-type]


def test_approve_with_a_live_tier2_holder_is_refused(make_world: MakeWorld, tier2_label: FailureLabel) -> None:
    w = make_world(tier2_label, DEAD)
    w.table.locks[w.lock_id].alive = True  # the holder came back to life after the request was opened
    with pytest.raises(GateRefused, match="running"):
        w.service.approve(w.request_id, "alice")  # type: ignore[arg-type]
    assert w.store.decisions(w.request_id) == []  # type: ignore[arg-type]
    assert audit_rows(w, "gate_decision", "approval_precheck")[0]["decision"] == "refused"


def test_the_second_tier2_approval_is_refused_if_the_holder_went_live_between_the_two(
    make_world: MakeWorld, tier2_label: FailureLabel
) -> None:
    w = make_world(tier2_label, DEAD)
    w.service.approve(w.request_id, "alice")  # type: ignore[arg-type]
    w.table.locks[w.lock_id].alive = True
    with pytest.raises(GateRefused):
        w.service.approve(w.request_id, "bob")  # type: ignore[arg-type]
    assert w.store.approvers(w.request_id) == ["alice"]  # type: ignore[arg-type]
    assert w.store.get(w.request_id).status == "pending"  # type: ignore[arg-type]


def test_deny_is_exempt_from_the_gate_and_the_requester_may_deny_their_own_request(
    make_world: MakeWorld, tier1_label: FailureLabel
) -> None:
    w = make_world(tier1_label)
    switch_off(w, global_=False)
    assert w.service.deny(w.request_id, "TRIGGER.user").status == "denied"  # type: ignore[arg-type]
    assert audit_rows(w, "gate_decision", "approval_precheck") == []


def test_the_precheck_is_not_a_rate_limit_attempt(make_world: MakeWorld, tier1_label: FailureLabel) -> None:
    w = make_world(tier1_label)
    w.service.approve(w.request_id, "alice")  # type: ignore[arg-type]
    plain = [e for e in w.orch.audit.read("gate_decision") if "stage" not in e.payload]
    assert len(plain) == 1  # only the request-time decision is an attempt


def test_execute_still_rechecks_after_an_approval_that_passed_its_own_gate(
    make_world: MakeWorld, tier1_label: FailureLabel
) -> None:
    w = make_world(tier1_label)
    w.service.approve(w.request_id, "alice")  # type: ignore[arg-type]
    assert audit_rows(w, "gate_decision", "approval_precheck")[0]["decision"] == "allowed"
    switch_off(w, global_=False)  # changed after the approval
    res = execute_approved(w.orch, w.store, w.request_id, clock=w.clock)  # type: ignore[arg-type]
    assert res.kind == "escalated" and res.gate is not None and res.gate.decision == "refused"
    assert w.store.get(w.request_id).status == "cancelled" and w.orch.target.executed == []  # type: ignore[attr-defined,arg-type]


def test_an_approval_never_turns_a_downgrade_into_an_allow(make_world: MakeWorld, tier2_label: FailureLabel) -> None:
    w = make_world(tier2_label, DEAD)
    del w.table.locks[w.lock_id]  # no proof any more
    with pytest.raises(GateRefused, match="downgraded"):
        w.service.approve(w.request_id, "alice")  # type: ignore[arg-type]
    w.store.approve(w.request_id, "alice")  # forced straight into the store, as a forged approval would be
    w.store.approve(w.request_id, "bob")
    res = execute_approved(w.orch, w.store, w.request_id, clock=w.clock)  # type: ignore[arg-type]
    assert res.kind == "escalated" and res.gate is not None and res.gate.decision == "downgraded"


# -- 6. expiry found by the claim is audited -------------------------------------------------------------------


class Wrapped:
    """Delegates to the real synthetic target; subclasses override one step."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def dry_run(self, action: Any) -> DryRunResult:
        return self.inner.dry_run(action)

    def execute(self, action: Any) -> Any:
        return self.inner.execute(action)

    def verify(self, action: Any) -> Any:
        return self.inner.verify(action)


def test_an_expiry_discovered_by_the_claim_is_audited_like_every_other_expiry(
    make_world: MakeWorld, tier1_label: FailureLabel
) -> None:
    w = make_world(tier1_label)
    w.service.approve(w.request_id, "alice")  # type: ignore[arg-type]
    req = w.store.get(w.request_id)  # type: ignore[arg-type]

    class SlowDryRun(Wrapped):
        def dry_run(self, action: Any) -> DryRunResult:
            result = self.inner.dry_run(action)
            w.clock.now = req.expires_at  # the window closes while the dry run is running
            return result

    res = execute_approved(w.orch, w.store, req.id, clock=w.clock, target=SlowDryRun(w.orch.target))  # type: ignore[arg-type]
    assert res.kind == "escalated" and res.status == "expired" and w.orch.target.executed == []  # type: ignore[attr-defined]
    expired = audit_rows(w, "expired", "approval")
    assert len(expired) == 1 and expired[0]["request_id"] == req.id and "escalated to a human" in expired[0]["reason"]
    assert audit_rows(w, "rejected", "approval_execute_refused")[-1]["status"] == "expired"


# -- 7. a holder equal to the failing execution is a contradiction ---------------------------------------------

LOCK = "eac527e5-bc46-7776-8d95-e9666f260927"


class Source:
    def __init__(self, statuses: dict[str, str]) -> None:
        self._s = statuses

    def get_execution(self, execution_id: str) -> Execution:
        if execution_id not in self._s:
            raise SourceError(f"unknown execution: {execution_id}")
        return Execution(execution_id, "p", self._s[execution_id], "2026-01-01T00:00:00Z")


def test_a_lock_holder_that_is_the_failing_execution_is_unknown_not_dead() -> None:
    table = LockTable({LOCK: LockEntry("ex-failing", False)})
    source = Source({"ex-failing": "failed", "ex-other": "failed"})
    same = derive_holder_proof(LOCK, table, source, failing_execution_id="ex-failing")
    assert (same.status, same.proven) == ("unknown", False) and "failing execution itself" in same.reason
    other = derive_holder_proof(LOCK, table, source, failing_execution_id="ex-other")
    assert (other.status, other.proven) == ("dead", True)
    assert derive_holder_proof(LOCK, table, source).proven  # callers that do not say keep the old behaviour


def test_the_orchestrator_passes_the_failing_execution_to_the_proof(
    make_world: MakeWorld, tier2_label: FailureLabel
) -> None:
    w = make_world(tier2_label, DEAD)
    w.table.locks[w.lock_id] = LockEntry(tier2_label.execution_id, False)  # the lock names the failing run itself
    with pytest.raises(GateRefused, match="downgraded"):
        w.service.approve(w.request_id, "alice")  # type: ignore[arg-type]
    proof = [e.payload for e in w.orch.audit.read("proposal") if e.payload.get("stage") == "holder_proof"][-1]
    assert proof["status"] == "unknown" and proof["proven"] is False


# -- 8. the proof holds but the target's own dry run fails -----------------------------------------------------


class FailingDryRun(Wrapped):
    def dry_run(self, action: Any) -> DryRunResult:
        self.inner.calls.append(("dry_run", action.action))
        return DryRunResult(False, "simulated target dry run failure")


@pytest.mark.parametrize("tier", [1, 2])
def test_a_failing_target_dry_run_never_executes_and_cancels_the_request(
    tier: int, make_world: MakeWorld, tier1_label: FailureLabel, tier2_label: FailureLabel
) -> None:
    w = make_world(tier1_label) if tier == 1 else make_world(tier2_label, DEAD)
    if tier == 2:
        assert w.outcome.gate is not None and w.outcome.gate.allowed  # the deterministic proof holds
    w.service.approve(w.request_id, "alice")  # type: ignore[arg-type]
    if tier == 2:
        w.service.approve(w.request_id, "bob")  # type: ignore[arg-type]
    res = execute_approved(w.orch, w.store, w.request_id, clock=w.clock, target=FailingDryRun(w.orch.target))  # type: ignore[arg-type]
    assert res.kind == "escalated" and "dry run failed" in res.reason
    assert w.orch.target.executed == []  # type: ignore[attr-defined]
    if tier == 2:
        assert w.lock_id in w.table.locks  # the simulated lock was not touched
    assert w.store.get(w.request_id).status == "cancelled"  # type: ignore[arg-type]
    dry = audit_rows(w, "execution", "dry_run")
    assert dry and dry[-1]["ok"] is False
    assert audit_rows(w, "failed", "dry_run") and audit_rows(w, "rejected", "approval_execute_refused")
    assert not audit_rows(w, "execution", "execute")
