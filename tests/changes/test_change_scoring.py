"""N3 scoring math: suspected-change accuracy and the decoy blame rate, kept separate."""

from __future__ import annotations

from pathlib import Path

from driftgate.domain import SuspectedChange
from driftgate.eval import report as rpt
from driftgate.eval.change_score import ChangeCase, change_case, compute_change_metrics, resolve_change

TIMELINE = [
    {"change_id": "chg-0001", "at": "2026-03-10T11:00:00Z", "kind": "commit", "ref": "p", "sha": "a" * 40},
    {"change_id": "chg-0002", "at": "2026-03-10T11:30:00Z", "kind": "template_release", "ref": "tpl", "sha": ""},
    {"change_id": "chg-0003", "at": "2026-03-10T11:45:00Z", "kind": "runner_pool_change", "ref": "pool", "sha": ""},
]


def sc(ref: str, kind: str = "commit", at: str = "") -> SuspectedChange:
    return SuspectedChange(kind, ref, at, "basis")


def case(causal: tuple[str, ...], decoy: tuple[str, ...], named: str | None, named_any: bool = True) -> ChangeCase:
    return ChangeCase("s", False, causal, decoy, named_any, named)


def test_resolve_by_change_id_sha_prefix_and_triple() -> None:
    assert resolve_change(sc("chg-0002"), TIMELINE) == "chg-0002"
    assert resolve_change(sc("a" * 40), TIMELINE) == "chg-0001"
    assert resolve_change(sc("a" * 7), TIMELINE) == "chg-0001"
    assert resolve_change(sc("aaa"), TIMELINE) is None  # too short a prefix to mean anything
    assert resolve_change(sc("pool", "runner_pool_change", "2026-03-10T11:45:00Z"), TIMELINE) == "chg-0003"
    assert resolve_change(sc("nothing"), TIMELINE) is None
    assert resolve_change(None, TIMELINE) is None


def test_accuracy_counts_over_cases_with_a_true_cause_and_misses_unnamed() -> None:
    m = compute_change_metrics(
        [
            case(("c1",), (), "c1"),  # right
            case(("c2",), ("d",), "d"),  # blamed a decoy
            case(("c3",), (), None, named_any=False),  # named nothing: a miss
            case((), ("d",), "d"),  # no true cause: not in the accuracy denominator
        ]
    )
    assert (m.suspected_change_accuracy.numerator, m.suspected_change_accuracy.denominator) == (1, 3)


def test_decoy_blame_is_over_reports_that_named_a_change_and_is_separate_from_accuracy() -> None:
    m = compute_change_metrics(
        [
            case(("c1",), ("d",), "c1"),
            case(("c2",), ("d",), "d"),
            case(("c3",), ("d",), "d"),
            case(("c4",), ("d",), None, named_any=False),  # named nothing: not in the blame denominator
            case(("c5",), ("d",), "zzz"),  # named something neither causal nor decoy
            case(("c6",), ("d",), None),  # named a reference that resolves to nothing
        ]
    )
    assert (m.decoy_blame_rate.numerator, m.decoy_blame_rate.denominator) == (2, 5)
    assert (m.suspected_change_accuracy.numerator, m.suspected_change_accuracy.denominator) == (1, 6)
    assert m.named_other == 2


def test_a_change_that_is_both_causal_and_decoy_is_never_blame() -> None:
    m = compute_change_metrics([case(("x",), ("x",), "x")])
    assert m.decoy_blame_rate.numerator == 0 and m.suspected_change_accuracy.numerator == 1


def test_zero_denominators_are_none_not_zero() -> None:
    m = compute_change_metrics([])
    assert m.suspected_change_accuracy.denominator == 0 and m.suspected_change_accuracy.value is None
    assert m.decoy_blame_rate.denominator == 0 and m.decoy_blame_rate.value is None
    m2 = compute_change_metrics([case((), (), None, named_any=False)])
    assert m2.suspected_change_accuracy.denominator == 0 and m2.decoy_blame_rate.denominator == 0
    assert rpt.fmt(m2.decoy_blame_rate) == "n/a (n=0)"


def test_change_case_resolves_from_the_timeline() -> None:
    c = change_case("s1", True, ["chg-0001"], ["chg-0003"], sc("chg-0003"), TIMELINE)
    assert c.held_out and c.named and c.named_id == "chg-0003"
    assert change_case("s2", False, [], [], None, TIMELINE).named is False


# --- the offline eval report -------------------------------------------------------------------------------------


def test_report_carries_the_labelled_n3_rows_and_leaves_the_mvp1_table_alone(dataset_dir: Path) -> None:
    data = rpt.build_report(dataset_dir, mode="fake", with_drills=False)
    md = rpt.render_markdown(data)
    assert "## Change correlation (N3, additional rows)" in md
    assert "Suspected-change accuracy" in md and "Decoy blame rate" in md
    assert "validate the metric code only" in md
    # scripted model names no change: accuracy has applicable cases (0 hits), blame has none -> plain n/a
    assert "n/a (n=0)" in md.split("Change correlation (N3")[1].split("## ")[0]
    js = rpt.to_json(data)
    assert js["change_correlation"]["dev"]["agent"]["decoy_blame_rate"]["denominator"] == 0
    assert js["change_correlation"]["dev"]["agent"]["suspected_change_accuracy"]["denominator"] > 0
    # the table embedded in the README is built from the unchanged MVP 1 rows
    assert "Suspected-change" not in rpt.readme_tables(data)
