"""Holder-death proof: derived from the log, the lock table and the execution source, never from the model."""

from __future__ import annotations

from pathlib import Path

import pytest

from driftgate.adapters.synthetic import LockEntry, LockTable
from driftgate.domain import Execution, SourceError
from driftgate.lock_proof import derive_holder_proof, extract_lock_id

LOCK = "eac527e5-bc46-7776-8d95-e9666f260927"
BLOCK = f"Error: Error acquiring the state lock\nLock Info:\n  ID:        {LOCK}\n  Path:      s/terraform.tfstate\n"


class Source:
    def __init__(self, statuses: dict[str, str]) -> None:
        self._s = statuses

    def get_execution(self, execution_id: str) -> Execution:
        if execution_id not in self._s:
            raise SourceError(f"unknown execution: {execution_id}")
        return Execution(execution_id, "p", self._s[execution_id], "2026-01-01T00:00:00Z")


def table(holder: str = "h1", alive: bool = False) -> LockTable:
    return LockTable({LOCK: LockEntry(holder, alive)})


def test_lock_id_is_extracted_with_and_without_ansi() -> None:
    assert extract_lock_id(BLOCK) == LOCK
    assert extract_lock_id(BLOCK.replace("ID:", "\x1b[31mID:\x1b[0m")) == LOCK


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no lock here",
        "Lock Info:\n  ID: short",
        BLOCK + BLOCK.replace(LOCK, "11111111-2222-3333-4444-555555555555"),
    ],
)
def test_no_or_ambiguous_lock_id_is_none(text: str) -> None:
    assert extract_lock_id(text) is None


@pytest.mark.parametrize("status", ["success", "failed", "approval_rejected"])
def test_a_terminal_holder_execution_with_a_matching_lock_is_dead_and_proven(status: str) -> None:
    p = derive_holder_proof(LOCK, table(), Source({"h1": status}))
    assert (p.status, p.proven, p.holder_execution_id) == ("dead", True, "h1")


@pytest.mark.parametrize("status", ["running", "in_progress", "queued", "RUNNING"])
def test_a_running_holder_execution_is_running(status: str) -> None:
    p = derive_holder_proof(LOCK, table(), Source({"h1": status}))
    assert (p.status, p.proven) == ("running", False)


def test_the_table_marking_the_holder_alive_wins_over_a_finished_looking_execution() -> None:
    p = derive_holder_proof(LOCK, table(alive=True), Source({"h1": "failed"}))
    assert (p.status, p.proven) == ("running", False)


@pytest.mark.parametrize(
    "lock_id, locks, source",
    [
        (None, table(), Source({"h1": "failed"})),  # no lock id in the log
        (LOCK, None, Source({"h1": "failed"})),  # no lock table
        (LOCK, LockTable(), Source({"h1": "failed"})),  # lock not in the table
        (LOCK, table(holder=""), Source({"h1": "failed"})),  # entry names no holder
        (LOCK, table(), Source({})),  # holder execution unknown to the source
        (LOCK, table(), Source({"h1": "weird"})),  # not a known terminal status
        ("another-lock-id-1234", table(), Source({"h1": "failed"})),  # a different lock
    ],
)
def test_anything_unproven_is_unknown_never_dead(lock_id, locks, source) -> None:  # type: ignore[no-untyped-def]
    p = derive_holder_proof(lock_id, locks, source)
    assert (p.status, p.proven) == ("unknown", False)


def test_lock_table_round_trips_through_json(tmp_path: Path) -> None:
    from driftgate.adapters.synthetic import load_lock_table, save_lock_table

    save_lock_table(table(alive=True), tmp_path / "s" / "locks.json")
    again = load_lock_table(tmp_path / "s" / "locks.json")
    assert again.locks == table(alive=True).locks and load_lock_table(tmp_path / "none.json").locks == {}
