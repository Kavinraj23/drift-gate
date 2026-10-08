"""Each tool against the seeded dataset, plus the dispatcher and registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from driftgate.adapters.synthetic import SyntheticSource
from driftgate.domain import SourceError
from driftgate.eval.ground_truth import GroundTruth, load_ground_truth
from driftgate.tools import TOOLS, ToolContext, dispatch, tool_definitions
from driftgate.tools.attempts import AttemptStore, RemediationAttempt
from driftgate.tools.base import fingerprint
from driftgate.tools.classify_signature import classify_signature
from driftgate.tools.flake_history import flake_history
from driftgate.tools.fleet_correlate import fleet_correlate
from driftgate.tools.get_execution import get_execution
from driftgate.tools.get_remediation_attempt import get_remediation_attempt
from driftgate.tools.get_step_logs import DEFAULT_BUDGET_CHARS, MAX_BUDGET_CHARS, get_step_logs
from driftgate.tools.read_repo_file import HARD_MAX_BYTES, read_repo_file
from driftgate.tools.signatures import BY_ID

MVP_TOOLS = {
    "get_execution",
    "classify_signature",
    "fleet_correlate",
    "flake_history",
    "get_step_logs",
    "read_repo_file",
    "get_remediation_attempt",
}


@pytest.fixture(scope="module")
def ctx(dataset_dir: Path) -> ToolContext:
    return ToolContext(SyntheticSource(dataset_dir))


@pytest.fixture(scope="module")
def truth(dataset_dir: Path) -> GroundTruth:
    return load_ground_truth(dataset_dir)


def _ids(truth: GroundTruth, fault_id: str) -> list[str]:
    return sorted(e for e, f in truth.failures.items() if f.fault_id == fault_id)


# ---- fingerprint ----


def test_fingerprint_is_deterministic_and_normalized() -> None:
    a = fingerprint("tf_state_lock", "infra-db", "Terraform Plan")
    assert a == fingerprint("TF_STATE_LOCK", "infra-db", "  terraform   plan ")
    assert a != fingerprint("tf_state_lock", "infra-db", "Terraform Apply")
    assert a != fingerprint("tf_state_lock", "infra-eks", "Terraform Plan")
    assert a != fingerprint("aws_throttling", "infra-db", "Terraform Plan")
    assert len(a) == 16


# ---- get_execution ----


def test_get_execution_returns_tree_failed_leaf_and_refs(ctx: ToolContext, truth: GroundTruth) -> None:
    eid = _ids(truth, "platform_expired_token")[0]
    d = get_execution(ctx.source, eid)
    assert d["status"] == "failed" and d["tree"]["children"]
    assert [leaf["name"] for leaf in d["failed_leaves"]] == ["Configure AWS credentials"]
    assert d["failed_leaves"][0]["error_summary"]
    assert {"connector", "template", "runner_pool", "infra_def"} <= set(d["refs"])


# ---- classify_signature ----


def test_signature_for_every_failure_covers_its_catalog_families(ctx: ToolContext, truth: GroundTruth) -> None:
    for eid, label in truth.failures.items():
        if label.disposition == "close":
            continue
        d = classify_signature(ctx, eid)
        found = {s["id"] for s in d["all_signatures"]}
        assert set(label.error_catalog_ids) <= found, eid
        if label.error_catalog_ids:
            assert d["deterministic_match"], eid
            # the operative (highest-ranked) family wins; the masked throttling never does
            assert d["signature"] == max(label.error_catalog_ids, key=lambda i: BY_ID[i].rank), eid
        else:
            assert d["signature"] == "exit_code_nonzero" and not d["deterministic_match"], eid


def test_signature_layers_and_tier0_rules_match_the_labels(ctx: ToolContext, truth: GroundTruth) -> None:
    for fault, layer, rule in [
        ("platform_state_lock", "L4", False),
        ("platform_expired_token", "L2", False),
        ("platform_oom_killed", "L1", False),
        ("transient_throttling", "L4", True),
        ("transient_registry_rate_limit", "L2", True),
        ("transient_image_pull", "L1", False),
        ("user_lockfile_mismatch", "L4", False),
    ]:
        eid = _ids(truth, fault)[0]
        d = classify_signature(ctx, eid)
        assert (d["layer"], d["tier0_rule_exists"]) == (layer, rule), fault


def test_masked_lock_is_classified_as_the_lock(ctx: ToolContext, truth: GroundTruth) -> None:
    eid = _ids(truth, "platform_throttle_masks_lock")[0]
    d = classify_signature(ctx, eid)
    assert d["signature"] == "tf_state_lock"
    assert [s["id"] for s in d["all_signatures"]][:2] == ["tf_state_lock", "aws_throttling"]


def test_classify_signature_on_success_has_no_signature(ctx: ToolContext) -> None:
    ok = ctx.source.list_executions(None, {"status": "success"})[0]
    assert classify_signature(ctx, ok.execution_id)["signature"] is None


# ---- fleet_correlate ----


def test_fleet_correlate_finds_every_burst(ctx: ToolContext, truth: GroundTruth) -> None:
    assert len(truth.bursts) >= 3
    for burst in truth.bursts:
        members = list(burst["execution_ids"])
        kind = str(burst["shared_dimension"]).split(":")[0]
        for eid in members:
            d = fleet_correlate(ctx, eid)
            assert d["fleet_wide"], eid
            assert d["executions_affected"] == len(members), eid
            tied = [r["dimension"] for r in d["dimensions"] if r["count"] == d["executions_affected"]]
            assert kind in tied, (eid, tied)


def test_fleet_correlate_is_quiet_outside_bursts(ctx: ToolContext, truth: GroundTruth) -> None:
    for eid, label in truth.failures.items():
        if label.disposition == "close" or label.fleet_wide:
            continue
        assert not fleet_correlate(ctx, eid)["fleet_wide"], eid


def test_fleet_window_excludes_failures_outside_30_minutes(ctx: ToolContext, truth: GroundTruth) -> None:
    burst = truth.bursts[0]
    d = fleet_correlate(ctx, burst["execution_ids"][0])
    assert d["window_minutes"] == 30
    starts = {s.execution_id: s.started_at for s in ctx.source.list_executions(None, None)}
    from driftgate.tools.fleet_correlate import parse_time

    t0 = parse_time(starts[burst["execution_ids"][0]])
    for row in d["dimensions"]:
        for eid in row["executions"]:
            assert abs((parse_time(starts[eid]) - t0).total_seconds()) <= 30 * 60


# ---- flake_history ----


def test_flake_history_precedent_matches_labels_for_flaky_pipelines(ctx: ToolContext, truth: GroundTruth) -> None:
    flaky = _ids(truth, "transient_flaky_test")
    assert len(flaky) >= 6
    results = [flake_history(ctx, e) for e in flaky]
    assert [r["precedent"] for r in results] == [truth.failures[e].precedent_exists for e in flaky]
    assert any(r["precedent"] for r in results)
    assert all(r["precedent"] == bool(r["fail_then_pass_episodes"]) for r in results)


def test_flake_history_never_uses_future_episodes(ctx: ToolContext, truth: GroundTruth) -> None:
    flaky = sorted(_ids(truth, "transient_flaky_test"), key=lambda e: ctx.source.get_execution(e).started_at)
    first = flake_history(ctx, flaky[0])
    assert not first["precedent"] and first["fail_then_pass_episodes"] == []
    for r in (flake_history(ctx, e) for e in flaky):
        for ep in r["fail_then_pass_episodes"]:
            assert ep["passed_at"] < ctx.source.get_execution(r["execution_id"]).started_at


def test_flake_history_with_no_retries_has_no_precedent(ctx: ToolContext, truth: GroundTruth) -> None:
    eid = _ids(truth, "platform_oom_killed")[0]
    d = flake_history(ctx, eid)
    assert d["precedent"] is False


# ---- get_step_logs ----


def test_step_logs_only_failed_step_and_strips_noise(ctx: ToolContext, truth: GroundTruth) -> None:
    eid = _ids(truth, "platform_state_lock")[0]
    d = get_step_logs(ctx, eid)
    assert d["step"] == "Terraform Plan"
    assert "\x1b" not in d["text"]
    assert "Error acquiring the state lock" in d["text"]
    assert d["blocks"][0]["signature"] == "tf_state_lock"


def test_step_logs_returns_all_blocks_ranked_for_masked_lock(ctx: ToolContext, truth: GroundTruth) -> None:
    eid = _ids(truth, "platform_throttle_masks_lock")[0]
    d = get_step_logs(ctx, eid)
    sigs = [b["signature"] for b in d["blocks"]]
    assert sigs[:2] == ["tf_state_lock", "aws_throttling"]
    assert d["text"].index("state lock") < d["text"].index("ThrottlingException")


def test_step_logs_refuses_a_step_that_did_not_fail(ctx: ToolContext, truth: GroundTruth) -> None:
    eid = _ids(truth, "platform_state_lock")[0]
    tree = get_execution(ctx.source, eid)["tree"]
    ok_leaf = tree["children"][0]["children"][0]["node_id"]  # Checkout
    with pytest.raises(SourceError):
        get_step_logs(ctx, eid, ok_leaf)


def test_step_logs_budget_is_enforced_at_the_tool_boundary(ctx: ToolContext, truth: GroundTruth) -> None:
    eid = _ids(truth, "platform_state_lock")[0]
    small = get_step_logs(ctx, eid, max_chars=200)
    assert len(small["text"]) <= 200 and small["truncated"]
    huge = get_step_logs(ctx, eid, max_chars=10**6)
    assert huge["budget_chars"] == MAX_BUDGET_CHARS
    assert len(get_step_logs(ctx, eid)["text"]) <= DEFAULT_BUDGET_CHARS


def test_step_logs_strips_ansi_from_colored_logs(dataset_dir: Path, ctx: ToolContext) -> None:
    colored = [p for p in (dataset_dir / "logs").rglob("*.log") if "\x1b" in p.read_text(encoding="utf-8")]
    assert colored
    eid = colored[0].parent.name
    d = get_step_logs(ctx, eid)
    assert "\x1b" not in d["text"] and "[31m" not in d["text"]


def test_dataset_logs_survive_redaction_untouched(dataset_dir: Path, ctx: ToolContext, truth: GroundTruth) -> None:
    """Catalog text contains no credentials; redaction must not eat it."""
    for eid, label in truth.failures.items():
        if label.disposition == "close":
            continue
        assert "[REDACTED]" not in get_step_logs(ctx, eid)["text"], eid


# ---- read_repo_file ----


def test_read_repo_file_reads_the_fault_snapshot_and_main(ctx: ToolContext, truth: GroundTruth) -> None:
    eid = _ids(truth, "user_lockfile_mismatch")[0]
    ex = get_execution(ctx.source, eid)
    paths = ("package.json", "package-lock.json")
    at_fault = [read_repo_file(ctx, ex["pipeline"], p, ex["refs"]["commit"])["content"] for p in paths]
    on_main = [read_repo_file(ctx, ex["pipeline"], p, "main")["content"] for p in paths]
    assert all(at_fault) and all(on_main)
    assert ex["refs"]["commit"] != "main" and at_fault != on_main  # the snapshot holds the injected fault


def test_read_repo_file_is_size_capped(ctx: ToolContext) -> None:
    d = read_repo_file(ctx, "api-build", "package-lock.json", "main", max_bytes=50)
    assert len(d["content"]) <= 50 and d["truncated"]
    assert read_repo_file(ctx, "api-build", "package-lock.json", "main", max_bytes=10**9)["max_bytes"] == HARD_MAX_BYTES


@pytest.mark.parametrize(
    "args",
    [
        ("api-build", "../../ground_truth/labels.json", "main"),
        ("api-build", "package.json", "../.."),
        ("..", "x", "main"),
        ("api-build", "/etc/passwd", "main"),
        ("api-build", "missing.txt", "main"),
    ],
)
def test_read_repo_file_refuses_escapes_and_missing(ctx: ToolContext, args: tuple[str, str, str]) -> None:
    with pytest.raises(SourceError):
        read_repo_file(ctx, *args)


# ---- get_remediation_attempt ----


def test_get_remediation_attempt_reads_the_injected_store(ctx: ToolContext) -> None:
    store = AttemptStore()
    a1 = RemediationAttempt("att-1", "fp-a", "ex-1", 0, "rerun_failed_job", "executed", {"verified": False})
    a2 = RemediationAttempt("att-2", "fp-b", "ex-2", 3, "regenerate_lockfile", "executed", {"verified": True})
    store.add(a1)
    store.add(a2)
    local = ToolContext(ctx.source, store)
    by_fp = get_remediation_attempt(local, fingerprint="fp-a")
    assert [a["attempt_id"] for a in by_fp["attempts"]] == ["att-1"]
    assert by_fp["attempts"][0]["verification"] == {"verified": False}
    assert get_remediation_attempt(local, attempt_id="att-2")["attempts"][0]["tier"] == 3
    assert get_remediation_attempt(local, fingerprint="nope")["attempts"] == []
    with pytest.raises(SourceError):
        get_remediation_attempt(local, attempt_id="missing")
    with pytest.raises(ValueError):
        get_remediation_attempt(local)


# ---- registry and dispatcher ----


def test_registry_exposes_anthropic_format_definitions() -> None:
    defs = tool_definitions()
    assert {d["name"] for d in defs} == MVP_TOOLS
    for d in defs:
        assert set(d) == {"name", "description", "input_schema"}
        assert d["description"] and d["input_schema"]["type"] == "object"
    assert json.loads(json.dumps(defs)) == defs
    assert tool_definitions() == tool_definitions()  # stable order for prompt caching
    assert [d["name"] for d in tool_definitions(("get_execution",))] == ["get_execution"]
    assert len({t.name for t in TOOLS}) == len(TOOLS)


def test_dispatch_returns_result_with_the_callers_source_id(ctx: ToolContext, truth: GroundTruth) -> None:
    eid = _ids(truth, "platform_oom_killed")[0]
    r = dispatch(ctx, "classify_signature", {"execution_id": eid}, "toolu_01ABC")
    assert r.ok and r.source == "toolu_01ABC" and r.tool == "classify_signature"
    assert r.data["signature"] == "k8s_oom_killed"
    assert r.to_dict()["source"] == "toolu_01ABC"


def test_dispatch_reports_errors_instead_of_raising(ctx: ToolContext) -> None:
    bad = [
        dispatch(ctx, "nope", {}, "c1"),
        dispatch(ctx, "get_execution", {}, "c2"),
        dispatch(ctx, "get_execution", {"execution_id": "ex-missing"}, "c3"),
        dispatch(ctx, "get_execution", {"execution_id": "x", "extra": 1}, "c4"),
        dispatch(ctx, "get_remediation_attempt", {}, "c5"),
    ]
    assert [r.ok for r in bad] == [False] * 5
    assert [r.source for r in bad] == ["c1", "c2", "c3", "c4", "c5"]
    assert all(r.error for r in bad)


def test_every_tool_runs_through_the_dispatcher(ctx: ToolContext, truth: GroundTruth) -> None:
    eid = _ids(truth, "transient_throttling")[0]
    ex = get_execution(ctx.source, eid)
    calls = {
        "get_execution": {"execution_id": eid},
        "classify_signature": {"execution_id": eid},
        "fleet_correlate": {"execution_id": eid},
        "flake_history": {"execution_id": eid},
        "get_step_logs": {"execution_id": eid},
        "read_repo_file": {"repo": ex["pipeline"], "path": "scripts/plan.sh", "ref": "main"},
        "get_remediation_attempt": {"fingerprint": "none"},
    }
    assert set(calls) == MVP_TOOLS
    for i, (name, args) in enumerate(calls.items()):
        r = dispatch(ctx, name, args, f"call-{i}")
        assert r.ok, (name, r.error)
        json.dumps(r.data)  # structured and serializable
