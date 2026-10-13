"""N1 runner: HoldoutSource, the proposal-only runner, labels-before-runs, the sealed test split, record-holdout caps.

Everything is offline: fake clock, injected git status, no key, no network, no sleep. The tiny holdout is built in
tmp_path by `holdout_helpers` (scaffolding; the log text is copied from verbatim-captured catalog entries)."""

from __future__ import annotations

import ast
import io
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import tasks
from holdout_helpers import (
    EXPIRED_LOG,
    NOW,
    RATE_LIMIT_LOG,
    RATIONALES,
    clean_git,
    clock_at,
    dirty_git,
    make_holdout,
    unknown_git,
)
from jsonschema import Draft202012Validator

from driftgate.domain import SourceError
from driftgate.eval import holdout as hd
from driftgate.eval import holdout_eval as he
from driftgate.eval import holdout_run as hr
from driftgate.eval import live
from driftgate.eval.holdout_source import HoldoutInput, HoldoutSource
from driftgate.llm.budget import CallRecord
from driftgate.llm.config import GatewayConfig
from driftgate.llm.errors import BudgetExceeded
from driftgate.llm.types import ModelRequest, ModelResponse, Usage
from driftgate.tools import ToolContext, dispatch

SRC = Path(__file__).resolve().parents[2] / "src" / "driftgate"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return make_holdout(tmp_path / "holdout")


def _inp(root: Path, case_id: str = "hc-01") -> HoldoutInput:
    h = hd.Holdout.load(root, now=NOW)
    case = h.case(case_id) if case_id in {c.id for c in h.dev_cases()} else None
    assert case is not None
    return hr.inputs_for(case, h.dev_log(case_id), root)


def _run(argv: list[str], root: Path, *, git=clean_git, clock=None) -> tuple[int, str]:  # type: ignore[no-untyped-def]
    out = io.StringIO()
    code = hr.main(["--dir", str(root), *argv], clock=clock or clock_at(), git_status=git, out=out)
    return code, out.getvalue()


# -- HoldoutSource ---------------------------------------------------------------------------------------------------
def test_source_serves_the_stored_log_and_metadata_only(root: Path) -> None:
    inp = _inp(root)
    src = HoldoutSource(inp)
    ex = src.get_execution("hc-01")
    assert ex.status == "failed" and ex.pipeline == "example-org/example-repo"
    assert ex.refs["commit"] == f"{1:040x}" and ex.refs["run_id"] == "1001" and ex.refs["job"] == "job-1"
    (leaf,) = src.get_failed_leaf_nodes("hc-01")
    assert src.get_step_logs("hc-01", leaf.node_id, 10**6).text == RATE_LIMIT_LOG
    assert src.get_step_logs("hc-01", leaf.node_id, 10).truncated
    with pytest.raises(SourceError):
        src.get_execution("hc-99")
    with pytest.raises(SourceError):
        src.read_file("example-org/example-repo", "a.txt", "main", 100)


def test_the_input_type_has_no_label_field() -> None:
    fields = set(HoldoutInput.__dataclass_fields__)
    assert not fields & {"layer", "classification", "action", "correct_action", "rationale", "split", "labeled_at"}


def test_the_loader_reads_nothing_from_the_label_but_id_and_source() -> None:
    tree = ast.parse((SRC / "eval" / "holdout_run.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "inputs_for")
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute) and ast.unparse(n.value) == "case"}
    assert attrs == {"id", "raw"}
    subs = {
        ast.unparse(n.slice)
        for n in ast.walk(fn)
        if isinstance(n, ast.Subscript) and ast.unparse(n.value) == "case.raw"
    }
    assert subs == {"'source'"}


def test_tools_return_honest_empties_never_synthesized(root: Path) -> None:
    ctx = ToolContext(HoldoutSource(_inp(root)))
    fleet = dispatch(ctx, "fleet_correlate", {"execution_id": "hc-01"}, "c1")
    assert fleet.ok and fleet.data["fleet_wide"] is False and fleet.data["executions_affected"] == 0
    flake = dispatch(ctx, "flake_history", {"execution_id": "hc-01"}, "c2")
    assert flake.ok and flake.data["precedent"] is False
    sig = dispatch(ctx, "classify_signature", {"execution_id": "hc-01"}, "c3")
    assert sig.ok and sig.data["signature"] == "docker_pull_rate_limit" and sig.data["deterministic_match"]
    logs = dispatch(ctx, "get_step_logs", {"execution_id": "hc-01"}, "c4")
    assert logs.ok
    repo = dispatch(ctx, "read_repo_file", {"repo": "example-org/example-repo", "path": "x.yml", "ref": "main"}, "c5")
    assert not repo.ok and "not available" in (repo.error or "")
    assert HoldoutSource(_inp(root)).list_changes() == []


def test_a_source_with_no_log_timestamps_says_where_its_times_came_from(root: Path) -> None:
    ex = HoldoutSource(_inp(root)).get_execution("hc-01")
    assert ex.refs["time_basis"] == "fetch_date" and ex.started_at == "2026-10-14T00:00:00Z"


# -- architecture ----------------------------------------------------------------------------------------------------
def _calls_attrs(path: Path) -> set[str]:
    return {n.attr for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.Attribute)}


def _names(path: Path) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add(n.module or "")
    return out


def test_source_agents_tools_never_name_a_label_answer_field() -> None:
    files = [SRC / "eval" / "holdout_source.py", SRC / "orchestrator.py"]
    for sub in ("agents", "tools"):
        files += sorted((SRC / sub).rglob("*.py"))
    for p in files:
        text = p.read_text(encoding="utf-8")
        for name in ("correct_action", "labels.json", "label_digest", "labeled_at"):
            assert name not in text, (p.name, name)
        assert "holdout" not in {n.split(".")[-1] for n in _names(p)}, p.name


def test_nothing_under_agents_tools_llm_imports_the_runner_or_source() -> None:
    for sub in ("agents", "tools", "llm"):
        for p in sorted((SRC / sub).rglob("*.py")):
            assert not any("holdout" in n for n in _names(p)), p


def test_runner_modules_have_no_network_sdk_or_adapter_import_and_read_no_environment() -> None:
    banned = ("anthropic", "requests", "socket", "urllib", "http", "httpx", "dotenv", "subprocess")
    for name in ("holdout_run.py", "holdout_source.py"):
        p = SRC / "eval" / name
        for mod in _names(p):
            assert mod.split(".")[0] not in banned, (name, mod)
            assert "github" not in mod and "adapters" not in mod, (name, mod)
        src = p.read_text(encoding="utf-8")
        assert "os.environ" not in src and "getenv" not in src and "dotenv" not in src, name
    rec = SRC / "eval" / "holdout_record.py"
    for mod in _names(rec):
        assert mod.split(".")[0] not in banned and "github" not in mod, mod
    assert "os.environ" not in rec.read_text(encoding="utf-8")


def test_the_proposal_only_target_has_no_capability() -> None:
    from driftgate.domain import Remediation

    t = hr.ProposalOnlyTarget()
    rem = Remediation(tier=0, action="rerun_failed_job", rationale="r", reversible=True, gate="g")
    assert t.dry_run(rem).ok is False
    for call in (t.execute, t.verify, t.rollback):
        with pytest.raises(AssertionError):
            call(rem)


# -- runs: records, mapping, no execution ----------------------------------------------------------------------------
def test_a_run_writes_schema_valid_records_that_validate_and_evaluate(root: Path) -> None:
    code, text = _run(["--mode", "fake", "--write", "--allow-fake-records"], root)
    assert code == 0 and "nothing executed" in text
    doc = json.loads((root / "runs.json").read_text(encoding="utf-8"))
    schema = json.loads((SRC.parents[1] / "contracts" / "holdout.schema.json").read_text(encoding="utf-8"))
    runs_schema = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": "#/$defs/runs_document"}
    Draft202012Validator(runs_schema).validate(doc)
    assert [r["case_id"] for r in doc["runs"]] == ["hc-01", "hc-02", "hc-11", "hc-12"]
    assert {r["system"] for r in doc["runs"]} == {"agent"}
    assert {r["ran_at"] for r in doc["runs"]} == {"2026-10-20T12:00:00Z"}
    by = {r["case_id"]: r for r in doc["runs"]}
    assert by["hc-01"]["action"] == {"kind": "rerun"} and by["hc-02"]["action"] == {"kind": "escalate"}
    case = hd.Holdout.load(root, now=NOW).case("hc-01")
    assert by["hc-01"]["label_sha256"] == hd.label_digest(case.raw)
    meta = json.loads((root / "runs_meta.json").read_text(encoding="utf-8"))
    assert meta["runs"]["hc-01"]["tools"] == "none" and meta["runs"]["hc-01"]["mode"] == "fake"
    assert set(meta["runs"]["hc-01"]) >= {"model", "tool_calls", "cost_usd", "fixture_id", "model_calls"}
    # validate flags only the unmet quotas of this 4-case tiny set (exit 1), never a run problem
    assert he.main(["validate", "--dir", str(root)], clock=clock_at(), git_status=clean_git) == 1
    assert not hd.validate(root, now=NOW).problems
    assert he.main(["eval", "--dir", str(root)], clock=clock_at(), git_status=clean_git) == 0


def test_fake_results_are_not_recorded_without_the_explicit_flag(root: Path) -> None:
    code, text = _run(["--mode", "fake", "--write"], root)
    assert code == 0 and "wrote 0 run record" in text
    assert json.loads((root / "runs.json").read_text(encoding="utf-8"))["runs"] == []
    assert not (root / "runs_meta.json").exists()
    code, text = _run(["--mode", "fake", "--allow-fake-records"], root)
    assert code == 2 and "--write" in text


def test_replay_without_fixtures_runs_nothing_and_says_so(root: Path, tmp_path: Path) -> None:
    code, text = _run(["--mode", "replay", "--fixtures", str(tmp_path / "nofx"), "--write"], root)
    assert code == 0 and "without fixture 2" in text and "wrote 0 run record" in text


def test_tier2_never_qualifies_and_no_history_means_only_the_transient_rule_reruns(root: Path) -> None:
    inp = _inp(root, "hc-02")  # signature without a Tier 0 rule
    run = hr.run_case(inp, lambda eid, _b: hr.plumbing_model(eid))
    assert run.action == {"kind": "escalate"}
    run1 = hr.run_case(_inp(root, "hc-01"), lambda eid, _b: hr.plumbing_model(eid))
    assert run1.action == {"kind": "rerun"} and run1.gate_decision == "allowed"
    assert run1.dry_runs == ("rerun_failed_job",)  # asked to dry-run, never executed


def test_gate_downgrades_a_tier0_proposal_without_a_known_transient_rule(root: Path) -> None:
    def greedy(eid: str, _b: object) -> object:
        from driftgate.agents.investigator import SUBMIT_REPORT
        from driftgate.llm.fake import FakeModel
        from driftgate.llm.types import ToolCall

        calls = [ToolCall("t1", "classify_signature", {"execution_id": eid})]
        report = {
            "classification": "transient",
            "layer": "L2",
            "confidence": 0.99,
            "hypothesis": "h",
            "evidence": [{"source": "t1", "finding": "f", "supports": "classification"}],
            "remediation": {"tier": 0, "action": "rerun_failed_job", "rationale": "r", "reversible": True},
        }
        return FakeModel(
            [
                lambda _r: ModelResponse(tool_calls=calls, usage=Usage(100, 10)),
                lambda _r: ModelResponse(tool_calls=[ToolCall("t2", SUBMIT_REPORT, report)], usage=Usage(100, 10)),
            ]
        )

    run = hr.run_case(_inp(root, "hc-02"), greedy)  # type: ignore[arg-type]
    assert run.action == {"kind": "escalate"} and run.gate_decision in ("downgraded", "refused", "none")


def test_map_outcome_names_each_vocabulary_item() -> None:
    from types import SimpleNamespace as NS

    def outcome(kind: str, tier: int, action: str, diff: str = "", paths: tuple[str, ...] = ()) -> NS:
        rem = NS(tier=tier, action=action)
        report = NS(layer="L3", classification="user", remediation=rem)
        return NS(
            kind=kind,
            report=report,
            gate=NS(allowed=True),
            reason="",
            investigation=NS(proposal=NS(diff=diff, paths=paths)),
        )

    assert hr.map_outcome(outcome("done", 0, "rerun_workflow"))[2] == {"kind": "rerun"}  # type: ignore[arg-type]
    assert hr.map_outcome(outcome("done", 3, "fix_secret_scope", "d", ("a",)))[2] == {  # type: ignore[arg-type]
        "kind": "pr_fix",
        "fix": "fix_secret_scope",
    }
    assert hr.map_outcome(outcome("done", 3, "fix_secret_scope", "", ()))[2] == {"kind": "escalate"}  # type: ignore[arg-type]
    assert hr.map_outcome(outcome("done", 2, "x"))[2] == {"kind": "escalate"}  # type: ignore[arg-type]


def test_optional_tools_are_refused_for_holdout_runs(root: Path, tmp_path: Path) -> None:
    for tools in ("similar_history", "changes_in_window", "both"):
        code, text = _run(["--tools", tools], root)
        assert code == 2 and "refused" in text and "synthetic" in text
    assert hr.TOOLS_BACKING == "none"


# -- labels before runs ----------------------------------------------------------------------------------------------
def test_refuses_when_a_label_is_dated_in_the_future(tmp_path: Path) -> None:
    root = make_holdout(tmp_path / "h", labeled_at="2026-10-25T09:00:00Z")
    code, text = _run(["--mode", "fake"], root)
    assert code == 1 and "REFUSED" in text


def test_refuses_when_labeled_at_equals_the_run_time(tmp_path: Path) -> None:
    root = make_holdout(tmp_path / "h", labeled_at="2026-10-20T12:00:00Z")
    code, text = _run(["--mode", "fake"], root)
    assert code == 1 and "REFUSED" in text


def test_refuses_dirty_or_unknown_git_state_of_labels(root: Path) -> None:
    code, text = _run(["--mode", "fake"], root, git=dirty_git)
    assert code == 1 and "uncommitted" in text
    code, text = _run(["--mode", "fake"], root, git=unknown_git)
    assert code == 1 and "fail closed" in text
    assert json.loads((root / "runs.json").read_text(encoding="utf-8"))["runs"] == []


def test_refuses_a_structurally_invalid_holdout(root: Path) -> None:
    (root / "logs" / "hc-01.log").write_text("tampered\n", encoding="utf-8")
    code, text = _run(["--mode", "fake"], root)
    assert code == 1 and "REFUSED" in text


def test_a_changed_label_voids_its_run_record(root: Path) -> None:
    _run(["--mode", "fake", "--write", "--allow-fake-records"], root)
    labels = json.loads((root / "labels.json").read_text(encoding="utf-8"))
    labels["cases"][0]["rationale"] = "edited after the run"
    (root / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
    problems = hd.validate(root, now=NOW + timedelta(days=1)).problems
    assert any("hc-01" in p for p in problems)


# -- sealed test split -----------------------------------------------------------------------------------------------
def test_test_split_prints_aggregates_only(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, text = _run(["--mode", "fake", "--write", "--allow-fake-records"], root)
    seen = text + capsys.readouterr().out + capsys.readouterr().err
    assert code == 0
    assert "test (sealed: aggregate only): 2 cases" in seen
    for test_id in ("hc-11", "hc-12"):
        assert test_id not in seen
    for r in ("TESTRATIONALE-eleven", "TESTRATIONALE-twelve", "job-11", "job-12", "1011", "1012"):
        assert r not in seen
    assert "docker_pull_rate_limit" not in seen.split("dev:")[-1]  # no signature or log text after the dev lines
    for line in RATE_LIMIT_LOG.splitlines() + EXPIRED_LOG.splitlines():
        if line.strip():
            assert line not in seen
    assert "hc-01" in seen and "hc-02" in seen  # dev cases may print per case
    assert "TESTRATIONALE" not in seen and not any(RATIONALES[k] in seen for k in ("hc-11", "hc-12"))


def test_test_split_only_run_prints_no_case_line(root: Path) -> None:
    code, text = _run(["--mode", "fake", "--split", "test"], root)
    assert code == 0 and not re.search(r"hc-\d+", text)


# -- tasks, human-only, autonomous ------------------------------------------------------------------------------------
def test_targets_are_wired_and_record_holdout_is_human_only() -> None:
    assert "record-holdout" in tasks.HUMAN_ONLY and "run-holdout" not in tasks.HUMAN_ONLY
    assert tasks.RUNNER_TARGETS == {"run-holdout", "record-holdout"} and {"run-holdout", "record-holdout"} <= set(tasks.HANDLERS)


def test_record_holdout_refuses_under_autonomous(tmp_path: Path) -> None:
    (tmp_path / ".autonomous").write_text("")

    def boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("must not load credentials or build a gateway under .autonomous")

    out = io.StringIO()
    code = live.main(
        ["record-holdout", "--yes-spend"], root=tmp_path, out=out, config_loader=boom, gateway_factory=boom
    )
    assert code == 1 and "human-only" in out.getvalue()


# -- record-holdout: estimate and caps -------------------------------------------------------------------------------
class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC).timestamp()

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        raise AssertionError("tests never sleep")


class _StubGateway:
    def __init__(self, cost: float, exc: Exception | None = None) -> None:
        self.calls: list[CallRecord] = []
        self.cost, self.exc = cost, exc

    def bind(self, budget: object = None, role: str = "investigator") -> _StubClient:
        return _StubClient(self)


class _StubClient:
    def __init__(self, gw: _StubGateway) -> None:
        self._gw, self._model = gw, None

    def complete(self, request: ModelRequest) -> ModelResponse:
        if self._gw.exc is not None:
            raise self._gw.exc
        if self._model is None:
            case_id = re.search(r"hc-\d+", json.dumps(request.messages))
            assert case_id, "the case id must be in the first user message"
            self._model = hr.plumbing_model(case_id.group(0))
        self._gw.calls.append(CallRecord("stub", 10, 5, 0, 0, self._gw.cost, 0.0, 0.0, "record"))
        return self._model.complete(request)


def _rec(argv: list[str], root: Path, tmp_path: Path, gw: _StubGateway | None = None, **kw):  # type: ignore[no-untyped-def]
    out = io.StringIO()

    def boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("must not be reached")

    def models(r: Path) -> GatewayConfig:
        return GatewayConfig(ledger_path=tmp_path / "spend.sqlite")

    def loader(r: Path) -> GatewayConfig:
        return GatewayConfig(api_key="sk-ant-TESTSECRET-not-real", ledger_path=tmp_path / "spend.sqlite")

    code = live.main(
        ["record-holdout", "--dir", str(root), "--fixtures", str(tmp_path / "fx"), *argv],
        root=tmp_path,
        out=out,
        models_loader=models,
        config_loader=loader if gw else boom,
        gateway_factory=(lambda _c, _m, _f: gw) if gw else boom,
        clock=_Clock(),
        git_status=kw.get("git", clean_git),
    )
    return code, out.getvalue()


def test_estimate_makes_no_request_needs_no_key_and_hides_test_ids(root: Path, tmp_path: Path) -> None:
    code, text = _rec(["--estimate"], root, tmp_path)
    assert code == 0 and "ESTIMATE ONLY" in text and "no API key" in text and "ledger:" in text
    assert "hc-01" in text and "hc-02" in text
    assert "hc-11" not in text and "hc-12" not in text and "test cases to run (2, sealed" in text
    assert json.loads((root / "runs.json").read_text(encoding="utf-8"))["runs"] == []


def test_spending_requires_yes_spend(root: Path, tmp_path: Path) -> None:
    code, text = _rec([], root, tmp_path)
    assert code == 2 and "--yes-spend" in text


def test_record_holdout_refuses_optional_tools_dirty_labels_and_future_labels(root: Path, tmp_path: Path) -> None:
    code, text = _rec(["--estimate", "--tools", "both"], root, tmp_path)
    assert code == 2 and "refused" in text
    code, text = _rec(["--estimate"], root, tmp_path, git=dirty_git)
    assert code == 1 and "uncommitted" in text
    future = make_holdout(tmp_path / "future", labeled_at="2026-10-25T09:00:00Z")
    code, text = _rec(["--estimate"], future, tmp_path)
    assert code == 1 and "REFUSED" in text


def test_record_runs_test_first_writes_fixtures_manifest_records_and_meta(root: Path, tmp_path: Path) -> None:
    gw = _StubGateway(cost=0.01)
    code, text = _rec(["--yes-spend", "--cap-usd", "1.0"], root, tmp_path, gw)
    assert code == 0, text
    assert len(gw.calls) == 12  # 4 cases x 3 model calls
    assert text.index("test (sealed") < text.index("dev:")  # test split ran first
    assert "hc-11" not in text and "hc-12" not in text
    assert "TESTSECRET" not in text
    doc = json.loads((root / "runs.json").read_text(encoding="utf-8"))
    assert {r["case_id"] for r in doc["runs"]} == {"hc-01", "hc-02", "hc-11", "hc-12"}
    meta = json.loads((root / "runs_meta.json").read_text(encoding="utf-8"))
    assert meta["runs"]["hc-11"]["mode"] == "record" and meta["runs"]["hc-11"]["fixture_id"] == "hc-11"
    assert (tmp_path / "fx" / "manifest.json").exists()


def test_record_stops_at_the_run_cap_and_later_cases_are_not_run(root: Path, tmp_path: Path) -> None:
    gw = _StubGateway(cost=0.10)
    code, text = _rec(["--yes-spend", "--cap-usd", "0.35"], root, tmp_path, gw)
    assert code == 3 and "stopped" in text
    assert len(gw.calls) < 12
    ran = {r["case_id"] for r in json.loads((root / "runs.json").read_text(encoding="utf-8"))["runs"]}
    assert ran != {"hc-01", "hc-02", "hc-11", "hc-12"}
    assert "hc-02" not in ran or "hc-01" in ran  # dev order is preserved, test cases came first


def test_a_gateway_cap_failure_leaves_no_run_record_for_that_case(root: Path, tmp_path: Path) -> None:
    gw = _StubGateway(cost=0.0, exc=BudgetExceeded("daily", 1.0, 1.0, 0.1))
    code, text = _rec(["--yes-spend", "--cap-usd", "1.0"], root, tmp_path, gw)
    assert code == 3 and "gateway failure" in text
    assert json.loads((root / "runs.json").read_text(encoding="utf-8"))["runs"] == []


def test_the_key_is_scrubbed_from_transport_errors(root: Path, tmp_path: Path) -> None:
    gw = _StubGateway(cost=0.0, exc=RuntimeError("401 invalid x-api-key sk-ant-TESTSECRET-not-real"))
    code, text = _rec(["--yes-spend"], root, tmp_path, gw)
    assert code == 3 and "[redacted]" in text and "TESTSECRET" not in text


def _sdk_from(resp: ModelResponse) -> SimpleNamespace:
    content: list[SimpleNamespace] = [SimpleNamespace(type="text", text=resp.text or "")]
    content += [SimpleNamespace(type="tool_use", id=c.id, name=c.name, input=c.input) for c in resp.tool_calls]
    usage = SimpleNamespace(
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(content=content, stop_reason="tool_use", usage=usage)


def test_record_through_a_real_gateway_with_a_stub_sdk_then_replay_offline(root: Path, tmp_path: Path) -> None:
    """A record-mode Gateway (SDK client stubbed, fake clock, real ledger file) writes fixtures; run-holdout then
    replays them with no client at all and writes agent records whose meta says mode=replay."""
    from driftgate.agents.fingerprint import prompt_fingerprint
    from driftgate.llm.gateway import Gateway
    from driftgate.llm.replay import FixtureStore

    models: dict[str, object] = {}

    def create(**payload):  # type: ignore[no-untyped-def]
        case_id = re.search(r"hc-\d+", json.dumps(payload["messages"]))
        assert case_id
        model = models.setdefault(case_id.group(0), hr.plumbing_model(case_id.group(0)))
        req = ModelRequest(system="", messages=payload["messages"], tools=payload.get("tools", []), max_tokens=1)
        return _sdk_from(model.complete(req))  # type: ignore[attr-defined]

    stub = SimpleNamespace(messages=SimpleNamespace(create=create))
    cfg = GatewayConfig(api_key="sk-ant-TESTSECRET-not-real", ledger_path=tmp_path / "spend.sqlite")
    clock = _Clock()
    fx = tmp_path / "fx"
    gateway = Gateway(
        cfg, mode="record", fixtures=FixtureStore(fx, fingerprint=prompt_fingerprint()), client=stub, clock=clock
    )
    out = io.StringIO()
    code = live.main(
        ["record-holdout", "--dir", str(root), "--fixtures", str(fx), "--yes-spend"],
        root=tmp_path,
        out=out,
        models_loader=lambda _r: cfg,
        config_loader=lambda _r: cfg,
        gateway_factory=lambda _c, _m, _f: gateway,
        clock=clock,
        git_status=clean_git,
    )
    assert code == 0, out.getvalue()
    assert (fx / "manifest.json").exists() and len(gateway.calls) == 12
    # Reset the run records, then replay.
    (root / "runs.json").write_text(json.dumps({"schema_version": 1, "runs": []}), encoding="utf-8")
    (root / "runs_meta.json").unlink()
    code, text = _run(["--mode", "replay", "--fixtures", str(fx), "--write"], root)
    assert code == 0 and "wrote 4 run record" in text, text
    meta = json.loads((root / "runs_meta.json").read_text(encoding="utf-8"))["runs"]
    assert {m["mode"] for m in meta.values()} == {"replay"}
    by = {r["case_id"]: r["action"] for r in json.loads((root / "runs.json").read_text(encoding="utf-8"))["runs"]}
    assert by["hc-01"] == {"kind": "rerun"} and by["hc-12"] == {"kind": "escalate"}
