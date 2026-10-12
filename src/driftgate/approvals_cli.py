"""Operator CLI for Tier 1/2 approvals: `python tasks.py approvals <list|show|approve|deny|expire-check|execute>`.

SYNTHETIC TARGET ONLY. Execution is simulated against the synthetic lock table; nothing here can reach live
infrastructure or GitHub. Identities (`--as`) are plain strings typed by the operator: this is a trusted local
operator interface, NOT authentication. It prevents accidents (the same name twice, the trigger approving their own
run), not impersonation. A real approval channel needs real authentication first.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from driftgate.adapters.synthetic import load_lock_table, save_lock_table
from driftgate.approval_flow import ApprovalService, execute_approved
from driftgate.approvals import ApprovalError, ApprovalRequest, ApprovalStore
from driftgate.audit import AuditLog
from driftgate.gates import DEFAULT_KILL_SWITCH_PATH
from driftgate.orchestrator import build_synthetic_orchestrator, make_synthetic_target
from driftgate.policy import DEFAULT_POLICY_PATH

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_DIR = ROOT / "state"
BANNER = "[synthetic target only: actions are simulated. Identities are unauthenticated: trusted local operator CLI]"

Clock = Callable[[], float]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python tasks.py approvals",
        description="Approve, deny, expire and execute Tier 1/2 remediations. " + BANNER,
    )
    p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="holds approvals.sqlite and audit.jsonl")
    p.add_argument("--data", type=Path, default=ROOT / "data", help="synthetic dataset (execution source)")
    p.add_argument(
        "--locks", type=Path, default=None, help="simulated lock table JSON (default <state-dir>/locks.json)"
    )
    p.add_argument("--audit", type=Path, default=None, help="audit log (default <state-dir>/audit.jsonl)")
    p.add_argument("--kill-switch", type=Path, default=DEFAULT_KILL_SWITCH_PATH)
    p.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    sub = p.add_subparsers(dest="command", required=True)
    lst = sub.add_parser("list", help="list requests")
    lst.add_argument("--status", default=None)
    show = sub.add_parser("show", help="show one request with its decisions")
    show.add_argument("id", type=int)
    for name, text in (("approve", "approve a pending request"), ("deny", "deny a pending request")):
        c = sub.add_parser(name, help=text)
        c.add_argument("id", type=int)
        c.add_argument("--as", dest="identity", required=True, help="who you are (required; there is no default)")
    sub.add_parser("expire-check", help="expire overdue requests (they escalate to a human, never execute)")
    ex = sub.add_parser("execute", help="re-run the gate and, if allowed, run the approved action (simulated)")
    ex.add_argument("id", type=int)
    return p


def _line(r: ApprovalRequest) -> str:
    return (
        f"{r.id:>4}  {r.status:<9} T{r.tier} {r.action:<24} {r.execution_id}  "
        f"by {r.requested_by}  expires {r.expires_at:.0f}"
    )


def _no_model(_execution_id: str, _budget: object) -> object:
    raise RuntimeError("the approvals CLI never calls a model")


def main(argv: Sequence[str] | None = None, *, clock: Clock = time.time, out: TextIO | None = None) -> int:
    stream = out or sys.stdout
    args = _parser().parse_args(list(argv) if argv is not None else None)

    def say(text: str = "") -> None:
        print(text, file=stream)

    say(BANNER)
    state: Path = args.state_dir
    audit_path: Path = args.audit or state / "audit.jsonl"
    locks_path: Path = args.locks or state / "locks.json"
    store = ApprovalStore(state / "approvals.sqlite", clock)
    try:
        audit = AuditLog(audit_path, clock)
        service = ApprovalService(store, audit, clock)
        try:
            return _run(args, store, service, audit_path, locks_path, clock, say)
        except ApprovalError as e:
            say(f"refused: {e}")
            return 1
    finally:
        store.close()


def _run(
    args: argparse.Namespace,
    store: ApprovalStore,
    service: ApprovalService,
    audit_path: Path,
    locks_path: Path,
    clock: Clock,
    say: Callable[[str], None],
) -> int:
    cmd = args.command
    if cmd == "list":
        rows = store.list(args.status)
        for r in rows:
            say(_line(r))
        say(f"{len(rows)} request(s)")
        return 0
    if cmd == "show":
        r = store.get(args.id)
        say(_line(r))
        say(f"  fingerprint {r.fingerprint}; needs {r.required} approval(s) from people other than {r.requested_by}")
        for d in store.decisions(r.id):
            say(f"  {d.decision:<7} {d.approver} at {d.at:.0f}")
        if r.note:
            say(f"  note: {r.note}")
        return 0
    if cmd in ("approve", "deny"):
        r = service.approve(args.id, args.identity) if cmd == "approve" else service.deny(args.id, args.identity)
        have = len(store.approvers(r.id))
        say(f"{cmd}d by {args.identity.strip()}: request {r.id} is now {r.status} ({have} of {r.required} approvals)")
        if r.status == "approved":
            say(f"run: python tasks.py approvals execute {r.id}")
        return 0
    if cmd == "expire-check":
        expired = service.expire_check()
        for r in expired:
            say(f"expired: {_line(r)}  -> escalated to a human; not executed")
        say(f"{len(expired)} request(s) expired")
        return 0
    if cmd == "execute":
        locks = load_lock_table(locks_path)
        target = make_synthetic_target(lock_table=locks)
        orch = build_synthetic_orchestrator(
            args.data,
            _no_model,  # type: ignore[arg-type]
            audit_path=audit_path,
            clock=clock,
            kill_switch_path=args.kill_switch,
            policy_path=args.policy,
            target=target,
            approvals=store,
            locks=locks,
        )
        res = execute_approved(orch, store, args.id, clock=clock)
        say(f"request {res.request_id}: {res.kind} ({res.status}): {res.reason}")
        if res.kind == "executed":
            save_lock_table(locks, locks_path)  # the simulated world after the unlock
            return 0
        return 1
    return 2  # unreachable: argparse requires a command


if __name__ == "__main__":
    raise SystemExit(main())
