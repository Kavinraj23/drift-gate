"""Integration of N2 `similar_history` and N3 `changes_in_window` into ONE optional-tools mechanism."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from pathlib import Path

import pytest

from driftgate.adapters.synthetic import SyntheticSource
from driftgate.agents.investigator import (
    CHANGES_PROMPT_ADDENDUM,
    SIMILAR_HISTORY_NOTE,
    SYSTEM_PROMPT,
    agent_tool_definitions,
    system_prompt,
)
from driftgate.eval.ground_truth import FailureLabel
from driftgate.eval.scripted_agent import scripted_model
from driftgate.llm.fake import FakeModel
from driftgate.orchestrator import Orchestrator
from driftgate.retrieval import HistoryIndex
from driftgate.tools import OPTIONAL_TOOLS, TOOLS, ToolContext, dispatch, optional_tool_definitions, tool_definitions

BOTH = frozenset({"similar_history", "changes_in_window"})


def _names(optional: object) -> list[str]:
    return [d["name"] for d in agent_tool_definitions(optional)]  # type: ignore[arg-type]


def test_registry_has_both_optional_tools_in_a_fixed_order() -> None:
    assert [t.name for t in OPTIONAL_TOOLS] == ["similar_history", "changes_in_window"]
    assert not {t.name for t in OPTIONAL_TOOLS} & {t.name for t in TOOLS}


def test_both_offered_in_a_stable_order_before_submit_report() -> None:
    base = [d["name"] for d in tool_definitions()]
    expected = [*base, "similar_history", "changes_in_window", "submit_report"]
    for order in itertools.permutations(["similar_history", "changes_in_window"]):
        assert _names(frozenset(order)) == expected
        assert _names(order) == expected  # a tuple of names is accepted and gives the same list


def test_each_alone_offers_only_that_one() -> None:
    base = [d["name"] for d in tool_definitions()]
    assert _names(frozenset({"similar_history"})) == [*base, "similar_history", "submit_report"]
    assert _names(frozenset({"changes_in_window"})) == [*base, "changes_in_window", "submit_report"]


def test_prompt_addenda_only_for_enabled_tools_in_fixed_order() -> None:
    assert system_prompt(frozenset()) == SYSTEM_PROMPT
    assert system_prompt(frozenset({"similar_history"})) == SYSTEM_PROMPT + SIMILAR_HISTORY_NOTE
    assert system_prompt(frozenset({"changes_in_window"})) == SYSTEM_PROMPT + CHANGES_PROMPT_ADDENDUM
    both = SYSTEM_PROMPT + SIMILAR_HISTORY_NOTE + CHANGES_PROMPT_ADDENDUM
    for order in itertools.permutations(["similar_history", "changes_in_window"]):
        assert system_prompt(frozenset(order)) == both
        assert system_prompt(order) == both  # type: ignore[arg-type]


def test_unknown_optional_name_is_an_error() -> None:
    with pytest.raises(ValueError):
        optional_tool_definitions(frozenset({"get_execution"}))
    with pytest.raises(ValueError):
        optional_tool_definitions(frozenset({"nope"}))


def test_dispatch_rejects_every_optional_tool_that_is_not_enabled(dataset_dir: Path) -> None:
    ctx = ToolContext(SyntheticSource(dataset_dir), history=HistoryIndex())
    for name in ("similar_history", "changes_in_window"):
        r = dispatch(ctx, name, {}, "c1")
        assert not r.ok and r.error == f"unknown tool: {name}"
        other = BOTH - {name}
        r = dispatch(ctx, name, {}, "c2", other)
        assert not r.ok and r.error == f"unknown tool: {name}"


def _run(make_orch: Callable[..., Orchestrator], label: FailureLabel, source: SyntheticSource, **kw) -> list:  # type: ignore[no-untyped-def]
    models: list[FakeModel] = []

    def factory(_e: str, _b: object) -> FakeModel:
        models.append(scripted_model(label, source, "correct"))
        return models[-1]

    make_orch(label, model_factory=factory, **kw).handle(label.execution_id)
    return [r for m in models for r in m.requests]


def test_orchestrator_setting_is_a_single_pass_through(
    make_orch: Callable[..., Orchestrator], pick: Callable[..., FailureLabel], dataset_dir: Path
) -> None:
    label = pick("transient_throttling", fleet=False)
    source = SyntheticSource(dataset_dir)
    for enabled in (BOTH, frozenset({"similar_history"}), frozenset({"changes_in_window"}), frozenset()):
        kw: dict = {"optional_tools": enabled}
        if "similar_history" in enabled:
            kw["history"] = HistoryIndex()
        orch = make_orch(label, **kw)
        assert orch._optional_tools == enabled
        requests = _run(make_orch, label, source, **kw)
        assert requests
        for r in requests:
            assert r.tools == agent_tool_definitions(enabled)
            assert r.system == system_prompt(enabled)


def test_alias_offer_similar_history_equals_the_set(
    make_orch: Callable[..., Orchestrator], pick: Callable[..., FailureLabel]
) -> None:
    label = pick("transient_throttling", fleet=False)
    orch = make_orch(
        label, history=HistoryIndex(), offer_similar_history=True, optional_tools=frozenset({"changes_in_window"})
    )
    assert orch._optional_tools == BOTH


def test_similar_history_needs_an_index_and_unknown_names_are_refused(
    make_orch: Callable[..., Orchestrator], pick: Callable[..., FailureLabel]
) -> None:
    label = pick("transient_throttling", fleet=False)
    with pytest.raises(ValueError, match="history index"):
        make_orch(label, optional_tools=frozenset({"similar_history"}))
    with pytest.raises(ValueError, match="not optional"):
        make_orch(label, optional_tools=frozenset({"get_execution"}))


def test_bound_case_is_set_whenever_similar_history_is_enabled_alongside_changes(dataset_dir: Path) -> None:
    from driftgate.agents.investigator import Investigator
    from driftgate.llm.budget import InvestigationBudget

    inv = Investigator(
        FakeModel([]),
        ToolContext(SyntheticSource(dataset_dir), history=HistoryIndex()),
        InvestigationBudget(),
        optional_tools=BOTH,
    )
    assert inv._optional == BOTH
