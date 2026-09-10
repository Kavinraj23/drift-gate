"""Demo: the full investigate -> hypothesize -> remediate loop against the synthetic
population. Runs the deterministic classifier first (real, cheap, zero LLM calls) and
only escalates genuinely unresolved cases to the LLM agent loop - mirrors PRD.md SS8's
cheapest-first, early-exit design.

Usage: python -m scripts.run_agent_demo
Requires ANTHROPIC_API_KEY in the environment for the agent-loop cases; the
deterministic-only case still runs and prints without it.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from drift_gate.agent.loop import run_agent
from drift_gate.classifier import classify, to_report
from drift_gate.dataset_io import load_ground_truth
from drift_gate.synthetic.source import SyntheticSource

DATA_DIR = Path("data/synthetic")

# Hand-picked to show the whole story end to end:
#  - a clean signature that resolves deterministically, zero LLM calls
#  - a Tier 3 fix where the agent has to draft real PR content
#  - a fleet-correlated burst the agent has to synthesize into one report
#  - a genuinely ambiguous case where the correct answer is to decline to act
DEMO_FAULT_IDS = {
    "registry_429": "deterministic-only: clean Tier 0 retry signature",
    "lockfile_mismatch": "agent: Tier 3 PR content the agent must draft",
    "expired_credential": "agent: fleet-correlated burst",
    # not oom_killed: that fault only lands on the two flaky pipelines, where an
    # unrelated later success can coincidentally trip the flake-check window.
    "assume_role_denied": "agent: no safe auto-remediation, agent should abstain",
}


def _report_to_json(report) -> dict:
    d = asdict(report)
    d["classification"] = report.classification.value
    d["layer"] = report.layer.value if report.layer else None
    if report.remediation:
        d["remediation"]["tier"] = report.remediation.tier.value
        d["remediation"]["gate"] = report.remediation.gate.value
    return d


def pick_demo_executions(data_dir: Path) -> dict[str, str]:
    gts = load_ground_truth(data_dir)
    picked: dict[str, str] = {}
    for gt in gts:
        fid = gt["injected_fault_id"]
        if fid in DEMO_FAULT_IDS and fid not in picked:
            picked[fid] = gt["execution_id"]
        if len(picked) == len(DEMO_FAULT_IDS):
            break
    return picked


def main() -> None:
    have_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not have_key:
        print("ANTHROPIC_API_KEY is not set - agent-loop cases will be skipped; "
              "the deterministic-only case still runs.", file=sys.stderr)

    source = SyntheticSource(DATA_DIR)
    picked = pick_demo_executions(DATA_DIR)

    for fault_id, exec_id in picked.items():
        print(f"\n{'=' * 70}\n{fault_id}: {DEMO_FAULT_IDS[fault_id]}\n"
              f"execution: {exec_id}\n{'=' * 70}")
        det = classify(source, exec_id)
        print(f"[deterministic] fingerprint={det.fingerprint} "
              f"classification={det.classification} resolved={det.resolved} "
              f"escalate_reason={det.escalate_reason}")

        if det.resolved:
            report = to_report(det)
            print("-> resolved deterministically, zero LLM calls")
            print(json.dumps(_report_to_json(report), indent=2, default=str))
            continue

        if not have_key:
            print("-> would escalate to agent, but no API key set; skipping")
            continue

        report, _ = run_agent(source, exec_id, det)
        print("-> escalated to agent")
        print(json.dumps(_report_to_json(report), indent=2, default=str))


if __name__ == "__main__":
    main()
