"""Phase 2 POC: the full investigate -> classify -> remediate -> verify loop against a
REAL GitHub Actions execution, not the synthetic generator (TASKS.md "never cut" scope).

Triggers .github/workflows/demo-failure.yml (fails on attempt 1, succeeds on any
rerun), waits for the real failure, pulls it through GitHubActionsSource, runs it
through the same deterministic classifier used on the synthetic population, and - if
it resolves to a Tier 0 action, as this signature does - routes it through the Phase 3
SafetyGate (drift_gate.gates), which is what actually calls the real target now, not
this script directly. Every proposal - executed or not - lands in data/audit_log.jsonl.

Usage: python -m scripts.run_github_demo
Requires GITHUB_TOKEN in the environment (loaded from .env), scoped to Actions
read+write on the target repo.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict

from dotenv import load_dotenv

load_dotenv()

from drift_gate.classifier import classify, to_report
from drift_gate.gates import SafetyGate
from drift_gate.github_actions.client import GitHubClient
from drift_gate.github_actions.source import GitHubActionsSource
from drift_gate.github_actions.target import GitHubActionsTarget

OWNER, REPO = "Kavinraj23", "drift-gate"
WORKFLOW_FILE = "demo-failure.yml"
POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 180


def _report_to_json(report) -> dict:
    d = asdict(report)
    d["classification"] = report.classification.value
    d["layer"] = report.layer.value if report.layer else None
    if report.remediation:
        d["remediation"]["tier"] = report.remediation.tier.value
        d["remediation"]["gate"] = report.remediation.gate.value
    return d


def _wait_for(predicate, description: str):
    deadline = time.time() + POLL_TIMEOUT_S
    while time.time() < deadline:
        result = predicate()
        if result is not None:
            return result
        time.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"timed out waiting for {description}")


def main() -> None:
    client = GitHubClient(OWNER, REPO)
    source = GitHubActionsSource(OWNER, REPO, client)
    target = GitHubActionsTarget(OWNER, REPO, client)
    gate = SafetyGate(target)

    print(f"Dispatching {WORKFLOW_FILE} on {OWNER}/{REPO}...")
    client.post(f"/actions/workflows/{WORKFLOW_FILE}/dispatches", {"ref": "main"})

    def _find_new_run():
        runs = client.get_all_pages(
            "/actions/workflows/{}/runs".format(WORKFLOW_FILE), "workflow_runs",
            params={"per_page": 1},
        )
        if not runs:
            return None
        run = runs[0]
        return run["id"] if run["status"] == "completed" else None

    run_id = str(_wait_for(_find_new_run, "the dispatched run to complete"))
    print(f"Run {run_id} completed (failed on attempt 1, as designed).")

    print("\n--- Deterministic classifier (real execution, zero LLM calls) ---")
    det = classify(source, run_id)
    print(f"fingerprint={det.fingerprint} classification={det.classification} "
          f"resolved={det.resolved} matched_fault_id={det.matched_fault_id}")

    if not det.resolved:
        print("Not resolved deterministically - would escalate to the agent loop.")
        sys.exit(0)

    report = to_report(det)
    print(json.dumps(_report_to_json(report), indent=2, default=str))

    print("\n--- Routing through the Phase 3 SafetyGate (rate limit, kill switch, "
          "abort ceiling, audit trail) ---")
    result = gate.propose_and_execute(report)
    if result is None:
        print("Gate rejected the remediation - see data/audit_log.jsonl for why. "
              "Not executing; caller should hard-escalate per PRD SS5.")
        sys.exit(0)
    print("execute:", result.detail)

    print("\n--- Verifying against the real re-run ---")

    def _check_verified():
        v = target.verify(report.remediation)
        return v if v.resolved or "running" not in v.detail else None

    verification = _wait_for(_check_verified, "the retry to complete")
    print(f"resolved={verification.resolved} detail={verification.detail}")


if __name__ == "__main__":
    main()
