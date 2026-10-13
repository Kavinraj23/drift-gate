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

from driftgate import autonomy
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


ALLOWED_CASE_ATTRS = {"id", "raw"}


def label_access_violations(source: str, func: str = "inputs_for") -> list[str]:
    """Everything a loader function does with its `case` parameter other than reading `case.id` and the single
    subscript `case.raw["source"]`. Aliases (`raw = case.raw`), `.get/.items/.copy`, `dict(case.raw)`, `getattr`,
    `vars`, passing `case` anywhere, and any other attribute are all violations."""
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func)
    parent = {child: node for node in ast.walk(fn) for child in ast.iter_child_nodes(node)}
    found: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id == "case" and isinstance(node.ctx, ast.Load):
            up = parent.get(node)
            if not (isinstance(up, ast.Attribute) and up.value is node):
                found.append(f"bare use of `case` ({ast.unparse(up) if up else '?'})")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "case":
            if node.attr not in ALLOWED_CASE_ATTRS:
                found.append(f"case.{node.attr}")
            elif node.attr == "raw":
                up = parent.get(node)
                ok = (
                    isinstance(up, ast.Subscript)
                    and up.value is node
                    and isinstance(up.ctx, ast.Load)
                    and isinstance(up.slice, ast.Constant)
                    and up.slice.value == "source"
                )
                if not ok:
                    found.append(f"case.raw used other than as case.raw['source'] ({ast.unparse(up)})")
    return found


def test_the_loader_reads_nothing_from_the_label_but_id_and_source() -> None:
    source = (SRC / "eval" / "holdout_run.py").read_text(encoding="utf-8")
    assert label_access_violations(source) == []
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "inputs_for")
    subs = {
        ast.unparse(n.slice)
        for n in ast.walk(fn)
        if isinstance(n, ast.Subscript) and ast.unparse(n.value) == "case.raw"
    }
    assert subs == {"'source'"}  # and the source subscript really is used


HOSTILE_LOADERS = {
    "alias of raw": "def inputs_for(case):\n    raw = case.raw\n    return raw['layer']\n",
    "alias of case": "def inputs_for(case):\n    c = case\n    return c.layer\n",
    "raw .get": "def inputs_for(case):\n    return case.raw.get('layer')\n",
    "raw .items": "def inputs_for(case):\n    return list(case.raw.items())\n",
    "raw .copy": "def inputs_for(case):\n    return case.raw.copy()\n",
    "dict(raw)": "def inputs_for(case):\n    return dict(case.raw)['correct_action']\n",
    "getattr": "def inputs_for(case):\n    return getattr(case, 'layer')\n",
    "vars": "def inputs_for(case):\n    return vars(case)\n",
    "other subscript": "def inputs_for(case):\n    return case.raw['layer']\n",
    "other attribute": "def inputs_for(case):\n    return case.classification\n",
    "__dict__": "def inputs_for(case):\n    return case.__dict__\n",
    "walrus alias": "def inputs_for(case):\n    return (r := case.raw)\n",
    "pass case on": "def inputs_for(case):\n    return helper(case)\n",
    "deepcopy": "def inputs_for(case):\n    return copy.deepcopy(case)\n",
    "store into raw": "def inputs_for(case):\n    case.raw['source'] = 1\n",
}


@pytest.mark.parametrize("name", sorted(HOSTILE_LOADERS))
def test_the_label_access_check_catches_every_hostile_form(name: str) -> None:
    assert label_access_violations(HOSTILE_LOADERS[name]), name


def test_the_label_access_check_accepts_the_two_allowed_reads() -> None:
    ok = "def inputs_for(case):\n    src = case.raw['source']\n    return case.id, src['repo']\n"
    assert label_access_violations(ok) == []


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
def real_records(root: Path, tmp_path: Path, cost: float = 0.01) -> str:
    """Write run records through the record path (stub gateway: a stand-in for the SDK). Mode `record`, with the real
    provenance entry that `eval-holdout` requires. Returns the output."""
    code, text = _rec(["--yes-spend", "--cap-usd", "1.0"], root, tmp_path, _StubGateway(cost=cost))
    assert code == 0, text
    return text


def _meta(root: Path) -> dict:  # type: ignore[type-arg]
    return json.loads((root / "runs_meta.json").read_text(encoding="utf-8"))


def _edit_meta(root: Path, fn) -> None:  # type: ignore[no-untyped-def]
    doc = _meta(root)
    fn(doc["runs"])
    (root / "runs_meta.json").write_text(json.dumps(doc), encoding="utf-8")


def _edit_runs(root: Path, fn) -> None:  # type: ignore[no-untyped-def]
    doc = json.loads((root / "runs.json").read_text(encoding="utf-8"))
    fn(doc["runs"])
    (root / "runs.json").write_text(json.dumps(doc), encoding="utf-8")


def _eval(root: Path) -> int:
    return he.main(["eval", "--dir", str(root)], clock=clock_at(), git_status=clean_git)


def test_a_recorded_run_writes_schema_valid_records_that_validate_and_evaluate(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real_records(root, tmp_path)
    doc = json.loads((root / "runs.json").read_text(encoding="utf-8"))
    schema = json.loads((SRC.parents[1] / "contracts" / "holdout.schema.json").read_text(encoding="utf-8"))
    runs_schema = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": "#/$defs/runs_document"}
    Draft202012Validator(runs_schema).validate(doc)
    assert sorted(r["case_id"] for r in doc["runs"]) == ["hc-01", "hc-02", "hc-11", "hc-12"]
    assert {r["system"] for r in doc["runs"]} == {"agent"}
    assert {r["ran_at"] for r in doc["runs"]} == {"2026-10-20T12:00:00Z"}
    by = {r["case_id"]: r for r in doc["runs"]}
    # Headline = action AFTER the gates: a lone stored job is 100% of its set, so the ceiling refuses the re-run.
    assert by["hc-01"]["action"] == {"kind": "escalate"} and by["hc-02"]["action"] == {"kind": "escalate"}
    case = hd.Holdout.load(root, now=NOW).case("hc-01")
    assert by["hc-01"]["label_sha256"] == hd.label_digest(case.raw)
    meta = _meta(root)["runs"]
    m = meta["hc-01"]
    assert m["tools"] == "none" and m["mode"] == "record" and m["provenance"] == "recorded_real_model"
    assert set(m) >= {"model", "tool_calls", "cost_usd", "fixture_id", "model_calls", "record_sha256"}
    # ... and the ungated view is recorded beside it.
    prop = m["proposal_before_gates"]
    assert prop["kind"] == "rerun" and prop["gate_decision"] == "refused"
    assert prop["gate_reason_code"] == "abort_ceiling_set_100pct" and prop["refused_by_abort_ceiling_only"] is True
    assert meta["hc-02"]["proposal_before_gates"]["kind"] == "escalate"
    # validate flags only the unmet quotas of this 4-case tiny set (exit 1), never a run problem
    assert he.main(["validate", "--dir", str(root)], clock=clock_at(), git_status=clean_git) == 1
    assert not hd.validate(root, now=NOW).problems
    capsys.readouterr()
    assert _eval(root) == 0


def test_fake_mode_never_writes_run_records_or_a_sidecar(root: Path) -> None:
    code, text = _run(["--mode", "fake", "--write"], root)
    assert code == 0 and "wrote 0 run record" in text
    assert json.loads((root / "runs.json").read_text(encoding="utf-8"))["runs"] == []
    assert not (root / "runs_meta.json").exists()
    code, text = _run(["--mode", "auto", "--fixtures", str(root / "nofx"), "--write"], root)  # double fills in
    assert code == 0 and "wrote 0 run record" in text and not (root / "runs_meta.json").exists()
    with pytest.raises(SystemExit):  # the old escape hatch is gone
        _run(["--mode", "fake", "--write", "--allow-fake-records"], root)
    assert json.loads((root / "runs.json").read_text(encoding="utf-8"))["runs"] == []


def test_replay_without_fixtures_runs_nothing_and_says_so(root: Path, tmp_path: Path) -> None:
    code, text = _run(["--mode", "replay", "--fixtures", str(tmp_path / "nofx"), "--write"], root)
    assert code == 0 and "without fixture 2" in text and "wrote 0 run record" in text


def test_tier2_never_qualifies_and_no_history_means_only_the_transient_rule_reruns(root: Path) -> None:
    inp = _inp(root, "hc-02")  # signature without a Tier 0 rule
    run = hr.run_case(inp, lambda eid, _b: hr.plumbing_model(eid))
    assert run.action == {"kind": "escalate"}
    run1 = hr.run_case(_inp(root, "hc-01"), lambda eid, _b: hr.plumbing_model(eid))
    # The headline is the action after the gates; the production inputs make a lone job 100% of its set, so the abort
    # ceiling refuses the re-run (no lift). The ungated proposal is still visible beside it.
    assert run1.action == {"kind": "escalate"} and run1.gate_decision == "refused"
    assert run1.dry_runs == ()  # the gate refused before any dry run
    assert run1.proposal["kind"] == "rerun" and run1.proposal["refused_by_abort_ceiling_only"] is True


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


def test_a_changed_label_voids_its_run_record(root: Path, tmp_path: Path) -> None:
    real_records(root, tmp_path)
    labels = json.loads((root / "labels.json").read_text(encoding="utf-8"))
    labels["cases"][0]["rationale"] = "edited after the run"
    (root / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
    problems = hd.validate(root, now=NOW + timedelta(days=1)).problems
    assert any("hc-01" in p for p in problems)


# -- sealed test split -----------------------------------------------------------------------------------------------
def test_test_split_prints_aggregates_only(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, text = _run(["--mode", "fake"], root)
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
    assert tasks.RUNNER_TARGETS == {"run-holdout", "record-holdout"} and {"run-holdout", "record-holdout"} <= set(
        tasks.HANDLERS
    )


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
    code, text = _rec(["--yes-spend", "--cap-usd", "1.0", "--cases", "hc-01", "hc-02"], root, tmp_path, gw)
    assert code == 3 and "gateway failure" in text  # a dev case may keep detail
    assert json.loads((root / "runs.json").read_text(encoding="utf-8"))["runs"] == []


def test_the_key_is_scrubbed_from_transport_errors(root: Path, tmp_path: Path) -> None:
    gw = _StubGateway(cost=0.0, exc=RuntimeError("401 invalid x-api-key sk-ant-TESTSECRET-not-real"))
    code, text = _rec(["--yes-spend", "--cases", "hc-01"], root, tmp_path, gw)  # dev case: detail kept, scrubbed
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


def _record_fixtures(root: Path, tmp_path: Path) -> Path:
    """A record-mode Gateway (SDK client stubbed, fake clock, real ledger file) writes fixtures via record-holdout.
    Returns the fixtures directory."""
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
    return fx


def _reset_runs(root: Path) -> None:
    (root / "runs.json").write_text(json.dumps({"schema_version": 1, "runs": []}), encoding="utf-8")
    (root / "runs_meta.json").unlink()


def test_record_through_a_real_gateway_with_a_stub_sdk_then_replay_offline(root: Path, tmp_path: Path) -> None:
    """Fixtures recorded through a real record-mode Gateway are replayed by run-holdout with no client at all; the
    written agent records carry mode=replay and a real-model provenance."""
    fx = _record_fixtures(root, tmp_path)
    _reset_runs(root)
    code, text = _run(["--mode", "replay", "--fixtures", str(fx), "--write"], root)
    assert code == 0 and "wrote 4 run record" in text, text
    meta = json.loads((root / "runs_meta.json").read_text(encoding="utf-8"))["runs"]
    assert {m["mode"] for m in meta.values()} == {"replay"}
    assert {m["provenance"] for m in meta.values()} == {"recorded_real_model"}
    by = {r["case_id"]: r["action"] for r in json.loads((root / "runs.json").read_text(encoding="utf-8"))["runs"]}
    assert by["hc-01"] == {"kind": "escalate"} and by["hc-12"] == {"kind": "escalate"}  # the lone-job ceiling
    props = {k: v["proposal_before_gates"]["kind"] for k, v in meta.items()}
    assert props["hc-01"] == "rerun" and props["hc-11"] == "rerun" and props["hc-02"] == "escalate"


# -- gate inputs: no lift; two views --------------------------------------------------------------------------------
def test_gate_inputs_are_the_production_derivation_with_no_lift(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from driftgate.gates import SafetyGate

    seen = []
    real = SafetyGate.decide

    def spy(self, proposal, facts):  # type: ignore[no-untyped-def]
        seen.append((proposal.tier, facts))
        return real(self, proposal, facts)

    monkeypatch.setattr(SafetyGate, "decide", spy)
    hr.run_case(_inp(root, "hc-01"), lambda eid, _b: hr.plumbing_model(eid))
    ((tier, facts),) = seen
    assert tier == 0
    assert facts.proposed_set_size == 1 and facts.resource_set_total == 1  # exactly what the orchestrator derives
    text = (SRC / "eval" / "holdout_run.py").read_text(encoding="utf-8")
    assert "assume_other_jobs" not in text and "resource_set_total=" not in text
    assert not re.search(r"from dataclasses import.*replace", text)  # nothing rewrites the gate facts
    for name in ("holdout_record.py", "live.py"):
        assert "assume_other_jobs" not in (SRC / "eval" / name).read_text(encoding="utf-8")


def test_a_lone_job_tier0_rerun_is_refused_by_the_ceiling_and_both_views_are_recorded(root: Path) -> None:
    run = hr.run_case(_inp(root, "hc-01"), lambda eid, _b: hr.plumbing_model(eid))
    assert run.action == {"kind": "escalate"}  # headline: action after the gates
    p = run.proposal  # ungated view
    assert p["kind"] == "rerun" and p["tier"] == 0 and p["action"] == "rerun_failed_job"
    assert p["gate_decision"] == "refused" and p["gate_reason_code"] == "abort_ceiling_set_100pct"
    assert p["refused_by_abort_ceiling_only"] is True
    other = hr.run_case(_inp(root, "hc-02"), lambda eid, _b: hr.plumbing_model(eid)).proposal
    assert other["kind"] == "escalate" and other["refused_by_abort_ceiling_only"] is False


def test_a_ceiling_refusal_with_another_blocker_is_not_counted_as_ceiling_only() -> None:
    gate = SimpleNamespace(decision="refused", reason="action would touch 100% of the resource set", abort_ceiling=5)
    base = {
        "signature_match": True,
        "flake_precedent": False,
        "known_transient_rule": True,
        "prior_tier0_failed": False,
        "blast_radius": SimpleNamespace(executions_affected=0),
        "shared_resources": (),
    }

    def only(**change: object) -> bool:
        return hr._ceiling_was_the_only_blocker(gate, SimpleNamespace(**{**base, **change}))  # type: ignore[arg-type]

    assert only() is True
    assert only(signature_match=False) is False and only(known_transient_rule=False) is False
    assert only(prior_tier0_failed=True) is False
    assert only(blast_radius=SimpleNamespace(executions_affected=9)) is False
    assert only(shared_resources=("a", "b")) is False
    allowed = SimpleNamespace(decision="allowed", reason="", abort_ceiling=5)
    assert hr._ceiling_was_the_only_blocker(allowed, SimpleNamespace(**base)) is False  # type: ignore[arg-type]


def test_the_report_has_a_labelled_proposal_before_gates_block_with_counts(root: Path, tmp_path: Path) -> None:
    real_records(root, tmp_path)
    text, _ = he.evaluate(root, NOW)
    assert text is not None
    assert "## Proposal before gates (ungated; NOT the headline)" in text
    assert "| split | cases | rerun | pr_fix | escalate | refused by the abort ceiling only |" in text
    assert "| dev | 2 | 1 | 0 | 1 | 1 |" in text and "| test | 2 | 1 | 0 | 1 | 1 |" in text
    block = text.split("## Proposal before gates")[1]
    assert "hc-01" in block and "hc-02" in block  # per case for dev only
    assert "hc-11" not in text and "hc-12" not in text  # sealed
    assert "Agent provenance: recorded_real_model" in text


# -- provenance: eval-holdout builds the agent column only from recorded real-model runs ----------------------------
def test_eval_accepts_real_provenance_and_states_it_in_the_header(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real_records(root, tmp_path)
    capsys.readouterr()
    assert _eval(root) == 0
    out = capsys.readouterr().out
    assert "Agent provenance: recorded_real_model" in out and "claude-haiku" in out
    assert "0 case(s) replayed from recorded fixtures, 4 recorded live" in out and "tool set: none" in out


def test_eval_states_replay_counts_for_replayed_records(root: Path, tmp_path: Path) -> None:
    fx = _record_fixtures(root, tmp_path)
    _reset_runs(root)
    assert _run(["--mode", "replay", "--fixtures", str(fx), "--write"], root)[0] == 0
    text, _ = he.evaluate(root, NOW)
    assert text is not None and "4 case(s) replayed from recorded fixtures, 0 recorded live" in text


@pytest.mark.parametrize("how", ["mode", "scripted_provenance", "model_name"])
def test_eval_refuses_a_fake_or_scripted_meta_entry(
    how: str, root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real_records(root, tmp_path)

    def fake(runs: dict) -> None:  # type: ignore[type-arg]
        entry = runs["hc-11"]
        if how == "mode":
            entry["mode"] = "fake"
        elif how == "scripted_provenance":
            entry["provenance"] = "scripted_double"
        else:
            entry["model"] = hr.FAKE_MODEL_NAME

    _edit_meta(root, fake)
    capsys.readouterr()
    assert _eval(root) == 1
    out = capsys.readouterr().out
    assert "REFUSING" in out and "1 run record(s) come from a fake/scripted double" in out
    assert "hc-" not in out and "| metric |" not in out  # no ids, no numbers


def test_eval_refuses_run_records_with_no_meta_entry(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real_records(root, tmp_path)
    _edit_meta(root, lambda runs: [runs.pop(k) for k in ("hc-01", "hc-12")])
    capsys.readouterr()
    assert _eval(root) == 1
    out = capsys.readouterr().out
    assert "2 run record(s) have no provenance entry" in out and "unknown provenance" in out and "hc-" not in out
    (root / "runs_meta.json").unlink()
    assert _eval(root) == 1
    assert "4 run record(s) have no provenance entry" in capsys.readouterr().out


def test_a_record_appended_by_hand_cannot_replace_a_real_one(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real_records(root, tmp_path)

    def append(runs: list) -> None:  # type: ignore[type-arg]
        newest = dict(next(r for r in runs if r["case_id"] == "hc-02"))
        newest["action"] = {"kind": "rerun"}
        runs.append(newest)  # same ran_at, later in the file: the latest record now wins

    _edit_runs(root, append)
    capsys.readouterr()
    assert _eval(root) == 1
    out = capsys.readouterr().out
    assert "1 run record(s) do not match" in out and "hc-" not in out

    def fake_meta(runs: dict) -> None:  # type: ignore[type-arg]
        runs["hc-02"]["mode"] = "fake"

    _edit_meta(root, fake_meta)
    assert _eval(root) == 1
    assert "come from a fake/scripted double" in capsys.readouterr().out


def test_a_changed_run_record_field_is_refused_as_a_mismatch(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real_records(root, tmp_path)

    def tamper(runs: list) -> None:  # type: ignore[type-arg]
        rec = next(r for r in runs if r["case_id"] == "hc-12")
        rec["classification"] = "user" if rec["classification"] != "user" else "transient"

    _edit_runs(root, tamper)
    capsys.readouterr()
    assert _eval(root) == 1
    assert "1 run record(s) do not match" in capsys.readouterr().out


def test_a_fake_run_cannot_replace_a_real_record(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real_records(root, tmp_path)
    before = ((root / "runs.json").read_bytes(), (root / "runs_meta.json").read_bytes())
    code, text = _run(["--mode", "fake", "--write", "--rerun"], root)
    assert code == 0 and "wrote 0 run record" in text
    assert ((root / "runs.json").read_bytes(), (root / "runs_meta.json").read_bytes()) == before
    capsys.readouterr()
    assert _eval(root) == 0


def test_no_run_records_means_no_agent_column_and_no_refusal(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _eval(root) == 0
    out = capsys.readouterr().out
    assert "no run records" in out and "not run" in out


# -- sealed stop lines -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("cap", ["0.3", "0.305"])  # at the cap; and just under it so the NEXT estimate would pass it
def test_a_stop_at_a_test_case_prints_a_generic_line_only(cap: str, root: Path, tmp_path: Path) -> None:
    code, text = _rec(["--yes-spend", "--cap-usd", cap], root, tmp_path, _StubGateway(cost=0.10))
    assert code == 3 and "stopped: cap reached" in text
    for leak in ("would pass", "would eat into", "estimate $", "reached the cap", "cumulative", "hc-11", "hc-12"):
        assert leak not in text, leak


def test_a_gateway_error_at_a_test_case_prints_no_exception_text(root: Path, tmp_path: Path) -> None:
    gw = _StubGateway(cost=0.0, exc=RuntimeError("boom sk-ant-TESTSECRET-not-real raw response body"))
    code, text = _rec(["--yes-spend"], root, tmp_path, gw)
    assert code == 3 and "stopped: gateway error" in text
    for leak in ("RuntimeError", "boom", "TESTSECRET", "raw response body", "redacted", "hc-11", "hc-12"):
        assert leak not in text, leak
    gw = _StubGateway(cost=0.0, exc=BudgetExceeded("daily", 1.0, 1.0, 0.1))
    code, text = _rec(["--yes-spend"], root, tmp_path, gw)
    assert code == 3 and "stopped: gateway error" in text and "BudgetExceeded" not in text and "hc-11" not in text


def test_a_stop_at_a_dev_case_may_keep_its_scrubbed_detail(root: Path, tmp_path: Path) -> None:
    code, text = _rec(["--yes-spend", "--cap-usd", "0.6"], root, tmp_path, _StubGateway(cost=0.10))
    assert code == 3 and "reached the cap" in text  # test cases ran first; the stop is at a dev case
    assert "hc-11" not in text and "hc-12" not in text


# -- crash safety -----------------------------------------------------------------------------------------------------
class _CrashingGateway(_StubGateway):
    """Raises KeyboardInterrupt (not an Exception, so nothing catches it) when the Nth case starts."""

    def __init__(self, crash_at_bind: int) -> None:
        super().__init__(cost=0.01)
        self.binds, self.crash_at = 0, crash_at_bind

    def bind(self, budget: object = None, role: str = "investigator") -> _StubClient:
        self.binds += 1
        if self.binds == self.crash_at:
            raise KeyboardInterrupt
        return super().bind(budget, role)


def test_a_crash_mid_run_keeps_the_records_of_the_cases_already_finished(root: Path, tmp_path: Path) -> None:
    with pytest.raises(KeyboardInterrupt):
        _rec(["--yes-spend", "--cap-usd", "1.0"], root, tmp_path, _CrashingGateway(crash_at_bind=3))
    done = {r["case_id"] for r in json.loads((root / "runs.json").read_text(encoding="utf-8"))["runs"]}
    assert done == {"hc-11", "hc-12"}  # test split first; the third case never finished
    assert set(_meta(root)["runs"]) == {"hc-11", "hc-12"}
    assert not list(root.glob("*.tmp"))  # atomic writes leave no temp files


def test_a_gateway_failed_case_still_writes_no_record_with_incremental_writes(root: Path, tmp_path: Path) -> None:
    class Flaky(_StubGateway):
        def __init__(self) -> None:
            super().__init__(cost=0.01)
            self.n = 0

        def bind(self, budget: object = None, role: str = "investigator") -> _StubClient:
            self.n += 1
            if self.n == 2:
                self.exc = BudgetExceeded("daily", 1.0, 1.0, 0.1)
            return super().bind(budget, role)

    code, _text = _rec(["--yes-spend", "--cap-usd", "1.0"], root, tmp_path, Flaky())
    assert code == 3
    done = {r["case_id"] for r in json.loads((root / "runs.json").read_text(encoding="utf-8"))["runs"]}
    assert len(done) == 1 and set(_meta(root)["runs"]) == done


def test_run_holdout_writes_each_case_as_it_finishes(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = _record_fixtures(root, tmp_path)
    _reset_runs(root)
    calls = {"n": 0}
    real = hr.process_case

    def crash_on_third(*a, **k):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated crash")
        return real(*a, **k)

    monkeypatch.setattr(hr, "process_case", crash_on_third)
    with pytest.raises(RuntimeError):
        _run(["--mode", "replay", "--fixtures", str(fx), "--write"], root)
    done = {r["case_id"] for r in json.loads((root / "runs.json").read_text(encoding="utf-8"))["runs"]}
    assert done == {"hc-01", "hc-02"} and set(_meta(root)["runs"]) == done


# -- the sealed-data note ---------------------------------------------------------------------------------------------
def test_the_runner_says_the_sidecar_and_fixtures_hold_per_case_test_data(root: Path, tmp_path: Path) -> None:
    code, text = _run(["--mode", "fake"], root)
    assert code == 0 and "runs_meta.json" in text and "fixtures/replay-holdout/" in text and "Do not open" in text
    out = real_records(root, tmp_path)
    assert "runs_meta.json" in out and "fixtures/replay-holdout/" in out and "Do not open" in out


# -- tasks.py dispatch ------------------------------------------------------------------------------------------------
class _Run:
    def __init__(self) -> None:
        self.cmds: list[list[str]] = []

    def __call__(self, cmd, **_k):  # type: ignore[no-untyped-def]
        self.cmds.append(list(cmd))
        return SimpleNamespace(returncode=0)


@pytest.fixture
def tasks_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Run:
    rec = _Run()
    monkeypatch.setattr(tasks, "ROOT", tmp_path)
    monkeypatch.setattr(tasks.subprocess, "run", rec)
    return rec


def _after_dash_m(cmd: list[str]) -> list[str]:
    return cmd[cmd.index("-m") + 1 :]


@pytest.mark.parametrize(
    "flags",
    [["--estimate"], ["--yes-spend", "--cap-usd", "0.5"], ["--tools", "none"], []],
    ids=["estimate", "yes-spend", "tools", "plain"],
)
def test_record_holdout_dispatches_to_the_live_module_with_its_options(flags: list[str], tasks_env: _Run) -> None:
    assert tasks.main(["tasks.py", "record-holdout", *flags]) == 0
    (cmd,) = tasks_env.cmds  # exactly one subprocess: the live module, with the options passed through
    assert _after_dash_m(cmd) == ["driftgate.eval.live", "record-holdout", *flags]
    assert "driftgate.eval.holdout_run" not in cmd  # never the offline runner (plain invocation included)


def test_run_holdout_dispatches_to_the_offline_module(tasks_env: _Run) -> None:
    assert tasks.main(["tasks.py", "run-holdout", "--mode", "replay", "--write"]) == 0
    (cmd,) = tasks_env.cmds
    assert _after_dash_m(cmd)[0] == "driftgate.eval.holdout_run"
    assert cmd[-3:] == ["--mode", "replay", "--write"] and "driftgate.eval.live" not in cmd
    assert tasks.HANDLERS["run-holdout"]() == 0 and _after_dash_m(tasks_env.cmds[-1])[0] == "driftgate.eval.holdout_run"
    assert tasks.HANDLERS["record-holdout"]() == 0 and _after_dash_m(tasks_env.cmds[-1])[0] == "driftgate.eval.live"


def test_the_pinned_target_sets_are_unchanged_and_only_record_holdout_is_human_only() -> None:
    assert tasks.LIVE_TARGETS == {"record", "eval-live", "e2e-live"}
    assert tasks.ARG_TARGETS == {"approvals", "trace-report"}
    assert "record-holdout" in tasks.HUMAN_ONLY and "run-holdout" not in tasks.HUMAN_ONLY


def test_record_holdout_is_refused_under_autonomous_through_tasks_main(
    tasks_env: _Run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".autonomous").write_text("")
    assert tasks.main(["tasks.py", "record-holdout", "--yes-spend"]) == 1
    assert "human-only" in capsys.readouterr().out and tasks_env.cmds == []
    assert tasks.main(["tasks.py", "run-holdout"]) == 0  # the offline runner is not human-only


def test_a_dangling_autonomous_symlink_counts_as_present(
    tasks_env: _Run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    try:
        (tmp_path / ".autonomous").symlink_to(tmp_path / "no-such-target")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    assert not (tmp_path / ".autonomous").exists()  # Path.exists() would have said no
    assert tasks.main(["tasks.py", "record-holdout", "--estimate"]) == 1
    assert "human-only" in capsys.readouterr().out and tasks_env.cmds == []
    out = io.StringIO()
    assert live.main(["record-holdout", "--yes-spend"], root=tmp_path, out=out) == 1
    assert "human-only" in out.getvalue()


def test_autonomous_at_the_main_checkout_is_seen_from_a_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    main_checkout = tmp_path / "main"
    wt = main_checkout / ".claude" / "worktrees" / "wt"
    wt.mkdir(parents=True)
    assert not autonomy.autonomous_present_at(wt)
    (main_checkout / ".autonomous").write_text("")
    assert autonomy.autonomous_present_at(wt)
    other = tmp_path / "elsewhere"  # a worktree elsewhere: only its .git file points at the main checkout
    other.mkdir()
    (other / ".git").write_text(f"gitdir: {main_checkout / '.git' / 'worktrees' / 'x'}\n", encoding="utf-8")
    assert autonomy.autonomous_present_at(other)
    monkeypatch.setattr(tasks, "ROOT", wt)
    run = _Run()
    monkeypatch.setattr(tasks.subprocess, "run", run)
    assert tasks.main(["tasks.py", "record-holdout", "--estimate"]) == 1 and run.cmds == []
    out = io.StringIO()
    assert live.main(["record-holdout", "--estimate"], root=wt, out=out) == 1 and "human-only" in out.getvalue()


def test_the_shared_check_gives_the_same_answers_as_the_fetchers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from driftgate.eval import holdout_fetch

    main_checkout = tmp_path / "m"
    wt = main_checkout / ".claude" / "worktrees" / "w"
    wt.mkdir(parents=True)
    plain = tmp_path / "plain"
    plain.mkdir()
    for root in (wt, plain):
        monkeypatch.setattr(holdout_fetch, "ROOT", root)
        assert holdout_fetch.autonomous_present() is autonomy.autonomous_present_at(root) is False
    (main_checkout / ".autonomous").write_text("")
    (plain / ".autonomous").write_text("")
    for root in (wt, plain):
        monkeypatch.setattr(holdout_fetch, "ROOT", root)
        assert holdout_fetch.autonomous_present() is autonomy.autonomous_present_at(root) is True


def test_the_autonomous_check_does_not_rely_on_path_exists(
    tasks_env: _Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Simulates a dangling symlink where symlinks cannot be created: Path.exists() says no, lexists says yes."""
    (tmp_path / ".autonomous").write_text("")
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert autonomy.autonomous_present_at(tmp_path) is True
    assert tasks.main(["tasks.py", "record-holdout", "--estimate"]) == 1
    assert "human-only" in capsys.readouterr().out and tasks_env.cmds == []
    out = io.StringIO()
    assert live.main(["record-holdout", "--yes-spend"], root=tmp_path, out=out) == 1
