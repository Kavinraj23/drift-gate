"""The PRD's shipping rule for similar_history, as a pure function."""

from __future__ import annotations

from dataclasses import replace

from driftgate.eval.metrics import Rate
from driftgate.eval.retrieval_eval import MIN_CASES, NO_EVIDENCE, ArmStats, shipping_verdict

BASE = ArmStats(
    recall=Rate(8, 14),
    success=Rate(8, 10),
    false_remediation=Rate(1, 33),
    escalation_precision=Rate(18, 19),
    mean_cost_usd=0.010,
)


def test_without_recorded_evidence_it_always_stays_off_even_if_the_numbers_look_great() -> None:
    great = replace(BASE, recall=Rate(14, 14), success=Rate(14, 14), mean_cost_usd=0.001)
    v = shipping_verdict(BASE, great, recorded=False)
    assert not v.enabled_by_default and v.reason == NO_EVIDENCE == "stays off: no recorded evidence"


def test_a_clear_recall_gain_with_no_extra_false_remediation_enables_it() -> None:
    on = replace(BASE, recall=Rate(8 + MIN_CASES, 14))
    v = shipping_verdict(BASE, on, recorded=True)
    assert v.enabled_by_default and "recall" in v.reason


def test_a_one_or_two_case_gain_is_noise() -> None:
    for gain in (1, 2):
        on = replace(BASE, recall=Rate(8 + gain, 14), success=Rate(8 + gain, 10 + gain))
        v = shipping_verdict(BASE, on, recorded=True)
        assert not v.enabled_by_default and "noise" in v.reason


def test_more_false_remediation_vetoes_any_gain() -> None:
    on = replace(BASE, recall=Rate(14, 14), false_remediation=Rate(2, 33))
    v = shipping_verdict(BASE, on, recorded=True)
    assert not v.enabled_by_default and "false remediation" in v.reason


def test_a_verified_fix_gain_enables_it_unless_the_success_rate_falls() -> None:
    better = replace(BASE, success=Rate(8 + MIN_CASES, 10 + MIN_CASES))
    assert shipping_verdict(BASE, better, recorded=True).enabled_by_default
    worse_rate = replace(BASE, success=Rate(8 + MIN_CASES, 20))
    assert not shipping_verdict(BASE, worse_rate, recorded=True).enabled_by_default


def test_a_real_cost_drop_without_losing_recall_enables_it() -> None:
    cheaper = replace(BASE, mean_cost_usd=0.0085)
    assert shipping_verdict(BASE, cheaper, recorded=True).enabled_by_default
    slightly = replace(BASE, mean_cost_usd=0.0095)
    assert not shipping_verdict(BASE, slightly, recorded=True).enabled_by_default
    loses_recall = replace(BASE, mean_cost_usd=0.005, recall=Rate(7, 14))
    assert not shipping_verdict(BASE, loses_recall, recorded=True).enabled_by_default


def test_no_change_stays_off() -> None:
    v = shipping_verdict(BASE, BASE, recorded=True)
    assert not v.enabled_by_default
