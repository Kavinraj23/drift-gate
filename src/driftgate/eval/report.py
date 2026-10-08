"""The agent-vs-baseline evaluation report, offline (PRD "Evaluation"). Only `eval/` may read ground truth.

`python -m driftgate.eval.report --data data --out data/eval [--mode fake|replay|auto]`

Both systems run over the same labelled scenarios; dev and held-out are reported separately. The agent column is the
orchestrator driven by the scripted model (or replay fixtures where recorded), so offline it is correct by
construction: the table validates the pipeline, gates and metric code, not agent quality. The baseline column is the
real deterministic baseline. Output is deterministic for a given dataset (no timestamps), and nothing here touches
the network or an API key.

Metric definitions (all per set; `n` is shown beside every figure):
- acted: a remediation reached the target (a Tier 0 action executed or a Tier 3 PR opened), counting a first attempt
  that later failed verification. Approval-gated Tier 1/2 proposals and escalations are declined, not acted.
- correct action: acted, and the label says remediate with the same action.
- false remediation: acted without it being the correct action (label says escalate/close, or a different action).
  Rate over ALL cases; the split by cause is reported beside it.
- escalation precision: of the declined cases, the share where the label says declining was right.
- remediation success (verified / attempted): agent only, from `eval/metrics.py`. The baseline has no verification
  loop; its row shows the label-correct share of its actions as a proxy.
- recovery rate: of the cases whose first attempt failed, the share ended by a VERIFIED fix. Taken from the loop
  drills (the real gate stops most wrong first fixes before they run), never folded with correct escalations.
- tool efficiency: of the cases resolved correctly (correct action or correct decline), the share that used only
  cheap tools (everything except `get_step_logs` and `read_repo_file`, which the tool descriptions rate Medium).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from driftgate.adapters.synthetic import SyntheticSource
from driftgate.baseline import BaselineResult, run_baseline
from driftgate.eval import e2e
from driftgate.eval.e2e import CONFLICT, MODES, ScenarioResult
from driftgate.eval.ground_truth import FailureLabel, GroundTruth, load_ground_truth
from driftgate.eval.metrics import Rate, attempted_remediation, recovery_breakdown, remediation_success_rate
from driftgate.eval.tier3_scripts import BAD_KINDS, Tier3Score, bad_diff, score_tier3, tier3_quality
from driftgate.llm.config import DEFAULT_MODEL, DEFAULT_PRICES, ModelPrice, estimate_cost
from driftgate.llm.types import Usage
from driftgate.tools import ToolContext

MEDIUM_TOOLS = frozenset({"get_step_logs", "read_repo_file"})
SMALL_N = 10
SYSTEMS = ("baseline", "agent")
SETS = ("dev", "held-out")
PRICED_AS = DEFAULT_MODEL


@dataclass(frozen=True)
class CaseResult:
    """One scored case: the label facts that matter, and what the system did. System-agnostic."""

    scenario_id: str
    held_out: bool
    conflict: bool  # known label conflict (BLOCKERS.md); reported on its own line
    disposition: str  # label: remediate | escalate | close
    correct_action: str | None  # label, only meaningful when disposition == remediate
    true_layer: str
    true_classification: str
    acted: bool
    action: str  # what it acted with ("" when it declined)
    layer: str
    classification: str
    verified: bool | None
    input_tokens: int
    output_tokens: int
    model_calls: int
    tool_calls: int
    tools: tuple[str, ...]
    latency_s: float = 0.0

    @property
    def correct_action_taken(self) -> bool:
        return self.acted and self.disposition == "remediate" and self.action == self.correct_action

    @property
    def false_remediation(self) -> bool:
        return self.acted and not self.correct_action_taken

    @property
    def resolved_correctly(self) -> bool:
        return self.correct_action_taken if self.acted else self.disposition != "remediate"


@dataclass(frozen=True)
class Metrics:
    n: int
    false_remediation: Rate  # acted without it being right / all cases
    false_on_escalate_close: Rate  # of the cases the label says not to act on, how many it acted on
    false_wrong_action: Rate  # of the label-remediate cases, how many it acted on with a different action
    remediation_success: Rate | None  # agent only: verified / attempted
    correct_action_share: Rate  # acted and label-correct / acted (the baseline's proxy for success)
    remediation_recall: Rate  # label-remediate cases acted on correctly
    escalation_precision: Rate
    layer_accuracy: Rate
    classification_accuracy: Rate
    tool_efficiency: Rate
    mean_input_tokens: float
    mean_output_tokens: float
    mean_cost_usd: float
    mean_tool_calls: float
    mean_model_calls: float
    mean_latency_s: float


def _mean(values: Iterable[float], n: int) -> float:
    return sum(values) / n if n else 0.0


def case_cost(case: CaseResult, prices: dict[str, ModelPrice] | None = None, model: str = PRICED_AS) -> float:
    """USD from the configured price table (llm/config.py): tokens in `usage` x price per million tokens."""
    usage = Usage(input_tokens=case.input_tokens, output_tokens=case.output_tokens)
    if usage.input_tokens == 0 and usage.output_tokens == 0:
        return 0.0
    return estimate_cost(prices or DEFAULT_PRICES, model, usage)


def compute_metrics(
    cases: list[CaseResult], *, has_verification: bool, prices: dict[str, ModelPrice] | None = None
) -> Metrics:
    n = len(cases)
    acted = [c for c in cases if c.acted]
    declined = [c for c in cases if not c.acted]
    non_rem = [c for c in cases if c.disposition != "remediate"]
    rem = [c for c in cases if c.disposition == "remediate"]
    resolved = [c for c in cases if c.resolved_correctly]
    success = None
    if has_verification:
        success = Rate(sum(c.verified is True for c in acted), len(acted))
    return Metrics(
        n=n,
        false_remediation=Rate(sum(c.false_remediation for c in cases), n),
        false_on_escalate_close=Rate(sum(c.acted for c in non_rem), len(non_rem)),
        false_wrong_action=Rate(sum(c.acted and not c.correct_action_taken for c in rem), len(rem)),
        remediation_success=success,
        correct_action_share=Rate(sum(c.correct_action_taken for c in acted), len(acted)),
        remediation_recall=Rate(sum(c.correct_action_taken for c in rem), len(rem)),
        escalation_precision=Rate(sum(c.disposition != "remediate" for c in declined), len(declined)),
        layer_accuracy=Rate(sum(c.layer == c.true_layer for c in cases), n),
        classification_accuracy=Rate(sum(c.classification == c.true_classification for c in cases), n),
        tool_efficiency=Rate(sum(not (set(c.tools) & MEDIUM_TOOLS) for c in resolved), len(resolved)),
        mean_input_tokens=_mean((c.input_tokens for c in cases), n),
        mean_output_tokens=_mean((c.output_tokens for c in cases), n),
        mean_cost_usd=_mean((case_cost(c, prices) for c in cases), n),
        mean_tool_calls=_mean((c.tool_calls for c in cases), n),
        mean_model_calls=_mean((c.model_calls for c in cases), n),
        mean_latency_s=_mean((c.latency_s for c in cases), n),
    )


# -- building cases from real runs ---------------------------------------------------------------------------------
def agent_case(res: ScenarioResult) -> CaseResult:
    out, label = res.outcome, res.label
    if out is None or label is None:
        raise ValueError(f"{res.row.scenario_id} produced no outcome (no replay fixture); report it, do not score it")
    first = out.first_round or out  # a first attempt that later failed verification is still an action taken
    acted = attempted_remediation(out)
    rem = first.report.remediation
    names = [r.tool for r in out.earlier_tool_results] + [
        r.tool for r in (out.investigation.tool_results if out.investigation else [])
    ]
    run = out.report.run
    return CaseResult(
        scenario_id=res.row.scenario_id,
        held_out=res.row.held_out,
        conflict=res.row.status == CONFLICT,
        disposition=label.disposition,
        correct_action=label.correct_action,
        true_layer=label.true_layer,
        true_classification=label.true_classification,
        acted=acted,
        action=(rem.action if rem and acted else ""),
        layer=out.report.layer,
        classification=out.report.classification,
        verified=out.verified,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        model_calls=out.model_calls,
        tool_calls=out.tool_calls,
        tools=tuple(names),
        latency_s=run.latency_s,
    )


def baseline_case(label: FailureLabel, res: BaselineResult, held_out: bool, conflict: bool) -> CaseResult:
    rem = res.report.remediation
    return CaseResult(
        scenario_id=label.scenario_id or label.execution_id,
        held_out=held_out,
        conflict=conflict,
        disposition=label.disposition,
        correct_action=label.correct_action,
        true_layer=label.true_layer,
        true_classification=label.true_classification,
        acted=res.would_remediate,
        action=rem.action if rem else "",
        layer=res.report.layer,
        classification=res.report.classification,
        verified=None,
        input_tokens=0,
        output_tokens=0,
        model_calls=0,
        tool_calls=res.tool_calls,
        tools=("classify_signature", "fleet_correlate", "flake_history")[: res.tool_calls],
    )


# -- the report ----------------------------------------------------------------------------------------------------
@dataclass
class ReportData:
    mode: str
    models: list[str]
    cases: dict[str, dict[str, list[CaseResult]]]  # system -> set -> cases
    conflict_ids: list[str]
    recovery: dict[str, Any]
    tier3: dict[str, Any]
    bad_diff_drills: dict[str, Any]
    loop_drills: list[dict[str, Any]] = field(default_factory=list)


def _tier3_scores(results: list[ScenarioResult], source: SyntheticSource) -> list[Tier3Score]:
    return [
        score_tier3(r.label, source, r.outcome, r.audit)
        for r in results
        if r.label is not None and r.outcome is not None and r.label.correct_tier == 3
    ]


def _rate_json(r: Rate) -> dict[str, Any]:
    return {"numerator": r.numerator, "denominator": r.denominator, "value": r.value}


def bad_diff_drills(data_dir: Path, truth: GroundTruth, source: SyntheticSource) -> dict[str, Any]:
    """Seeded bad diffs through the real Tier 3 flow with the strict scripted reviewer: how many never open a PR."""
    scores: list[Tier3Score] = []
    seen_actions: set[str] = set()
    for label in truth.scenarios(held_out=False):  # one dev scenario per fix action keeps the drill small
        if label.correct_tier != 3 or label.fleet_wide or label.correct_action in seen_actions or not label.fix_diff:
            continue
        seen_actions.add(label.correct_action or "")
        for kind in BAD_KINDS:
            if bad_diff(kind, label, source) is None:  # the kind does not apply to this scenario's repo
                continue
            res = e2e.run_scenario(data_dir, label, False, mode="fake", variant=f"diff:{kind}")
            if res.outcome is None:
                continue
            s = score_tier3(label, source, res.outcome, res.audit)
            if s.bad_diff:  # kinds that do not apply to a repo produce no bad diff and are skipped
                scores.append(s)
    return tier3_quality(scores) | {"runs": len(scores)}


def build_report(
    data_dir: Path, *, mode: str = "auto", fixtures_dir: Path | None = None, with_drills: bool = True
) -> ReportData:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    truth = load_ground_truth(data_dir)
    source = SyntheticSource(data_dir)
    results = e2e.run_all(data_dir, mode=mode, fixtures_dir=fixtures_dir, truth=truth)
    scored = [r for r in results if r.outcome is not None]
    conflict_ids = sorted(r.row.scenario_id for r in scored if r.row.status == CONFLICT)
    ctx = ToolContext(source)
    agent_cases = [agent_case(r) for r in scored]
    base_cases = [
        baseline_case(
            r.label,  # type: ignore[arg-type]
            run_baseline(ctx, r.label.execution_id),  # type: ignore[union-attr]
            r.row.held_out,
            r.row.status == CONFLICT,
        )
        for r in scored
    ]
    cases = {
        name: {s: [c for c in lst if c.held_out == (s == "held-out")] for s in SETS}
        for name, lst in (("baseline", base_cases), ("agent", agent_cases))
    }
    drills = (
        e2e.run_all(data_dir, mode="fake", truth=truth, drills=True)[len(results) :]
        if with_drills and mode != "replay"
        else []
    )
    loop = [(r.label, r.outcome) for r in [*scored, *drills] if r.label is not None and r.outcome is not None]
    b = recovery_breakdown(loop)
    recovery = {
        "cases": len(loop),
        "success": _rate_json(remediation_success_rate(loop)),
        "recovered_by_verified_fix": _rate_json(b.fixed),
        "correct_escalation_after_attempt": _rate_json(b.correct_escalation),
        "not_recovered": _rate_json(b.not_recovered),
        "wrong_first_fix_stopped_by_gate": _rate_json(b.gate_blocked),
        "drills_run": len(drills),
    }
    quality = tier3_quality(_tier3_scores(scored, source))
    return ReportData(
        mode=mode,
        models=sorted({r.row.model for r in scored}),
        cases=cases,
        conflict_ids=conflict_ids,
        recovery=recovery,
        tier3=quality,
        bad_diff_drills=bad_diff_drills(data_dir, truth, source) if with_drills and mode != "replay" else {},
        loop_drills=[asdict(r.row) for r in drills],
    )


# -- rendering -----------------------------------------------------------------------------------------------------
HONESTY = (
    "**Read this before the numbers.** The agent column is a SCRIPTED model: offline it is correct by construction "
    "(it is written from the labels to exercise each path). So this table validates the pipeline, the safety gates "
    "and the metric code. It says NOTHING about how a real model performs. Real agent numbers require the "
    "human-run `record` and `eval-live` targets, which have not been run. The baseline column is real: it is the "
    "deterministic pipeline with no model calls. Where the scripted agent and the labels disagree, that is a gate "
    "or label finding (see the label-conflict line), not model skill. The synthetic dataset is generated; error "
    "text is copied from real sources but unverified (BLOCKERS.md). No production traffic is involved."
)


def fmt(r: Rate | None, na: str = "n/a") -> str:
    if r is None:
        return na
    if not r.denominator:
        return "n/a (n=0)"
    return f"{r.numerator}/{r.denominator} = {r.value:.0%}" + ("*" if r.denominator < SMALL_N else "")


_METRIC_ROWS: tuple[tuple[str, str], ...] = (
    ("False remediation rate (all cases; lower is better)", "false_remediation"),
    ("  of which: acted where label says escalate/close", "false_on_escalate_close"),
    ("  of which: acted with a different action than the label", "false_wrong_action"),
    ("Remediation success (verified / attempted)", "remediation_success"),
    ("  proxy: label-correct share of actions taken", "correct_action_share"),
    ("Remediation recall (label-remediate cases fixed correctly)", "remediation_recall"),
    ("Escalation precision", "escalation_precision"),
    ("Classification accuracy", "classification_accuracy"),
    ("Layer accuracy", "layer_accuracy"),
    ("Tool efficiency (resolved with cheap tools only)", "tool_efficiency"),
)


def _columns() -> list[tuple[str, str]]:
    return [(sys_, s) for s in SETS for sys_ in SYSTEMS]


def _md_table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def _metrics_table(data: ReportData, *, exclude_conflicts: bool) -> str:
    cols = _columns()
    metrics: dict[tuple[str, str], Metrics] = {}
    for sys_, s in cols:
        cs = [c for c in data.cases[sys_][s] if not (exclude_conflicts and c.conflict)]
        metrics[(sys_, s)] = compute_metrics(cs, has_verification=sys_ == "agent")
    header = ["metric"] + [f"{sys_} / {s} (n={metrics[(sys_, s)].n})" for sys_, s in cols]
    rows = []
    for label, attr in _METRIC_ROWS:
        rows.append([label] + [fmt(getattr(metrics[k], attr), "n/a (no verifier)") for k in cols])
    rows.append(["Recovery rate after a failed first attempt"] + [_recovery_cell(data, k) for k in cols])
    return _md_table(header, rows)


def _recovery_cell(data: ReportData, key: tuple[str, str]) -> str:
    if key[0] == "baseline":
        return "n/a (never retries)"
    if key[1] == "held-out":
        return "n/a (no held-out drills)"
    r = data.recovery["recovered_by_verified_fix"]
    return f"{r['numerator']}/{r['denominator']} (drills, dev)" + ("*" if r["denominator"] < SMALL_N else "")


def _cost_table(data: ReportData) -> str:
    cols = _columns()
    ms = {k: compute_metrics(data.cases[k[0]][k[1]], has_verification=k[0] == "agent") for k in cols}
    header = ["per case (mean)"] + [f"{sys_} / {s}" for sys_, s in cols]
    spec: tuple[tuple[str, Callable[[Metrics], str]], ...] = (
        ("input tokens", lambda m: f"{m.mean_input_tokens:,.0f}"),
        ("output tokens", lambda m: f"{m.mean_output_tokens:,.0f}"),
        (f"USD (price table, as {PRICED_AS}; nothing was spent)", lambda m: f"${m.mean_cost_usd:.4f}"),
        ("tool calls", lambda m: f"{m.mean_tool_calls:.1f}"),
        ("model calls", lambda m: f"{m.mean_model_calls:.1f}"),
        ("latency (s; scripted model, not meaningful offline)", lambda m: f"{m.mean_latency_s:.1f}"),
    )
    return _md_table(header, [[name] + [f(ms[k]) for k in cols] for name, f in spec])


def readme_tables(data: ReportData) -> str:
    """The block embedded in README.md under "Eval results (offline)"; a test keeps it in sync with the report."""
    return "\n".join(
        [
            "Label-conflict cases counted as misses:",
            "",
            _metrics_table(data, exclude_conflicts=False),
            "",
            "Label-conflict cases excluded:",
            "",
            _metrics_table(data, exclude_conflicts=True),
        ]
    )


def render_markdown(data: ReportData) -> str:
    models = ", ".join(data.models)
    mode_note = {
        "fake": "scripted fake model for every case",
        "replay": "gateway replay of recorded fixtures",
        "auto": "replay where fixtures exist, otherwise the scripted fake model",
    }[data.mode]
    mixed = any(m.startswith("fake") for m in data.models)
    t3, rec, bad = data.tier3, data.recovery, data.bad_diff_drills
    all_conflict = [c for s in SETS for c in data.cases["agent"][s] if c.conflict]
    n_cases = sum(len(v) for v in data.cases["agent"].values())
    lines = [
        "# DriftGate offline evaluation: agent vs. deterministic baseline",
        "",
        f"Mode: `{data.mode}` ({mode_note}). Agent model source: {models}."
        + (" The agent column is SCRIPTED." if mixed else ""),
        f"Cases: {n_cases} labelled synthetic scenarios (dev and held-out reported separately).",
        "",
        "## Honesty",
        "",
        HONESTY,
        "",
        "Figures are `k/n = rate`. A trailing `*` marks n < "
        f"{SMALL_N}: too small to read as a rate. Every held-out cell is small.",
        "",
        "## Metrics, label-conflict cases counted as misses",
        "",
        _metrics_table(data, exclude_conflicts=False),
        "",
        "## Metrics, label-conflict cases excluded",
        "",
        _metrics_table(data, exclude_conflicts=True),
        "",
        f"Label-conflict footnote: {len(data.conflict_ids)} scenarios ({', '.join(data.conflict_ids) or 'none'}) are "
        "flaky-test failures that the generator labels Tier 0 via flake precedent, but whose only signature is the "
        "generic exit code. Invariant 3 needs a deterministic signature match, so the gate (correctly) escalates "
        "them and both systems 'miss' the label. This is an open label question in BLOCKERS.md, not a model result. "
        f"Dev/held-out split of these cases: {sum(not c.held_out for c in all_conflict)} dev, "
        f"{sum(c.held_out for c in all_conflict)} held-out. "
        "The first table counts them as misses; the second removes them.",
        "",
        "## Cost and latency per case",
        "",
        _cost_table(data),
        "",
        "## Verification loop (agent only)",
        "",
        f"Computed over {rec['cases']} cases: the labelled scenarios plus {rec['drills_run']} scripted loop drills "
        "(a wrong first fix is injected, because the real gate stops most before they run).",
        "",
        f"- Remediation success (verified / attempted): {_fmt_json(rec['success'])}",
        f"- Recovery rate (verified fix after a failed first attempt): {_fmt_json(rec['recovered_by_verified_fix'])}",
        "- Correct escalation after a failed attempt (not counted as recovery): "
        f"{_fmt_json(rec['correct_escalation_after_attempt'])}",
        f"- Not recovered: {_fmt_json(rec['not_recovered'])}",
        f"- Wrong first fix stopped by the gate before it ran: {_fmt_json(rec['wrong_first_fix_stopped_by_gate'])}",
        "- Baseline: not applicable; it never verifies or retries.",
        "",
        "## Tier 3 PR quality (agent only; the baseline cannot write a diff)",
        "",
        f"- Diffs proposed on labelled scenarios: {t3['diffs_proposed']}; matching the injected fault's correct fix "
        f"(by resulting-file hash): {t3['correct_diffs']}; correct diffs approved: {t3['correct_diffs_approved']}; "
        f"correct diffs rejected: {t3['correct_diffs_rejected']}.",
    ]
    if bad:
        lines.append(
            f"- Seeded bad diffs ({bad['runs']} runs, {len(BAD_KINDS)} kinds, strict scripted reviewer): "
            f"caught {bad['bad_diffs_caught']}/{bad['bad_diffs']} (no PR opened), missed {bad['bad_diffs_missed']}. "
            "Several kinds are stopped by deterministic diff checks before the reviewer sees them; the reviewer here "
            "is scripted, so a real reviewer's catch rate is unmeasured."
        )
    else:
        lines.append("- Seeded bad-diff drills were not run in this mode.")
    lines += [
        "",
        "## What this does and does not show",
        "",
        "- Shows: the orchestrator, gates, audit, verification loop and metric code run end to end and agree with the "
        "labels on every path, and the baseline's real limit: it can only re-run, so it fixes few of the cases "
        "that need a diff (low recall), though it did not act wrongly on this dataset.",
        "- Does not show: real-model accuracy, real-model false remediation, real cost or latency, or generalization "
        "beyond the generator's templates. Those need `record` + `eval-live` (human-run) and a real-log holdout.",
    ]
    return "\n".join(lines) + "\n"


def _fmt_json(d: dict[str, Any]) -> str:
    return fmt(Rate(d["numerator"], d["denominator"]))


def to_json(data: ReportData) -> dict[str, Any]:
    out: dict[str, Any] = {
        "mode": data.mode,
        "models": data.models,
        "honesty": HONESTY,
        "label_conflicts": data.conflict_ids,
        "recovery": data.recovery,
        "tier3": data.tier3,
        "bad_diff_drills": data.bad_diff_drills,
        "loop_drills": data.loop_drills,
        "metrics": {},
    }
    for variant, exclude in (("conflicts_counted", False), ("conflicts_excluded", True)):
        block: dict[str, Any] = {}
        for sys_ in SYSTEMS:
            for s in SETS:
                cs = [c for c in data.cases[sys_][s] if not (exclude and c.conflict)]
                m = compute_metrics(cs, has_verification=sys_ == "agent")
                block[f"{sys_}/{s}"] = {
                    k: (_rate_json(v) if isinstance(v, Rate) else v) for k, v in asdict_metrics(m).items()
                }
        out["metrics"][variant] = block
    return out


def asdict_metrics(m: Metrics) -> dict[str, Any]:
    return {f: getattr(m, f) for f in m.__dataclass_fields__}


def write_report(data: ReportData, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md, js = out_dir / "report.md", out_dir / "report.json"
    md.write_text(render_markdown(data), encoding="utf-8")
    js.write_text(json.dumps(to_json(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return md, js


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--out", type=Path, default=Path("data") / "eval")
    ap.add_argument("--mode", choices=MODES, default="auto")
    ap.add_argument("--fixtures", type=Path, default=e2e.DEFAULT_FIXTURES)
    args = ap.parse_args(argv)
    data = build_report(args.data, mode=args.mode, fixtures_dir=args.fixtures)
    md, js = write_report(data, args.out)
    print(render_markdown(data))
    print(f"wrote {md} and {js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
