"""M1 acceptance tests for the seeded synthetic dataset generator."""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from driftgate.eval.ground_truth import load_ground_truth
from driftgate.generator.catalog import load_catalog
from driftgate.generator.population import Dataset, generate
from driftgate.generator.writer import DatasetValidationError, validate_dataset, validate_files, write_dataset

SRC = Path(__file__).resolve().parents[2] / "src" / "driftgate"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z ")


@pytest.fixture(scope="module")
def ds() -> Dataset:
    return generate(seed=7)


def _json(ds: Dataset, rel: str) -> object:
    return json.loads(ds.files[rel])


def _ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")


def test_same_seed_is_byte_identical(ds: Dataset) -> None:
    again = generate(seed=7)
    assert again.files == ds.files


def test_different_seed_differs(ds: Dataset) -> None:
    assert generate(seed=8).files["executions.json"] != ds.files["executions.json"]


def test_dataset_shape(ds: Dataset) -> None:
    execs = _json(ds, "executions.json")
    manifest = _json(ds, "manifest.json")
    assert 12 == len({e["pipeline"] for e in execs})
    span = max(_ts(e["started_at"]) for e in execs) - min(_ts(e["started_at"]) for e in execs)
    assert timedelta(days=28) <= span <= timedelta(days=31)
    assert len(manifest["pipelines"]) == 12


def test_base_rates_within_tolerance(ds: Dataset) -> None:
    s = ds.stats
    assert 0.77 <= s["success"] / s["executions"] <= 0.83
    by_class = s["failures_by_class"]
    n = by_class["user"] + by_class["platform"] + by_class["transient"]
    assert 0.52 <= by_class["user"] / n <= 0.68
    assert 0.19 <= by_class["platform"] / n <= 0.31
    assert 0.09 <= by_class["transient"] / n <= 0.21


def test_correlated_bursts_present(ds: Dataset) -> None:
    labels = _json(ds, "ground_truth/labels.json")
    execs = {e["execution_id"]: e for e in _json(ds, "executions.json")}
    assert 3 <= len(labels["bursts"]) <= 4
    for burst in labels["bursts"]:
        members = [execs[i] for i in burst["execution_ids"]]
        assert len(members) >= 3
        times = [_ts(m["started_at"]) for m in members]
        assert max(times) - min(times) <= timedelta(minutes=20)
        dim, value = burst["shared_dimension"].split(":")
        key = {"template": "template", "connector": "connector", "runner_pool": "runner_pool"}[dim]
        assert all(m["refs"][key].split("@")[0] == value for m in members)
        assert all(m["status"] == "failed" for m in members)


def test_flaky_pipelines_fail_then_pass_on_retry(ds: Dataset) -> None:
    labels = _json(ds, "ground_truth/labels.json")
    execs = {e["execution_id"]: e for e in _json(ds, "executions.json")}
    assert len(labels["flaky_pipelines"]) == 2
    for name in labels["flaky_pipelines"]:
        pairs = [
            e
            for e in execs.values()
            if e["pipeline"] == name
            and "retry_of" in e["refs"]
            and e["status"] == "success"
            and execs[e["refs"]["retry_of"]]["status"] == "failed"
        ]
        assert len(pairs) >= 3


def test_change_timeline_has_decoys(ds: Dataset) -> None:
    changes = _json(ds, "changes.json")
    labels = _json(ds, "ground_truth/labels.json")
    causal = {c for f in labels["failures"].values() for c in f["causal_change_ids"]}
    assert causal and causal < {c["change_id"] for c in changes}
    decoys = [c for c in changes if c["change_id"] not in causal]
    assert len(decoys) > 20
    assert any(f["decoy_change_ids"] for f in labels["failures"].values())


def test_scenario_coverage_and_held_out(ds: Dataset) -> None:
    labels = _json(ds, "ground_truth/labels.json")
    manifest = _json(ds, "ground_truth/scenarios.json")
    assert 8 <= len(manifest["held_out_scenarios"]) <= 10
    assert {sc["scenario_id"] for sc in manifest["scenarios"] if sc["held_out"]} == set(manifest["held_out_scenarios"])
    families = {labels["failures"][sc["execution_id"]]["fault_id"] for sc in manifest["scenarios"]}
    needed = {
        "user_lockfile_mismatch",
        "user_provider_pin",
        "user_undefined_variable",
        "transient_flaky_test",
        "transient_throttling",
        "governance_approval_rejected",
        "platform_throttle_masks_lock",
        "platform_template_bump",
        "platform_expired_token",
        "platform_oom_killed",
    }
    assert needed <= families
    assert any(f["fleet_wide"] and f["disposition"] == "escalate" for f in labels["failures"].values())
    assert any(f["first_fix_fails"] for f in labels["failures"].values())


def test_tier3_failures_have_repo_snapshot_and_fix(ds: Dataset) -> None:
    labels = _json(ds, "ground_truth/labels.json")
    execs = {e["execution_id"]: e for e in _json(ds, "executions.json")}
    tier3 = [f for f in labels["failures"].values() if f["correct_tier"] == 3]
    assert tier3
    followups = _json(ds, "world/followups.json")
    for f in tier3:
        e = execs[f["execution_id"]]
        sha = e["refs"]["commit"]
        assert sha != "main"
        assert f["fix_diff"] and f["fix_paths"]
        for path in f["fix_paths"]:
            assert f"repos/{e['pipeline']}/{sha}/{path}" in ds.files or f"repos/{e['pipeline']}/main/{path}" in ds.files
        assert set(followups[f["execution_id"]]["fix_applied"]["accepted_files"]) == set(f["fix_paths"])


def test_lockfile_fault_is_really_out_of_sync(ds: Dataset) -> None:
    labels = _json(ds, "ground_truth/labels.json")
    f = next(f for f in labels["failures"].values() if f["fault_id"] == "user_lockfile_mismatch")
    sha = next(e for e in _json(ds, "executions.json") if e["execution_id"] == f["execution_id"])["refs"]["commit"]
    root = f"repos/{f['pipeline']}/{sha}/"
    pkg = json.loads(ds.files[root + "package.json"])
    lock = json.loads(ds.files[root + "package-lock.json"])
    assert set(pkg["dependencies"]) - set(lock["packages"][""]["dependencies"]) == {"dayjs"}


def test_every_error_string_has_a_source() -> None:
    catalog = load_catalog()
    raw = json.loads((SRC / "generator" / "error_catalog.json").read_text(encoding="utf-8"))
    assert raw["entries"]
    for entry in raw["entries"]:
        assert entry["tool"].strip() and len(entry["source"].strip()) >= 10, entry["id"]
        assert catalog[entry["id"]].source == entry["source"]
        # Invariant 9: nothing here is a captured verbatim sample, and the catalog must say so honestly.
        assert isinstance(entry["verified"], bool), entry["id"]
        assert entry["source_kind"] in ("recalled-from-widely-posted-output", "doc-quote", "captured-verbatim"), entry[
            "id"
        ]
        if entry["verified"]:
            assert entry["source_kind"] == "captured-verbatim", entry["id"]
    assert "copied from real" not in raw["note"] and "Nothing is invented" not in raw["note"]


def test_logs_contain_only_catalog_text(ds: Dataset) -> None:
    """Invariant 9: every log line is the rendering of a catalogued entry, nothing hand-written."""
    catalog = load_catalog()
    index = _json(ds, "ground_truth/catalog_use.json")
    assert index
    for path, entries in index.items():
        expected: list[str] = []
        for use in entries:
            entry = catalog[use["entry"]]
            expected += [line for line in entry.render(use["params"])]
        got = []
        for raw in ds.files[path].splitlines():
            line = TS.sub("", ANSI.sub("", raw))
            if line in ("╷", "╵"):
                continue
            got.append(line[2:] if line.startswith("│ ") or line == "│" else line)
        assert [g.rstrip() for g in got] == [e.rstrip() for e in expected], path


def test_logs_exist_for_every_failed_step_except_governance(ds: Dataset) -> None:
    execs = _json(ds, "executions.json")
    labels = _json(ds, "ground_truth/labels.json")

    def leaves(n: dict) -> list[dict]:
        return [x for c in n["children"] for x in leaves(c)] if n["children"] else [n]

    for e in execs:
        if e["status"] != "failed":
            continue
        failed = [n for n in leaves(e["root"]) if n["status"] == "failed"]
        assert len(failed) == 1, e["execution_id"]
        assert f"logs/{e['execution_id']}/{failed[0]['node_id']}.log" in ds.files
        assert labels["failures"][e["execution_id"]]["true_classification"] != "governance"


AGENT_HIDDEN_DIRS = ("ground_truth/", "world/")
FORBIDDEN_KEYS = (
    "true_layer",
    "true_classification",
    "correct_tier",
    "fix_diff",
    "fault_id",
    "causal_change",
    "flaky",
    "held_out",
    "first_fix_fails",
)


def test_ground_truth_not_leaked_into_agent_visible_data(ds: Dataset) -> None:
    catalog_ids = load_catalog().ids()
    scenario_ids = [sc["scenario_id"] for sc in _json(ds, "ground_truth/scenarios.json")["scenarios"]]
    assert scenario_ids and catalog_ids
    visible = [rel for rel in ds.files if not rel.startswith(AGENT_HIDDEN_DIRS)]
    assert "manifest.json" in visible and "executions.json" in visible
    for rel in visible:
        text = ds.files[rel]
        for key in FORBIDDEN_KEYS:
            assert key not in text, f"{key} leaked into {rel}"
        for token in (*scenario_ids, *catalog_ids):
            assert token not in text, f"{token} leaked into {rel}"


def test_followups_structure_is_uniform(ds: Dataset) -> None:
    followups = _json(ds, "world/followups.json")
    execs = {e["execution_id"] for e in _json(ds, "executions.json")}
    assert set(followups) <= execs
    for entry in followups.values():
        assert set(entry) == {"rerun", "fix_applied"}
        assert set(entry["rerun"]) == {"status", "log"}
        assert set(entry["fix_applied"]) == {"status", "accepted_files"}


def test_simulator_world_only_referenced_by_synthetic_adapter_generator_and_eval() -> None:
    """followups.json is simulator behaviour; agents and tools must never see the `world` directory."""
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith(("generator/", "eval/", "adapters/synthetic")):
            continue
        text = path.read_text(encoding="utf-8")
        if "followups" in text or re.search(r"""["'/]world[/"']""", text):
            offenders.append(rel)
    assert offenders == []


def test_nothing_outside_eval_and_generator_touches_ground_truth() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith(("eval/", "generator/")):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or "", *[f"{node.module}.{a.name}" for a in node.names]]
            if any(m.startswith(("driftgate.eval", "driftgate.generator")) for m in mods):
                offenders.append(rel)
    assert offenders == []


def test_schema_validation_and_eval_reader_round_trip(ds: Dataset, tmp_path: Path) -> None:
    validate_files(ds.files)
    write_dataset(ds, tmp_path / "data")
    validate_dataset(tmp_path / "data")
    gt = load_ground_truth(tmp_path / "data")
    labels = _json(ds, "ground_truth/labels.json")
    assert set(gt.failures) == set(labels["failures"])
    assert len(gt.scenarios(held_out=True)) == len(_json(ds, "ground_truth/scenarios.json")["held_out_scenarios"])
    assert len(gt.scenarios(held_out=False)) + len(gt.scenarios(held_out=True)) == len(gt.scenarios())
    assert gt.flaky_pipelines and gt.bursts


def test_schema_validation_rejects_bad_data(ds: Dataset) -> None:
    bad = dict(ds.files)
    execs = json.loads(bad["executions.json"])
    execs[0]["status"] = "bogus"
    bad["executions.json"] = json.dumps(execs)
    with pytest.raises(DatasetValidationError):
        validate_files(bad)


def test_simulator_world_isolation_is_ast_based() -> None:
    """Stricter companion to the substring test: string literals, f-string parts and identifiers are inspected."""
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith(("generator/", "eval/", "adapters/synthetic")):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            texts: list[str] = []
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                texts.append(node.value)
            elif isinstance(node, ast.Name):
                texts.append(node.id)
            elif isinstance(node, ast.Attribute):
                texts.append(node.attr)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                texts += [a.name for a in node.names] + [getattr(node, "module", None) or ""]
            for t in texts:
                if "followups" in t or re.search(r"(^|[/\\])world([/\\]|$)", t):
                    offenders.append(f"{rel}: {t!r}")
    assert offenders == []


LEAK_TOKENS = ("canary", "flaky", "flake", "intermittent", "retry")


def _pipeline_signature(ds: Dataset, pipeline: str) -> tuple[set[str], set[str], set[str]]:
    """Names, step names and file paths a model could see for one pipeline (agent-visible data only)."""
    execs = [e for e in _json(ds, "executions.json") if e["pipeline"] == pipeline]  # type: ignore[union-attr]
    manifest = _json(ds, "manifest.json")
    entry = next(p for p in manifest["pipelines"] if p["name"] == pipeline)  # type: ignore[index]
    names = {pipeline, entry["ecosystem"] if "ecosystem" in entry else entry.get("eco", "")}
    names |= {str(v) for v in entry.values() if isinstance(v, str)}

    steps: set[str] = set()

    def walk(n: dict) -> None:
        steps.add(n["name"])
        for c in n.get("children", []):
            walk(c)

    for e in execs:
        for child in e["root"]["children"]:  # the root node is named after the pipeline itself
            walk(child)
    prefix_hits = {
        rel for rel in ds.files if f"/{pipeline}/" in f"/{rel}" and not rel.startswith(("ground_truth/", "world/"))
    }
    paths = set()
    for rel in prefix_hits:
        parts = rel.split("/")
        paths.add("/".join(parts[parts.index(pipeline) + 2 :]) if pipeline in parts else rel)
    return names, steps, paths


def test_flaky_pipelines_not_identifiable_from_names_steps_or_files(ds: Dataset) -> None:
    labels = _json(ds, "ground_truth/labels.json")
    flaky = set(labels["flaky_pipelines"])  # type: ignore[index]
    pipelines = [p["name"] for p in _json(ds, "manifest.json")["pipelines"]]  # type: ignore[index]
    ordinary = [p for p in pipelines if p not in flaky]
    assert len(flaky) == 2 and ordinary

    visible_text = {rel: t for rel, t in ds.files.items() if not rel.startswith(("ground_truth/", "world/"))}
    for p in flaky:
        names, steps, paths = _pipeline_signature(ds, p)
        for item in names | steps | paths:
            assert not any(tok in item.lower() for tok in LEAK_TOKENS), (p, item)
        # Step names and file paths must be ones an ordinary pipeline also uses.
        assert steps
        assert any(steps <= _pipeline_signature(ds, o)[1] for o in ordinary), (p, steps)
        assert any(paths <= _pipeline_signature(ds, o)[2] for o in ordinary), (p, paths)
    # Repo trees and logs for flaky pipelines carry no flake vocabulary either.
    for rel, text in visible_text.items():
        if any(f"/{p}/" in f"/{rel}" for p in flaky):
            assert not any(tok in text.lower() for tok in ("canary", "intermittent")), rel

    # Ordinary pipelines also fail with ordinary faults, so recurring failure alone is not a tell.
    failing = {e["pipeline"] for e in _json(ds, "executions.json") if e["status"] == "failed"}  # type: ignore[union-attr]
    assert len(failing - flaky) >= len(ordinary) - 2
