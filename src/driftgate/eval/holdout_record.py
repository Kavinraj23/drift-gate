"""Cost estimate and spend-capped recording for the real-log holdout. Human-run via `python tasks.py record-holdout`.

This module holds the logic only. It reads no environment, loads no credentials and builds no gateway: the entry point
in `eval/live.py` (the one module allowed to) loads the config, builds the record-mode gateway and passes both in.
Everything here is therefore testable with a stub SDK client, a fake clock and no key.

Safeguards (same shape as `record`):
- the estimate prints expected and worst-case cost per dev case and ONE aggregate for the sealed test split, with no
  network and no key;
- nothing is spent without `--yes-spend`;
- a cumulative `--cap-usd`, checked before each case against its with-margin estimate and before every model call
  against the real cost so far; the residual backstop is the gateway's own $2.50 lifetime and $1.00 daily caps;
- test cases run FIRST, then dev; the run stops at the first cap or gateway failure and later cases are not run;
- a case that hit a gateway failure is excluded (no run record): a spend cap is not an agent decision to escalate;
- fixtures go to `fixtures/replay-holdout/`, run records to `holdout/runs.json`, cost and tool counts to
  `holdout/runs_meta.json`. Test cases print only aggregates.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from driftgate.agents.fingerprint import prompt_fingerprint
from driftgate.eval.holdout import Holdout, HoldoutCase, RunRecord
from driftgate.eval.holdout_run import (
    CaseRun,
    Choice,
    RunContext,
    Session,
    inputs_for,
    iterate,
    plumbing_model,
    print_tallies,
    process_case,
    run_case,
    skip_reason,
)
from driftgate.llm.budget import InvestigationBudget
from driftgate.llm.config import GatewayConfig, InvestigationLimits, estimate_cost
from driftgate.llm.errors import BudgetExceeded, GatewayError, MissingApiKey, RetriesExhausted, UnknownModel
from driftgate.llm.gateway import estimate_input_tokens
from driftgate.llm.replay import save_manifest_entry
from driftgate.llm.types import ModelClient, ModelRequest, ModelResponse, Usage

ASSUMED_OUTPUT_TOKENS = 300  # per model call; the scripted double's own numbers are not real token counts
DEFAULT_MARGIN = 1.5  # real model may take more turns; ~30% tokenizer difference; chars/4 error
DEFAULT_CAP_USD = 1.25
MIN_RESERVE_USD = 0.25
FATAL = (BudgetExceeded, RetriesExhausted, MissingApiKey, UnknownModel)
RECORD = "record"


def scrub(text: str, secret: str | None) -> str:
    return text.replace(secret, "[redacted]") if secret else text


# -- estimate ----------------------------------------------------------------------------------------------------
@dataclass
class _Tally:
    config: GatewayConfig
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class _Counting:
    """Wraps the plumbing double: sizes each request's real text (chars/4) and prices it with the real price table."""

    def __init__(self, inner: ModelClient, tally: _Tally) -> None:
        self._inner, self._tally = inner, tally

    def complete(self, request: ModelRequest) -> ModelResponse:
        t = self._tally
        tokens = estimate_input_tokens(request)
        t.calls += 1
        t.input_tokens += tokens
        t.output_tokens += ASSUMED_OUTPUT_TOKENS
        t.cost_usd += estimate_cost(
            t.config.prices,
            t.config.investigator_model,
            Usage(input_tokens=tokens, output_tokens=ASSUMED_OUTPUT_TOKENS),
        )
        return self._inner.complete(request)


@dataclass(frozen=True)
class CaseEstimate:
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    margin: float

    @property
    def with_margin(self) -> float:
        return self.cost_usd * self.margin


@dataclass(frozen=True)
class EstimateSummary:
    dev: dict[str, CaseEstimate]  # per dev case (inspection allowed)
    test_total: CaseEstimate  # one aggregate for the sealed split
    test_cases: int
    ceiling_per_case: float
    pending_dev: int
    pending_test: int


def ceiling_cost(config: GatewayConfig) -> float:
    """Worst case for one investigation: the whole 40k-token budget priced at the dearer (output) rate."""
    limit = InvestigationLimits().max_total_tokens
    return estimate_cost(config.prices, config.investigator_model, Usage(input_tokens=0, output_tokens=limit))


def estimate_case(ctx: RunContext, case: HoldoutCase, log: str, config: GatewayConfig, margin: float) -> CaseEstimate:
    """Size one case by running the plumbing double through the real tools and prompts (no network)."""
    tally = _Tally(config)
    run_case(
        inputs_for(case, log, ctx.root),
        lambda eid, _b: _Counting(plumbing_model(eid), tally),
        tools=ctx.tools,
        kill_switch_path=ctx.kill_switch_path,
        policy_path=ctx.policy_path,
        assume_other_jobs=ctx.assume_other_jobs,
    )
    return CaseEstimate(tally.calls, tally.input_tokens, tally.output_tokens, tally.cost_usd, margin)


class Estimates:
    """Per-case estimates held privately (the run loop needs them for its cap checks); `summary()` is what prints."""

    def __init__(self) -> None:
        self._by_id: dict[str, CaseEstimate] = {}

    def of(self, case_id: str) -> CaseEstimate | None:
        return self._by_id.get(case_id)

    def compute(self, holdout: Holdout, ctx: RunContext, config: GatewayConfig, margin: float) -> EstimateSummary:
        def score(case: HoldoutCase, log: str, prior: RunRecord | None) -> tuple[str, CaseEstimate | None]:
            if skip_reason(ctx, case, prior):
                return case.id, None
            est = estimate_case(ctx, case, log, config, margin)
            self._by_id[case.id] = est
            return case.id, est

        dev = {c.id: e for c, (_i, e) in holdout.dev_details(score) if e is not None}

        def total(results: list[tuple[str, CaseEstimate | None]]) -> tuple[CaseEstimate, int, int]:
            ests = [e for _i, e in results if e is not None]
            agg = CaseEstimate(
                sum(e.calls for e in ests),
                sum(e.input_tokens for e in ests),
                sum(e.output_tokens for e in ests),
                sum(e.cost_usd for e in ests),
                margin,
            )
            return agg, len(ests), len(results)

        test_total, pending_test, _n = holdout.test_aggregate(score, total)
        return EstimateSummary(dev, test_total, pending_test, ceiling_cost(config), len(dev), pending_test)


def format_estimate(
    s: EstimateSummary, config: GatewayConfig, *, lifetime_remaining: float, daily_remaining: float, lifetime_cap: float
) -> str:
    dev_total = sum(e.with_margin for e in s.dev.values())
    lines = [
        "record-holdout: ESTIMATE ONLY (no network, no API key used)",
        f"model: investigator={config.investigator_model}; tools are the standard set (optional tools only if --tools)",
        "method: the label-blind plumbing double is run through the real tools and prompts on each stored log; each "
        f"model request's text is sized (chars/4), {ASSUMED_OUTPUT_TOKENS} output tokens per call are assumed, "
        "no cache discount, and 'w/ margin' multiplies by the margin. A real model may take other paths; "
        "order of magnitude only.",
        f"worst case per case: ${s.ceiling_per_case:.4f} (the whole {InvestigationLimits().max_total_tokens:,}-token "
        "budget at the output rate); the run cap and the gateway caps bind first.",
        "",
        f"dev cases to run ({s.pending_dev}):",
    ]
    for cid, e in sorted(s.dev.items()):
        lines.append(
            f"  {cid}: {e.calls} calls, {e.input_tokens:,} in / {e.output_tokens:,} out, "
            f"est ${e.cost_usd:.4f}, w/ margin ${e.with_margin:.4f}"
        )
    t = s.test_total
    lines += [
        f"test cases to run ({s.pending_test}, sealed, aggregate only): {t.calls} calls, est ${t.cost_usd:.4f}, "
        f"w/ margin ${t.with_margin:.4f}",
        "",
        f"all pending cases: est ${t.cost_usd + sum(e.cost_usd for e in s.dev.values()):.4f}, "
        f"w/ margin ${t.with_margin + dev_total:.4f}; "
        f"worst case ${(s.pending_dev + s.pending_test) * s.ceiling_per_case:.2f}",
        f"ledger: lifetime remaining ${lifetime_remaining:.4f} of ${lifetime_cap:.2f}; today remaining "
        f"${daily_remaining:.4f} of ${config.daily_cap_usd:.2f}",
    ]
    return "\n".join(lines)


# -- spending ----------------------------------------------------------------------------------------------------
def room(
    est: CaseEstimate | None, cumulative: float, cap: float, life_left: float, day_left: float, reserve: float
) -> str:
    """'' if the next case fits, else why not. Uses the with-margin estimate (fails closed)."""
    need = est.with_margin if est is not None else 0.0
    if cumulative >= cap:
        return f"cumulative spend ${cumulative:.4f} reached the cap ${cap:.2f}"
    if cumulative + need > cap:
        return f"estimate ${need:.4f} would pass the cap ${cap:.2f} (spent ${cumulative:.4f})"
    if need > day_left - cumulative:
        return f"estimate ${need:.4f} exceeds today's remaining ${max(day_left - cumulative, 0):.4f} of the daily cap"
    if need > life_left - cumulative - reserve:
        return f"estimate ${need:.4f} would eat into the ${reserve:.2f} reserve"
    return ""


class _Spy:
    """Notes gateway failures that make a run unusable, and refuses a call once the run's own cap is reached."""

    def __init__(self, inner: ModelClient, errors: list[GatewayError], guard: Callable[[], None]) -> None:
        self._inner, self._errors, self._guard = inner, errors, guard

    def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            self._guard()
            return self._inner.complete(request)
        except FATAL as exc:
            self._errors.append(exc)
            raise


@dataclass
class SpendState:
    cumulative_usd: float = 0.0


def run_record(
    holdout: Holdout,
    ctx: RunContext,
    *,
    gateway: object,  # a Gateway in record mode; typed loosely so tests can pass a stub with .bind() and .calls
    config: GatewayConfig,
    estimates: Estimates,
    cap_usd: float,
    life_left: float,
    day_left: float,
    reserve: float,
    fixtures_dir: Path,
    out: TextIO,
) -> tuple[Session, SpendState]:
    """Record every pending case, test split first. Returns the session (records to write) and the spend."""
    session, spend = Session(), SpendState()
    fingerprint = prompt_fingerprint()
    calls = gateway.calls  # type: ignore[attr-defined]

    def process(case: HoldoutCase, log: str, prior: RunRecord | None) -> tuple[str, CaseRun | None, str]:
        if session.stopped or skip_reason(ctx, case, prior):
            return process_case(ctx, session, lambda _c: [], case, log, prior)
        why = room(estimates.of(case.id), spend.cumulative_usd, cap_usd, life_left, day_left, reserve)
        if why:
            session.stopped = f"stopped before the next case: {why}"
            return "skipped", None, session.stopped
        before = len(calls)
        errors: list[GatewayError] = []

        def guard() -> None:
            spent = spend.cumulative_usd + sum(c.cost_usd for c in calls[before:])
            if spent >= cap_usd:
                raise BudgetExceeded("run cap", cap_usd, spent, 0.0)

        def factory(_eid: str, budget: InvestigationBudget) -> ModelClient:
            return _Spy(gateway.bind(budget, role="investigator"), errors, guard)  # type: ignore[attr-defined]

        choice = Choice(factory, RECORD, config.investigator_model, case.id)
        try:
            status, run, detail = process_case(ctx, session, lambda _c: [choice], case, log, prior)
        except Exception as exc:  # noqa: BLE001 - SDK or transport failure: report it scrubbed and stop
            spend.cumulative_usd += sum(c.cost_usd for c in calls[before:])
            session.stopped = f"stopped: {type(exc).__name__}: {scrub(str(exc), config.api_key)}"
            return "unusable", None, "error"
        cost = sum(c.cost_usd for c in calls[before:])
        spend.cumulative_usd += cost
        if errors:  # a gateway failure inside the run: its record is not an agent decision
            session.records[:] = [r for r in session.records if r["case_id"] != case.id]
            session.meta.pop(case.id, None)
            session.stopped = f"stopped: gateway failure ({type(errors[0]).__name__}); that case has no run record"
            return "unusable", None, "gateway failure"
        if status == "ran":
            save_manifest_entry(
                fixtures_dir,
                case.id,
                {
                    "fingerprint": fingerprint,
                    "models": {"investigator": config.investigator_model},
                    "tools": ctx.tools,
                    "model_calls": run.model_calls if run else 0,
                    "cost_usd": round(cost, 6),
                },
            )
        return status, run, detail

    tallies = iterate(holdout, process, order=("test", "dev"), out=out)
    print_tallies(tallies, ("test", "dev"), out)
    return session, spend
