"""N4 hardening: schema version, code-level backstops with the triggers removed, embedded NUL, CLI load errors,
and the audit history in `approvals show`."""

from __future__ import annotations

import io
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from driftgate.approval_flow import execute_approved
from driftgate.approvals import (
    SCHEMA_VERSION,
    TIER1_TIMEOUT_S,
    ApprovalStore,
    IdentityError,
    NotOpen,
    SchemaError,
    normalize_identity,
)
from driftgate.approvals_cli import CliInputs, main
from driftgate.eval.ground_truth import FailureLabel
from driftgate.eval.tier12_cases import Holder, ManualClock, World

MakeWorld = Callable[..., World]


def new_request(store: ApprovalStore, tier: int = 1, by: str = "trigger", eid: str = "ex-1") -> int:
    return store.create(
        execution_id=eid, fingerprint="fp", tier=tier, action="a", requested_by=by, payload={"k": "v"}
    ).id


# -- F1: schema version ---------------------------------------------------------------------------------------------


def make_file(tmp_path: Path, mutate: Callable[[sqlite3.Connection], None] | None = None) -> Path:
    path = tmp_path / "state" / "approvals.sqlite"
    ApprovalStore(path, ManualClock()).close()
    if mutate:
        db = sqlite3.connect(path)
        try:
            mutate(db)
            db.commit()
        finally:
            db.close()
    return path


def test_a_fresh_database_gets_the_current_schema_and_version_and_reopens(tmp_path: Path) -> None:
    clock = ManualClock()
    path = tmp_path / "state" / "approvals.sqlite"
    with ApprovalStore(path, clock) as s:
        rid = new_request(s)
    db = sqlite3.connect(path)
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION != 0
    finally:
        db.close()
    with ApprovalStore(path, clock) as again:  # reopening is fine and keeps the data
        assert again.get(rid).status == "pending"


OLD_SCHEMA = """
CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT, fingerprint TEXT, tier INTEGER,
    action TEXT, requested_by TEXT, requested_at REAL, expires_at REAL, status TEXT, required INTEGER,
    payload TEXT, note TEXT DEFAULT '', gate_seq INTEGER);
CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, approver TEXT, decision TEXT,
    at REAL, UNIQUE (request_id, approver));
"""


def test_a_pre_hardening_database_is_refused_and_left_untouched(tmp_path: Path) -> None:
    path = tmp_path / "approvals.sqlite"
    db = sqlite3.connect(path)
    db.executescript(OLD_SCHEMA)  # what an older build created: no CHECKs, no triggers, user_version 0
    db.close()
    before = path.read_bytes()
    with pytest.raises(SchemaError, match="no migration"):
        ApprovalStore(path, ManualClock())
    assert path.read_bytes() == before


def test_old_tables_stamped_with_the_current_version_are_still_refused(tmp_path: Path) -> None:
    path = tmp_path / "approvals.sqlite"
    db = sqlite3.connect(path)
    db.executescript(OLD_SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION};")
    db.close()
    with pytest.raises(SchemaError, match="does not match"):
        ApprovalStore(path, ManualClock())


@pytest.mark.parametrize("version", [0, SCHEMA_VERSION - 1, SCHEMA_VERSION + 1])
def test_a_version_mismatch_is_refused(tmp_path: Path, version: int) -> None:
    path = make_file(tmp_path, lambda db: db.execute(f"PRAGMA user_version = {version}"))
    with pytest.raises(SchemaError, match="schema version"):
        ApprovalStore(path, ManualClock())


def test_a_missing_trigger_is_refused(tmp_path: Path) -> None:
    path = make_file(tmp_path, lambda db: db.execute("DROP TRIGGER decisions_no_self_approval"))
    with pytest.raises(SchemaError, match="decisions_no_self_approval"):
        ApprovalStore(path, ManualClock())


def test_a_changed_trigger_or_an_extra_object_is_refused(tmp_path: Path) -> None:
    def weaken(db: sqlite3.Connection) -> None:
        db.execute("DROP TRIGGER decisions_no_self_approval")
        db.execute("CREATE TRIGGER decisions_no_self_approval BEFORE INSERT ON decisions WHEN 0 BEGIN SELECT 1; END")

    with pytest.raises(SchemaError):
        ApprovalStore(make_file(tmp_path / "a", weaken), ManualClock())
    with pytest.raises(SchemaError):
        ApprovalStore(make_file(tmp_path / "b", lambda db: db.execute("CREATE TABLE extra (x)")), ManualClock())


def test_the_cli_refuses_a_stale_database_with_a_clear_message(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    db = sqlite3.connect(state / "approvals.sqlite")
    db.executescript(OLD_SCHEMA)
    db.close()
    out = io.StringIO()
    code = main(["--state-dir", str(state), "list"], clock=ManualClock(), out=out)
    assert code == 1 and "refused" in out.getvalue() and "no migration" in out.getvalue()


# -- F2: the code-level backstops, with every trigger removed ---------------------------------------------------------


def forge(store: ApprovalStore, rid: int, approvers: list[str], status: str = "approved") -> None:
    """Write decisions and a status straight into the file with the triggers dropped and CHECKs ignored: the
    state a partial or hostile writer could leave. Only the Python-side recounts can still say no."""
    db = sqlite3.connect(store.path)
    try:
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
            db.execute(f"DROP TRIGGER {name}")
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0] == 0
        db.execute("PRAGMA ignore_check_constraints = ON")
        db.execute("DELETE FROM decisions WHERE request_id = ?", (rid,))
        for who in approvers:
            db.execute(
                "INSERT INTO decisions (request_id, approver, decision, at) VALUES (?,?,'approve',0)", (rid, who)
            )
        db.execute("UPDATE requests SET status = ? WHERE id = ?", (status, rid))
        db.commit()
    finally:
        db.close()


REQ = "trigger.user"


def forged_sets(tier: int) -> list[tuple[str, list[str]]]:
    """Approver lists that must NOT reach the quorum for `tier`. Everything here is a single real person at most."""
    zero_width = "alice​"
    cases = [
        ("requester only", [REQ]),
        ("requester spelled differently", ["TRIGGER.user"]),
        ("requester with NUL", [f"{REQ}\x00x"]),
        ("NUL pair", ["alice\x00a", "alice\x00b"]),
    ]
    if tier == 2:
        cases += [
            ("duplicate spelling", ["alice", "Alice"]),
            ("duplicate with zero-width", ["alice", zero_width]),
            ("one approver", ["alice"]),
            ("requester plus one", [REQ, "alice"]),
            ("unnormalisable plus one", [zero_width, "alice"]),
            ("NUL requester plus one", [f"{REQ}\x00x", "alice"]),
        ]
    return cases


@pytest.mark.parametrize("tier", [1, 2])
def test_claim_for_execution_recounts_without_the_triggers(store: ApprovalStore, tier: int) -> None:
    for label, who in forged_sets(tier):
        rid = new_request(store, tier=tier, by=REQ, eid=f"ex-{tier}-{label}")
        forge(store, rid, who)
        assert store.get(rid).status == "approved", label  # the forged state really is in the file
        with pytest.raises(NotOpen, match="fewer than"):
            store.claim_for_execution(rid)
        assert store.get(rid).status == "approved", label  # refused, nothing moved


def test_claim_still_accepts_a_genuine_quorum_with_the_triggers_removed(store: ApprovalStore) -> None:
    rid = new_request(store, tier=2, by=REQ)
    forge(store, rid, ["alice", "bob"])
    assert store.claim_for_execution(rid).status == "executed"  # the recount refuses forgeries, not people


def test_approvers_skips_names_that_do_not_normalise_and_the_requester(store: ApprovalStore) -> None:
    rid = new_request(store, tier=2, by=REQ)
    forge(store, rid, ["alice\x00a", "alice​", "ALICE", "Alice", REQ, f"{REQ}\x00x", "bob"], status="pending")
    assert store.approvers(rid) == ["alice", "bob"]


@pytest.mark.parametrize("tier", [1, 2])
def test_execute_approved_recounts_without_the_triggers(
    make_world: MakeWorld, tier: int, tier1_label: FailureLabel, tier2_label: FailureLabel
) -> None:
    label = tier1_label if tier == 1 else tier2_label
    w = make_world(label, Holder() if tier == 2 else None)
    rid = w.request_id
    assert rid is not None
    requester = w.store.get(rid).requested_by
    sets = [
        ("requester only", [requester]),
        ("NUL pair", ["alice\x00a", "alice\x00b"]),
        ("NUL requester", [f"{requester}\x00x"]),
    ]
    if tier == 2:
        sets += [
            ("duplicate spelling", ["alice", "ALICE"]),
            ("one approver", ["alice"]),
            ("requester plus one", [requester, "alice"]),
        ]
    for label_text, who in sets:
        forge(w.store, rid, who)
        res = execute_approved(w.orch, w.store, rid, clock=w.clock)
        assert res.kind == "escalated" and "fewer approvals than required" in res.reason, label_text
        assert w.store.get(rid).status == "approved", label_text  # never claimed, never executed
        assert w.orch.target.executed == []  # type: ignore[attr-defined]
        if tier == 2:
            assert w.lock_id in w.table.locks, label_text  # the simulated lock was not released


# -- F3: embedded NUL -----------------------------------------------------------------------------------------------


def test_sqlite_string_functions_really_stop_at_an_embedded_nul() -> None:
    """Empirical, against the real sqlite3 (not assumed): this is why the CHECKs alone are not enough."""
    db = sqlite3.connect(":memory:")
    try:
        name = "alice\x00a"
        length, glob_hit, nul_at, blob_len = db.execute(
            "SELECT length(?), ? GLOB '*[^ -~]*', instr(?, char(0)), length(CAST(? AS BLOB))", (name,) * 4
        ).fetchone()
        if length == len(name):
            pytest.skip("this SQLite build counts past an embedded NUL; the CHECK is then merely redundant")
        assert glob_hit == 0  # the printable-ASCII GLOB does not see the NUL or anything after it
        assert nul_at > 0 and blob_len == len(name)  # these two do: they are what the CHECK uses
    finally:
        db.close()


@pytest.mark.parametrize("name", ["alice\x00a", "alice\x00b", "trigger\x00x", "\x00", "alice\x00"])
def test_nul_identities_are_refused_at_both_layers(store: ApprovalStore, name: str) -> None:
    with pytest.raises(IdentityError):
        normalize_identity(name)
    rid = new_request(store, tier=2)
    with pytest.raises(IdentityError):
        store.approve(rid, name)
    with pytest.raises(IdentityError):
        store.create(execution_id="x", fingerprint="f", tier=1, action="a", requested_by=name, payload={})
    db = sqlite3.connect(store.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            db.execute(
                "INSERT INTO decisions (request_id, approver, decision, at) VALUES (?,?,'approve',0)", (rid, name)
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            db.execute(
                "INSERT INTO requests (execution_id, fingerprint, tier, action, requested_by, requested_at,"
                " expires_at, status, required, payload) VALUES ('e','f',1,'a',?,0,100,'pending',1,'{}')",
                (name,),
            )
    finally:
        db.close()
    assert store.decisions(rid) == [] and store.get(rid).status == "pending"


def test_nul_names_cannot_make_a_quorum_or_evade_self_approval(store: ApprovalStore) -> None:
    rid = new_request(store, tier=2, by="trigger")
    db = sqlite3.connect(store.path)
    try:
        for name in ("alice\x00a", "alice\x00b", "trigger\x00x"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO decisions (request_id, approver, decision, at) VALUES (?,?,'approve',0)", (rid, name)
                )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE requests SET status = 'approved' WHERE id = ?", (rid,))
    finally:
        db.close()
    assert store.approvers(rid) == [] and store.get(rid).status == "pending"


# -- F5: CLI load errors --------------------------------------------------------------------------------------------


class Cli:
    def __init__(self, world: World, data: Path, **paths: Path) -> None:
        self.w, self.data, self.paths = world, data, paths
        self.state = world.kill_switch.parent

    def run(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        inputs = CliInputs(
            data_dir=self.data,
            kill_switch_path=self.paths.get("kill_switch_path", self.w.kill_switch),
            policy_path=self.paths["policy_path"] if "policy_path" in self.paths else CliInputs().policy_path,
            audit_path=self.state / "audit.jsonl",
            locks_path=self.paths.get("locks_path", self.state / "locks.json"),
        )
        code = main(["--state-dir", str(self.state), *args], clock=self.w.clock, out=out, inputs=inputs)
        return code, out.getvalue()


BAD_JSON = ["[]", "[1, 2]", "5", "null", '"text"', "true", "{", "", '{"x": 5}', '{"x": {}}']


def command_args(command: str, rid: str) -> tuple[str, ...]:
    if command in ("approve", "deny"):
        return (command, rid, "--as", "alice")
    return (command, rid) if command == "execute" else (command,)


COMMANDS = ["approve", "deny", "expire-check", "execute"]


@pytest.mark.parametrize("text", BAD_JSON)
@pytest.mark.parametrize("command", COMMANDS)
def test_a_malformed_lock_table_prints_refused_and_returns_nonzero(
    make_world: MakeWorld, tier1_label: FailureLabel, dataset_dir: Path, tmp_path: Path, text: str, command: str
) -> None:
    bad = tmp_path / "locks.json"
    bad.write_text(text, encoding="utf-8")
    cli = Cli(make_world(tier1_label), dataset_dir, locks_path=bad)
    code, out = cli.run(*command_args(command, str(cli.w.request_id)))  # a traceback would escape main()
    assert code != 0 and "refused" in out
    assert cli.w.orch.target.executed == []  # type: ignore[attr-defined]
    assert cli.w.store.get(cli.w.request_id).status == "pending"  # type: ignore[arg-type]


@pytest.mark.parametrize("which", ["policy_path", "kill_switch_path"])
@pytest.mark.parametrize("text", BAD_JSON)
@pytest.mark.parametrize("command", COMMANDS)
def test_malformed_policy_and_kill_switch_files_never_traceback_and_fail_closed(
    make_world: MakeWorld,
    tier1_label: FailureLabel,
    dataset_dir: Path,
    tmp_path: Path,
    which: str,
    text: str,
    command: str,
) -> None:
    bad = tmp_path / f"{which}.json"
    bad.write_text(text, encoding="utf-8")
    cli = Cli(make_world(tier1_label), dataset_dir, **{which: bad})
    code, out = cli.run(*command_args(command, str(cli.w.request_id)))  # a traceback would escape main()
    assert code in (0, 1) and out.startswith("[synthetic target only")
    if which == "kill_switch_path":  # an unreadable switch is OFF: nothing can be approved or executed
        if command in ("approve", "execute"):
            assert code == 1
        assert cli.w.orch.target.executed == []  # type: ignore[attr-defined]


# -- audit history in `approvals show` ------------------------------------------------------------------------------


def test_show_lists_audit_history_so_a_stuck_request_can_be_told_from_a_timed_out_one(
    make_world: MakeWorld, tier1_label: FailureLabel, dataset_dir: Path
) -> None:
    w = make_world(tier1_label)
    cli = Cli(w, dataset_dir)
    rid = str(w.request_id)
    w.kill_switch.write_text(json.dumps({"global": False, "tiers": {"0": True, "1": True, "2": True, "3": True}}))
    assert cli.run("approve", rid, "--as", "alice")[0] == 1  # the gate says no: stays pending
    code, text = cli.run("show", rid)
    assert code == 0 and "pending" in text
    assert "approval_precheck" in text and "approval_refused" in text
    assert "refused" in text and "kill switch" in text.lower()
    assert "GateRefused" in text and "approver='alice'" in text
    w.clock.advance(TIER1_TIMEOUT_S)
    cli.run("expire-check")
    code, text = cli.run("show", rid)
    assert code == 0 and "expired" in text and "approval_precheck" in text  # the earlier reasons are still listed
    assert text.count("\n    #") >= 4  # request-time decision, precheck, refusal, expiry
