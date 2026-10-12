"""With the tool off, the model sees exactly what it saw in MVP 1, so recorded replay fixtures still replay."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from driftgate.agents.fingerprint import prompt_fingerprint
from driftgate.agents.investigator import SYSTEM_PROMPT, agent_tool_definitions, system_prompt
from driftgate.eval.ground_truth import FailureLabel
from driftgate.eval.scripted_agent import scripted_model
from driftgate.llm.fake import FakeModel
from driftgate.orchestrator import Orchestrator
from driftgate.retrieval import HistoryIndex
from driftgate.tools import TOOLS, optional_tool_definitions, tool_definitions

REPO = Path(__file__).resolve().parents[2]

# Snapshots taken from the MVP 1 code (commit 3e746cb) before N2 changed anything.
MVP1_PROMPT_FINGERPRINT = "88a8278480e61498"
MVP1_TOOL_DEFINITIONS_SHA256 = "29f5b73c94ba03dd34ec07d4379b1501007d39bb4e44ec9d176aee573634ace5"
MVP1_SYSTEM_PROMPT_SHA256 = "dcb1fc33810fd7de677889d1f70c6744e5d370d6c4432331930120bf87a994c6"
MVP1_TOOLS = [
    "get_execution",
    "classify_signature",
    "fleet_correlate",
    "flake_history",
    "get_step_logs",
    "read_repo_file",
    "get_remediation_attempt",
    "submit_report",
]


def _sha(obj: object) -> str:
    canon = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def test_default_tool_definitions_and_prompt_are_the_mvp1_bytes() -> None:
    assert [t["name"] for t in agent_tool_definitions()] == MVP1_TOOLS
    assert _sha(agent_tool_definitions()) == MVP1_TOOL_DEFINITIONS_SHA256
    assert hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest() == MVP1_SYSTEM_PROMPT_SHA256
    assert system_prompt() == SYSTEM_PROMPT == system_prompt(())
    assert prompt_fingerprint() == MVP1_PROMPT_FINGERPRINT


def test_replay_fixtures_manifest_still_matches_the_prompt_fingerprint() -> None:
    manifest = json.loads((REPO / "fixtures" / "replay" / "manifest.json").read_text(encoding="utf-8"))
    recorded = {s["fingerprint"] for s in manifest["scenarios"].values() if "fingerprint" in s}
    assert recorded == {prompt_fingerprint()}


def test_the_optional_tool_is_not_in_the_default_registry_and_is_offered_only_by_name() -> None:
    assert "similar_history" not in {t.name for t in TOOLS}
    assert "similar_history" not in {d["name"] for d in tool_definitions()}
    assert [d["name"] for d in optional_tool_definitions(("similar_history",))] == ["similar_history"]
    with pytest.raises(ValueError):
        optional_tool_definitions(("get_execution",))  # not optional: it is already in the default list
    offered = agent_tool_definitions(("similar_history",))
    assert [d["name"] for d in offered] == [*MVP1_TOOLS[:-1], "similar_history", "submit_report"]
    assert system_prompt(("similar_history",)).startswith(SYSTEM_PROMPT)
    assert system_prompt(("similar_history",)) != SYSTEM_PROMPT


def _run(make_orch: Callable[..., Orchestrator], label: FailureLabel, source, **kw) -> list:  # type: ignore[no-untyped-def]
    models: list[FakeModel] = []

    def factory(_e: str, _b: object) -> FakeModel:
        models.append(scripted_model(label, source, "correct"))
        return models[-1]

    make_orch(label, model_factory=factory, **kw).handle(label.execution_id)
    return [r for m in models for r in m.requests]


def test_orchestrator_sends_mvp1_tools_and_prompt_by_default(
    make_orch: Callable[..., Orchestrator], pick: Callable[..., FailureLabel], dataset_dir: Path
) -> None:
    from driftgate.adapters.synthetic import SyntheticSource

    label = pick("transient_throttling", fleet=False)
    requests = _run(make_orch, label, SyntheticSource(dataset_dir))
    assert requests
    for r in requests:
        assert r.system == SYSTEM_PROMPT
        assert _sha(r.tools) == MVP1_TOOL_DEFINITIONS_SHA256


def test_an_index_alone_does_not_turn_the_tool_on(
    make_orch: Callable[..., Orchestrator], pick: Callable[..., FailureLabel], dataset_dir: Path
) -> None:
    from driftgate.adapters.synthetic import SyntheticSource

    label = pick("transient_throttling", fleet=False)
    requests = _run(make_orch, label, SyntheticSource(dataset_dir), history=HistoryIndex())
    assert all(_sha(r.tools) == MVP1_TOOL_DEFINITIONS_SHA256 and r.system == SYSTEM_PROMPT for r in requests)


def test_offering_the_tool_adds_it_and_its_prompt_note_only_when_asked(
    make_orch: Callable[..., Orchestrator], pick: Callable[..., FailureLabel], dataset_dir: Path
) -> None:
    from driftgate.adapters.synthetic import SyntheticSource

    label = pick("transient_throttling", fleet=False)
    requests = _run(make_orch, label, SyntheticSource(dataset_dir), history=HistoryIndex(), offer_similar_history=True)
    assert requests
    for r in requests:
        assert "similar_history" in [t["name"] for t in r.tools]
        assert r.system == system_prompt(("similar_history",))


def test_offering_without_an_index_is_refused(make_orch: Callable[..., Orchestrator], pick) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="history index"):
        make_orch(pick("transient_throttling", fleet=False), offer_similar_history=True)
