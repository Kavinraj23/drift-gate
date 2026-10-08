"""Pre-filter: governance outcomes and aborts close with a valid Report and zero model calls."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from driftgate.adapters.synthetic import SyntheticSource
from driftgate.domain import Execution, Node
from driftgate.eval.ground_truth import load_ground_truth
from driftgate.prefilter import ABORT_STATUSES, GOVERNANCE_STATUSES, prefilter

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"


def _validator() -> Draft202012Validator:
    remediation = json.loads((CONTRACTS / "remediation.schema.json").read_text(encoding="utf-8"))
    report = json.loads((CONTRACTS / "report.schema.json").read_text(encoding="utf-8"))
    registry = Registry().with_resource("remediation.schema.json", Resource.from_contents(remediation))
    return Draft202012Validator(report, registry=registry)


def _execution(status: str, node_status: str = "success") -> Execution:
    leaf = Node("n2", "Manual approval", node_status, "n1")
    return Execution("ex-1", "infra-db", status, "2026-09-08T00:00:00Z", root=Node("n0", "p", status, None, [leaf]))


def test_generated_approval_rejections_close_without_a_model(dataset_dir: Path) -> None:
    truth = load_ground_truth(dataset_dir)
    src = SyntheticSource(dataset_dir)
    gov = [e for e, f in truth.failures.items() if f.disposition == "close"]
    assert len(gov) == 3
    validator = _validator()
    for eid in gov:
        result = prefilter(src.get_execution(eid))
        assert result.closed and result.report is not None
        r = result.report
        assert (r.classification, r.layer) == ("governance", "L5")
        assert r.remediation is None and not r.abstained and r.run.tool_calls == 0
        assert r.run.input_tokens == r.run.output_tokens == 0 and r.run.cost_usd == 0
        assert r.escalation_reason.startswith("closed by pre-filter")
        validator.validate(json.loads(json.dumps(r.to_dict())))
        assert r.fingerprint == prefilter(src.get_execution(eid)).report.fingerprint  # type: ignore[union-attr]


@pytest.mark.parametrize("status", sorted(GOVERNANCE_STATUSES))
def test_governance_statuses_close_as_l5(status: str) -> None:
    r = prefilter(_execution(status, "rejected")).report
    assert r is not None and (r.classification, r.layer) == ("governance", "L5")


@pytest.mark.parametrize("status", sorted(ABORT_STATUSES))
def test_user_aborts_close_without_remediation(status: str) -> None:
    result = prefilter(_execution(status, "aborted"))
    assert result.closed and result.report is not None
    assert result.report.classification == "user" and result.report.remediation is None
    _validator().validate(result.report.to_dict())


@pytest.mark.parametrize("status", ["failed", "success", "running"])
def test_other_statuses_are_not_closed(status: str) -> None:
    result = prefilter(_execution(status))
    assert not result.closed and result.report is None


def test_keyed_on_status_not_on_log_text() -> None:
    """A failed execution whose node says 'rejected' is still not closed: only the execution status counts."""
    assert not prefilter(_execution("failed", "rejected")).closed


def test_prefilter_has_no_path_to_a_model() -> None:
    tree = ast.parse((ROOT / "src" / "driftgate" / "prefilter.py").read_text(encoding="utf-8"))
    imported = {
        n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in (n.names if isinstance(n, ast.Import) else [None])
    }
    assert not any(m and (m.startswith("driftgate.llm") or m.split(".")[0] == "anthropic") for m in imported)
