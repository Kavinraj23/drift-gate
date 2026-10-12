"""`python tasks.py approvals ...`: the operator CLI, driven in-process on a manual clock (no subprocess, no sleep)."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from driftgate.adapters.synthetic import LockEntry, LockTable, save_lock_table
from driftgate.approvals import TIER1_TIMEOUT_S
from driftgate.approvals_cli import BANNER, main
from driftgate.eval.ground_truth import FailureLabel
from driftgate.eval.tier12_cases import Holder, World

MakeWorld = Callable[..., World]


class Cli:
    def __init__(self, world: World, data: Path) -> None:
        self.w, self.data = world, data
        self.state = world.kill_switch.parent  # the world's files are laid out like a state dir

    def run(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        code = main(
            ["--state-dir", str(self.state), "--data", str(self.data), "--kill-switch", str(self.w.kill_switch), *args],
            clock=self.w.clock,
            out=out,
        )
        return code, out.getvalue()


@pytest.fixture
def tier1_cli(make_world: MakeWorld, tier1_label: FailureLabel, dataset_dir: Path) -> Cli:
    return Cli(make_world(tier1_label), dataset_dir)


def tier2_cli(make_world: MakeWorld, label: FailureLabel, dataset_dir: Path, *, alive: bool = False) -> Cli:
    w = make_world(label, Holder())
    # The CLI reads the shared lock table file and the dataset's executions: point the lock at a real finished one.
    save_lock_table(LockTable({w.lock_id: LockEntry("ex-000001", alive)}), w.kill_switch.parent / "locks.json")
    return Cli(w, dataset_dir)


def test_every_command_says_it_is_synthetic_only_and_unauthenticated(tier1_cli: Cli) -> None:
    code, text = tier1_cli.run("list")
    assert code == 0 and BANNER in text
    assert "synthetic target only" in BANNER and "unauthenticated" in BANNER


def test_help_states_the_trust_model(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "synthetic target only" in help_text and "unauthenticated" in help_text


def test_approve_and_deny_require_an_explicit_identity(tier1_cli: Cli, capsys: pytest.CaptureFixture[str]) -> None:
    rid = str(tier1_cli.w.request_id)
    for cmd in ("approve", "deny"):
        with pytest.raises(SystemExit) as e:
            tier1_cli.run(cmd, rid)
        assert e.value.code == 2
    assert "--as" in capsys.readouterr().err
    code, text = tier1_cli.run("approve", rid, "--as", "   ")
    assert code == 1 and "identity is required" in text


def test_list_and_show(tier1_cli: Cli) -> None:
    rid = tier1_cli.w.request_id
    code, text = tier1_cli.run("list")
    assert code == 0 and "pending" in text and "1 request(s)" in text
    tier1_cli.run("approve", str(rid), "--as", "Alice")
    code, text = tier1_cli.run("show", str(rid))
    assert code == 0 and "approve" in text and "alice" in text and "approved" in text
    assert tier1_cli.run("list", "--status", "pending")[1].endswith("0 request(s)\n")
    assert tier1_cli.run("show", "999")[0] == 1


def test_tier1_end_to_end_through_the_cli(tier1_cli: Cli) -> None:
    rid = str(tier1_cli.w.request_id)
    code, text = tier1_cli.run("approve", rid, "--as", "alice")
    assert code == 0 and "approved" in text and "execute" in text
    code, text = tier1_cli.run("execute", rid)
    assert code == 0 and "executed" in text
    assert tier1_cli.run("execute", rid)[0] == 1  # a second execute is refused
    stages = [(e.kind, e.payload.get("stage")) for e in tier1_cli.w.orch.audit.read()]
    assert ("gate_decision", "approval_recheck") in stages and ("execution", "execute") in stages


def test_self_approval_and_approval_after_denial_are_refused_with_exit_1(tier1_cli: Cli) -> None:
    rid = str(tier1_cli.w.request_id)
    code, text = tier1_cli.run("approve", rid, "--as", " TRIGGER.user ")
    assert code == 1 and "cannot approve" in text
    tier1_cli.run("deny", rid, "--as", "bob")
    code, text = tier1_cli.run("approve", rid, "--as", "alice")
    assert code == 1 and "not pending" in text
    assert tier1_cli.run("execute", rid)[0] == 1


def test_execute_before_approval_is_refused(tier1_cli: Cli) -> None:
    code, text = tier1_cli.run("execute", str(tier1_cli.w.request_id))
    assert code == 1 and "pending" in text and tier1_cli.w.orch.target.executed == []  # type: ignore[attr-defined]


def test_expire_check_expires_overdue_requests_and_they_never_execute(tier1_cli: Cli) -> None:
    rid = str(tier1_cli.w.request_id)
    tier1_cli.w.clock.advance(TIER1_TIMEOUT_S)
    code, text = tier1_cli.run("expire-check")
    assert code == 0 and "1 request(s) expired" in text and "escalated to a human" in text
    assert tier1_cli.run("approve", rid, "--as", "alice")[0] == 1
    assert tier1_cli.run("execute", rid)[0] == 1


def test_a_kill_switch_flipped_before_execute_blocks_it(tier1_cli: Cli) -> None:
    rid = str(tier1_cli.w.request_id)
    tier1_cli.run("approve", rid, "--as", "alice")
    tier1_cli.w.kill_switch.write_text(json.dumps({"global": False, "tiers": {"1": True}}))
    code, text = tier1_cli.run("execute", rid)
    assert code == 1 and "kill switch" in text


def test_tier2_needs_two_people_through_the_cli_and_releases_the_simulated_lock(
    make_world: MakeWorld, tier2_label: FailureLabel, dataset_dir: Path
) -> None:
    cli = tier2_cli(make_world, tier2_label, dataset_dir)
    rid = str(cli.w.request_id)
    assert cli.run("approve", rid, "--as", "alice")[0] == 0
    assert cli.run("approve", rid, "--as", "ALICE")[0] == 1
    assert cli.run("execute", rid)[0] == 1  # one approver is not enough
    assert cli.run("approve", rid, "--as", "bob")[0] == 0
    code, text = cli.run("execute", rid)
    assert code == 0 and "executed" in text
    assert json.loads((cli.state / "locks.json").read_text()) == {}  # the simulated lock is gone, persisted


def test_tier2_with_a_live_holder_in_the_lock_file_is_refused(
    make_world: MakeWorld, tier2_label: FailureLabel, dataset_dir: Path
) -> None:
    cli = tier2_cli(make_world, tier2_label, dataset_dir)
    rid = str(cli.w.request_id)
    cli.run("approve", rid, "--as", "alice")
    cli.run("approve", rid, "--as", "bob")
    save_lock_table(LockTable({cli.w.lock_id: LockEntry("ex-000001", True)}), cli.state / "locks.json")  # went live
    code, text = cli.run("execute", rid)
    assert code == 1 and "running" in text
    assert cli.w.lock_id in json.loads((cli.state / "locks.json").read_text())  # the lock was not touched
