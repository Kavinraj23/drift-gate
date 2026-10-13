"""`similar_history` on vs. off over the synthetic scenarios, offline (PRD MVP 2, N2). `python tasks.py eval-retrieval`.

Both arms run every labelled scenario through the orchestrator with the SCRIPTED model, in start-time order. In the
"on" arm the tool is offered and a shared index is filled as incidents finish; the index is built only from
earlier synthetic incidents, using only agent-visible report fields (never a label), and every query excludes the
case itself and anything that did not finish strictly before the case started.

What this does and does not show. The scripted model is written from the labels and never decides anything about
the tool; the "on" arm adds one scripted call to it on the first turn so the plumbing (index, query, tool-call
budget, citation, redaction) is exercised end to end. The metric differences are therefore pipeline effects, not
model quality, and say nothing about whether retrieval helps a real model. A recorded real-model on/off comparison
needs fixtures that do not exist yet (the human-run `record` target), so the shipping verdict below is always
"stays off" for now. Nothing here touches the network or an API key.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from driftgate.adapters.synthetic import SyntheticSource
from driftgate.eval import e2e
from driftgate.eval.e2e import ScenarioResult
from driftgate.eval.ground_truth import FailureLabel, load_ground_truth
from driftgate.eval.metrics import Rate
from driftgate.eval.report import SMALL_N, CaseResult, agent_case, compute_metrics, fmt
from driftgate.llm.types import ModelClient, ModelRequest, ModelResponse, ToolCall
from driftgate.retrieval import HistoryIndex
from driftgate.tools import ToolResult
from driftgate.tools.similar_history import NAME as TOOL_NAME
from driftgate.tools.similar_history import start_epoch

#: A difference smaller than this many cases is noise at this sample size (PRD risks: report counts, and do not claim
#: differences of one or two cases as improvements).
MIN_CASES = 3
#: Mean cost per case must fall by at least this share to count as a cost improvement.
MIN_COST_DROP = 0.10
NO_EVIDENCE = "stays off: no recorded evidence"


# -- the shipping rule (pure) ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ArmStats:
    """The five figures the shipping rule compares, as counts (a rate is numerator / denominator)."""

    recall: Rate  # label-remediate cases fixed correctly
    success: Rate  # verified / attempted
    false_remediation: Rate  # acted without it being right / all cases
    escalation_precision: Rate
    mean_cost_usd: float


@dataclass(frozen=True)
class Verdict:
    enabled_by_default: bool
    reason: str


def _rate_lt(a: Rate, b: Rate) -> bool:
    """a < b, comparing fractions exactly; an empty denominator is treated as 0."""
    if not a.denominator or not b.denominator:
        return False
    return a.numerator * b.denominator < b.numerator * a.denominator


def shipping_verdict(off: ArmStats, on: ArmStats, *, recorded: bool) -> Verdict:
    """The PRD's rule: enabled by default only if retrieval improves remediation success (recall, or verified over
    attempted) or cost per case without raising the false remediation rate; otherwise it stays off.

    `recorded` is True only when both arms come from recorded real-model runs. Scripted runs prove plumbing, never
    benefit, so they can never enable the tool. "Improves" means by at least MIN_CASES cases (recall; verified
    count) or a MIN_COST_DROP fall in mean cost with no loss of recall.
    """
    if not recorded:
        return Verdict(False, NO_EVIDENCE)
    if _rate_lt(off.false_remediation, on.false_remediation):
        return Verdict(
            False,
            f"stays off: false remediation rate rose ({off.false_remediation.numerator}/"
            f"{off.false_remediation.denominator} -> {on.false_remediation.numerator}/"
            f"{on.false_remediation.denominator})",
        )
    recall_gain = on.recall.numerator - off.recall.numerator
    verified_gain = on.success.numerator - off.success.numerator
    success_better = verified_gain >= MIN_CASES and not _rate_lt(on.success, off.success)
    cost_better = (
        off.mean_cost_usd > 0
        and on.mean_cost_usd <= off.mean_cost_usd * (1 - MIN_COST_DROP)
        and recall_gain >= 0
        and not _rate_lt(on.success, off.success)
    )
    if recall_gain >= MIN_CASES or success_better or cost_better:
        what = [
            *([f"recall +{recall_gain} cases"] if recall_gain >= MIN_CASES else []),
            *([f"verified fixes +{verified_gain}"] if success_better else []),
            *(["mean cost per case lower"] if cost_better else []),
        ]
        return Verdict(True, f"enabled by default: {', '.join(what)}, false remediation rate not raised")
    return Verdict(
        False,
        f"stays off: no improvement beyond noise (needs +{MIN_CASES} cases of recall or verified fixes, or a "
        f"{MIN_COST_DROP:.0%} lower cost per case)",
    )


# -- the scripted plumbing probe ------------------------------------------------------------------------------------
class HistoryProbe:
    """Decorates a scripted model client: on its first tool-calling turn, when `similar_history` is among the offered
    tools, it adds one call to it. This stands in for 'the agent decided to call it'; a scripted model never decides."""

    def __init__(self, inner: ModelClient, execution_id: str, serial: int) -> None:
        self._inner = inner
        self._execution_id = execution_id
        self._serial = serial
        self._done = False

    def complete(self, request: ModelRequest) -> ModelResponse:
        resp = self._inner.complete(request)
        offered = any(t.get("name") == TOOL_NAME for t in request.tools)
        if offered and not self._done and resp.tool_calls and all(c.name != "submit_report" for c in resp.tool_calls):
            self._done = True
            call = ToolCall(f"toolu_{self._execution_id}_sh{self._serial}", TOOL_NAME, {})
            resp = ModelResponse(resp.text, [*resp.tool_calls, call], resp.usage, resp.stop_reason)
        return resp


def probe_wrapper(execution_id: str) -> Callable[[ModelClient], ModelClient]:
    count = [0]

    def wrap(client: ModelClient) -> ModelClient:
        count[0] += 1
        return HistoryProbe(client, execution_id, count[0])

    return wrap


# -- running the arms -----------------------------------------------------------------------------------------------
@dataclass
class Arm:
    name: str
    cases: list[CaseResult] = field(default_factory=list)
    results: list[ScenarioResult] = field(default_factory=list)


@dataclass(frozen=True)
class Plumbing:
    """What the 'on' arm did with the tool, and the leakage self-check (violations must be 0)."""

    cases: int
    cases_calling_tool: int
    calls_ok: int
    calls_failed: int
    calls_with_matches: int
    matches_returned: int
    leak_violations: int
    index_size: int


def _scenarios(data_dir: Path) -> list[tuple[FailureLabel, bool, float]]:
    """Every labelled scenario with its set and start time, in start-time order (ties broken by scenario id)."""
    truth = load_ground_truth(data_dir)
    source = SyntheticSource(data_dir)
    rows = [
        (label, held, start_epoch(source.get_execution(label.execution_id).started_at))
        for held in (False, True)
        for label in truth.scenarios(held_out=held)
    ]
    return sorted(rows, key=lambda r: (r[2], r[0].scenario_id or ""))


def run_arms(data_dir: Path) -> tuple[Arm, Arm, Plumbing]:
    off, on = Arm("off"), Arm("on")
    index = HistoryIndex(":memory:")
    leaks = 0
    for label, held, start in _scenarios(data_dir):
        r_off = e2e.run_scenario(data_dir, label, held, mode="fake")
        r_on = e2e.run_scenario(
            data_dir,
            label,
            held,
            mode="fake",
            wrap=probe_wrapper(label.execution_id),
            history=index,
            offer_similar_history=True,
        )
        for arm, res in ((off, r_off), (on, r_on)):
            arm.results.append(res)
            if res.outcome is not None:
                arm.cases.append(agent_case(res))
        leaks += _leaks(label.execution_id, start, r_on)
    stats = _plumbing(on.results, leaks, len(index))
    return off, on, stats


def _tool_results(res: ScenarioResult) -> list[ToolResult]:
    out = res.outcome
    if out is None:
        return []
    rounds = [*out.earlier_tool_results, *(out.investigation.tool_results if out.investigation else [])]
    return [r for r in rounds if r.tool == TOOL_NAME]


def _leaks(case_id: str, start: float, res: ScenarioResult) -> int:
    """Self-check against the independently known case: `case_id` and `start` come from the scenario list, not from
    what the tool was given. See `_leaks_in`."""
    return _leaks_in(_tool_results(res), case_id, start)


def _leaks_in(results: list[ToolResult], case_id: str, start: float) -> int:
    """Violations in tool results for the case (`case_id`, started at epoch `start`): a result answering for another
    execution (a mis-bound call), or any returned match that is the case itself or did not finish strictly before
    the case started."""
    bad = 0
    for r in results:
        if not r.ok:
            continue
        if r.data.get("execution_id") != case_id:
            bad += 1
        for m in r.data.get("matches", []):
            if m["execution_id"] == case_id or start_epoch(m["finished_at"]) >= start:
                bad += 1
    return bad


def _plumbing(results: list[ScenarioResult], leaks: int, index_size: int) -> Plumbing:
    calls = [r for res in results for r in _tool_results(res)]
    ok = [r for r in calls if r.ok]
    with_matches = [r for r in ok if r.data.get("matches")]
    return Plumbing(
        cases=len(results),
        cases_calling_tool=sum(1 for res in results if _tool_results(res)),
        calls_ok=len(ok),
        calls_failed=len(calls) - len(ok),
        calls_with_matches=len(with_matches),
        matches_returned=sum(len(r.data["matches"]) for r in with_matches),
        leak_violations=leaks,
        index_size=index_size,
    )


def arm_stats(arm: Arm) -> ArmStats:
    m = compute_metrics(arm.cases, has_verification=True)
    assert m.remediation_success is not None
    return ArmStats(
        m.remediation_recall, m.remediation_success, m.false_remediation, m.escalation_precision, m.mean_cost_usd
    )


# -- the report -----------------------------------------------------------------------------------------------------
HONESTY = (
    "**Read this first.** Both columns are a SCRIPTED model on generated scenarios. The scripted model never "
    "chooses to use the tool; the 'on' column adds one scripted `similar_history` call per investigation so that "
    "the plumbing (index, no-leakage filter, tool-call budget, citation, redaction) is exercised. Any difference "
    "between the columns is a pipeline effect (an extra tool call), NOT evidence that retrieval helps or hurts a "
    "real model. The scripted model's token usage does not depend on the tools offered, so equal cost is expected "
    "and says nothing about real cost. Recorded real-model on/off fixtures do not exist yet (the human-run `record` "
    "target), so no improvement is claimed and none can be."
)


def _diff(off: Rate, on: Rate) -> str:
    d = on.numerator - off.numerator
    return f"{d:+d} cases" if on.denominator == off.denominator else "n/a"


def render(off: Arm, on: Arm, plumbing: Plumbing, verdict: Verdict) -> str:
    so, sn = arm_stats(off), arm_stats(on)
    n = len(off.cases)
    rows = [
        ("Remediation recall (label-remediate cases fixed correctly)", so.recall, sn.recall),
        ("Remediation success (verified / attempted)", so.success, sn.success),
        ("False remediation rate (all cases; lower is better)", so.false_remediation, sn.false_remediation),
        ("Escalation precision", so.escalation_precision, sn.escalation_precision),
    ]
    lines = [
        "# similar_history on vs. off (synthetic, SCRIPTED model, offline)",
        "",
        HONESTY,
        "",
        f"Cases: {n} labelled synthetic scenarios per arm (dev and held-out together), processed in start-time order. "
        f"A trailing `*` marks a denominator below {SMALL_N}.",
        "",
        "| metric | off | on | difference |",
        "| --- | --- | --- | --- |",
        *[f"| {name} | {fmt(a)} | {fmt(b)} | {_diff(a, b)} |" for name, a, b in rows],
        f"| Mean cost per case (price table, nothing was spent) | ${so.mean_cost_usd:.4f} | ${sn.mean_cost_usd:.4f} "
        f"| {sn.mean_cost_usd - so.mean_cost_usd:+.4f} USD |",
        "",
        "## Plumbing check (on arm)",
        "",
        f"- Investigations that called the tool: {plumbing.cases_calling_tool}/{plumbing.cases} "
        f"({plumbing.calls_ok} calls ok, {plumbing.calls_failed} failed).",
        f"- Calls that returned at least one earlier incident: {plumbing.calls_with_matches}/{plumbing.calls_ok} "
        f"({plumbing.matches_returned} matches in total; the index ends with {plumbing.index_size} incidents).",
        f"- Leakage self-check on every returned match (finished strictly before the case started, not the case "
        f"itself): {plumbing.leak_violations} violations.",
        "- Index time: in this synthetic eval an incident enters the index as of the failed execution's own "
        "`finished_at`, not the time the agent finished its report, remediation and verification (no latency is "
        "invented). The live path (`record_incident`) uses the completion time the caller states, which is later and "
        "so more conservative.",
        "- The index holds only agent-visible report fields from earlier scripted runs of these same scenarios, so a "
        "match is a scripted incident, not a real one.",
        "",
        "## Shipping rule (PRD MVP 2)",
        "",
        "Enabled by default only if retrieval improves remediation success (recall, or verified / attempted) or cost "
        "per case without raising the false remediation rate; otherwise it stays off. A difference under "
        f"{MIN_CASES} cases is noise at this sample size.",
        "",
        f"**Verdict: {verdict.reason}**",
    ]
    return "\n".join(lines) + "\n"


def to_json(off: Arm, on: Arm, plumbing: Plumbing, verdict: Verdict) -> dict[str, object]:
    def arm(a: Arm) -> dict[str, object]:
        s = arm_stats(a)
        return {
            "recall": [s.recall.numerator, s.recall.denominator],
            "success": [s.success.numerator, s.success.denominator],
            "false_remediation": [s.false_remediation.numerator, s.false_remediation.denominator],
            "escalation_precision": [s.escalation_precision.numerator, s.escalation_precision.denominator],
            "mean_cost_usd": s.mean_cost_usd,
        }

    return {
        "model": "scripted",
        "recorded": False,
        "off": arm(off),
        "on": arm(on),
        "plumbing": plumbing.__dict__,
        "verdict": {"enabled_by_default": verdict.enabled_by_default, "reason": verdict.reason},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--out", type=Path, default=Path("data") / "eval")
    args = ap.parse_args(argv)
    off, on, plumbing = run_arms(args.data)
    verdict = shipping_verdict(arm_stats(off), arm_stats(on), recorded=False)  # no recorded real-model fixtures exist
    text = render(off, on, plumbing, verdict)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "retrieval.md").write_text(text, encoding="utf-8")
    (args.out / "retrieval.json").write_text(
        json.dumps(to_json(off, on, plumbing, verdict), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(text)
    print(f"wrote {args.out / 'retrieval.md'} and {args.out / 'retrieval.json'}")
    return 1 if plumbing.leak_violations or plumbing.calls_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
