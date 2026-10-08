"""Report/Remediation schemas validate dataclass output and reject malformed documents."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from driftgate.domain import Evidence, Remediation, Report, Review, SuspectedChange

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"


def _load(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _validator(name: str) -> Draft202012Validator:
    remediation = _load("remediation.schema.json")
    registry = Registry().with_resource("remediation.schema.json", Resource.from_contents(remediation))
    return Draft202012Validator(_load(name), registry=registry)


def _report() -> Report:
    return Report(
        execution_id="e1",
        fingerprint="fp1",
        classification="transient",
        layer="L4",
        confidence=0.7,
        hypothesis="throttled",
        evidence=[Evidence(source="call_1", finding="x", supports="y")],
        suspected_change=SuspectedChange(kind="k", ref="r", at="t", basis="b"),
        remediation=Remediation(
            tier=0,
            action="rerun",
            rationale="r",
            reversible=True,
            gate="auto",
            gate_decision="allowed",
        ),
        review=Review(verdict="approve", comments="ok"),
    )


def test_valid_report_serializes_and_validates() -> None:
    doc = json.loads(json.dumps(_report().to_dict()))
    _validator("report.schema.json").validate(doc)


def test_report_without_optionals_validates() -> None:
    r = _report()
    r.remediation = None
    r.review = None
    r.suspected_change = None
    _validator("report.schema.json").validate(r.to_dict())


@pytest.mark.parametrize(
    "path,value",
    [
        (("classification",), "bogus"),
        (("layer",), "L9"),
        (("remediation", "gate"), "yolo"),
        (("remediation", "gate_decision"), "maybe"),
        (("remediation", "tier"), 7),
        (("review", "verdict"), "meh"),
        (("confidence",), 1.5),
    ],
)
def test_bad_values_fail(path: tuple[str, ...], value: object) -> None:
    doc = copy.deepcopy(_report().to_dict())
    target = doc
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        _validator("report.schema.json").validate(doc)


@pytest.mark.parametrize("field", list(_report().to_dict().keys()))
def test_missing_required_report_field_fails(field: str) -> None:
    doc = copy.deepcopy(_report().to_dict())
    del doc[field]
    with pytest.raises(ValidationError):
        _validator("report.schema.json").validate(doc)


def test_remediation_missing_field_fails() -> None:
    doc = copy.deepcopy(_report().to_dict()["remediation"])
    del doc["gate"]
    with pytest.raises(ValidationError):
        _validator("remediation.schema.json").validate(doc)
