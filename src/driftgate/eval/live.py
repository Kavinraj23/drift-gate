"""HUMAN-RUN live entry points: `python tasks.py record` and `python tasks.py eval-live`.

This is the only module that builds a Gateway in live or record mode, and the only one (besides `llm/config.py`
itself) that calls `load_config()`; it also reads the two model names via `dotenv_values` for `--estimate`. The other
env reader is `adapters/github_client.py`. An architecture test enforces all of this, and that nothing but
`tasks.py` references this module. Offline targets (`e2e`, `eval`, `mvp-check`),
the tests and every default never reach it. Nothing is read from the environment or `.env` at import time.

    python tasks.py record --estimate            # no network, no key: token and cost estimate, ledger status
    python tasks.py record --yes-spend           # ONE cheap Tier 0 scenario, real API
    python tasks.py record --all --only-stale --yes-spend
    python tasks.py eval-live --estimate
    python tasks.py eval-live --yes-spend

Safeguards: nothing calls the API without `--yes-spend`; `--estimate` builds no gateway and never touches the key
(only the model names are taken from `.env`); `record` stops at a cumulative `--cap-usd`, checked before each
scenario (estimate) and before every model call inside a scenario (real cost so far), so one scenario cannot silently
overshoot it by more than one call; the residual backstop is the gateway, which fails fast past the $2.50 lifetime
and $1.00 daily caps; `eval-live` keeps a reserve; errors are scrubbed of the key before printing.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TextIO

from driftgate.agents import reviewer as reviewer_agent
from driftgate.agents.fingerprint import prompt_fingerprint
from driftgate.eval import e2e
from driftgate.eval.e2e import ScenarioResult
from driftgate.eval.ground_truth import FailureLabel, GroundTruth, load_ground_truth
from driftgate.eval.report import LiveInfo, build_report_from_results, render_markdown, write_report
from driftgate.llm.clock import Clock, SystemClock
from driftgate.llm.config import (
    PRICES_AS_OF,
    PRICING_URL,
    GatewayConfig,
    config_from_env,
    estimate_cost,
    load_config,
)
from driftgate.llm.errors import BudgetExceeded, GatewayError, MissingApiKey, RetriesExhausted, UnknownModel
from driftgate.llm.gateway import Gateway, estimate_input_tokens
from driftgate.llm.ledger import SpendLedger, utc_day
from driftgate.llm.replay import FRESH, FixtureStore, save_manifest_entry, scenario_status
from driftgate.llm.types import ModelClient, ModelRequest, ModelResponse, Usage

ASSUMED_OUTPUT_TOKENS = 300  # per model call; the scripted model's own numbers are not real token counts
DEFAULT_MARGIN = 1.5  # covers: the real model taking more turns, the ~30% tokenizer difference, chars/4 error
DEFAULT_RECORD_CAP_USD = 1.25
MIN_RESERVE_USD = 0.25
MODEL_VARS = ("DRIFTGATE_INVESTIGATOR_MODEL", "DRIFTGATE_REVIEWER_MODEL")
FATAL = (BudgetExceeded, RetriesExhausted, MissingApiKey, UnknownModel)

GatewayFactory = Callable[[GatewayConfig, str, "FixtureStore | None"], Gateway]  # (config, mode, fixtures)


# -- estimate ----------------------------------------------------------------------------------------------------
@dataclass
class _Tally:
    config: GatewayConfig
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class _Counting:
    """Wraps a scripted client: reads each request's size, prices it per call with the real price table."""

    def __init__(self, inner: ModelClient, tally: _Tally) -> None:
        self._inner, self._tally = inner, tally

    def complete(self, request: ModelRequest) -> ModelResponse:
        t = self._tally
        model = (
            t.config.reviewer_model if request.system == reviewer_agent.SYSTEM_PROMPT else t.config.investigator_model
        )
        tokens = estimate_input_tokens(request)
        t.calls += 1
        t.input_tokens += tokens
        t.output_tokens += ASSUMED_OUTPUT_TOKENS
        t.cost_usd += estimate_cost(
            t.config.prices, model, Usage(input_tokens=tokens, output_tokens=ASSUMED_OUTPUT_TOKENS)
        )
        return self._inner.complete(request)


@dataclass(frozen=True)
class ScenarioEstimate:
    scenario_id: str
    held_out: bool
    fault_id: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    margin: float

    @property
    def cost_with_margin(self) -> float:
        return self.cost_usd * self.margin


def estimate_scenario(
    data_dir: Path, label: FailureLabel, held_out: bool, config: GatewayConfig, margin: float
) -> ScenarioEstimate:
    tally = _Tally(config)
    e2e.run_scenario(data_dir, label, held_out, mode="fake", wrap=lambda c: _Counting(c, tally))
    return ScenarioEstimate(
        label.scenario_id or "",
        held_out,
        label.fault_id,
        tally.calls,
        tally.input_tokens,
        tally.output_tokens,
        tally.cost_usd,
        margin,
    )


def estimate_all(data_dir: Path, truth: GroundTruth, config: GatewayConfig, margin: float) -> list[ScenarioEstimate]:
    """Every labelled scenario, dev first then held-out (the order `record --all` uses)."""
    return [
        estimate_scenario(data_dir, label, held_out, config, margin)
        for held_out in (False, True)
        for label in truth.scenarios(held_out=held_out)
    ]


def default_scenario(truth: GroundTruth, estimates: list[ScenarioEstimate]) -> str:
    """The cheapest dev scenario that is a Tier 0 remediation and uses the model."""
    by_id = {e.scenario_id: e for e in estimates}
    cands = [
        lab.scenario_id
        for lab in truth.scenarios(held_out=False)
        if lab.disposition == "remediate" and lab.correct_tier == 0 and by_id[lab.scenario_id or ""].calls > 0
    ]
    if not cands:
        raise SystemExit("no dev Tier 0 scenario found to use as the default; pass --scenario")
    return min(cands, key=lambda sid: (by_id[sid or ""].cost_usd, sid or "")) or ""


@dataclass
class LedgerStatus:
    lifetime_spent: float
    daily_spent: float
    lifetime_cap: float
    daily_cap: float

    @property
    def lifetime_remaining(self) -> float:
        return max(0.0, self.lifetime_cap - self.lifetime_spent)

    @property
    def daily_remaining(self) -> float:
        return max(0.0, self.daily_cap - self.daily_spent)


def ledger_status(config: GatewayConfig, clock: Clock) -> LedgerStatus:
    ledger = SpendLedger(config.ledger_path)
    return LedgerStatus(
        ledger.lifetime_spend(),
        ledger.daily_spend(utc_day(clock.time())),
        config.lifetime_cap_usd,
        config.daily_cap_usd,
    )


def format_estimate(
    estimates: list[ScenarioEstimate], selected: list[str], config: GatewayConfig, status: LedgerStatus, title: str
) -> str:
    inv, rev = config.investigator_model, config.reviewer_model
    margin = estimates[0].margin if estimates else DEFAULT_MARGIN
    lines = [
        f"{title}: ESTIMATE ONLY (no network, no API key used)",
        f"models: investigator={inv}, reviewer={rev}",
        f"prices: llm/config.py table, verified {PRICES_AS_OF} against {PRICING_URL}",
        "method: the scripted pipeline is run; each model request's real text is sized (chars/4), "
        f"{ASSUMED_OUTPUT_TOKENS} output tokens per call are assumed, no cache discount is taken, and "
        f"'with margin' multiplies by {margin:g}. A real model may take other paths; treat as order of magnitude.",
        "",
        f"{'scenario':<9}{'set':<9}{'fault':<34}{'calls':>6}{'in tok':>9}{'out tok':>9}"
        f"{'est USD':>10}{'w/ margin':>11}  sel",
    ]
    for e in estimates:
        lines.append(
            f"{e.scenario_id:<9}{'held-out' if e.held_out else 'dev':<9}{e.fault_id[:33]:<34}{e.calls:>6}"
            f"{e.input_tokens:>9,}{e.output_tokens:>9,}{e.cost_usd:>10.4f}{e.cost_with_margin:>11.4f}  "
            + ("*" if e.scenario_id in selected else "")
        )
    sel = [e for e in estimates if e.scenario_id in selected]
    used = [e for e in estimates if e.calls > 0]
    lines += [
        "",
        f"selected ({len(sel)}): est ${sum(e.cost_usd for e in sel):.4f}, with margin "
        f"${sum(e.cost_with_margin for e in sel):.4f}",
        f"all scenarios that use the model ({len(used)}): est ${sum(e.cost_usd for e in used):.4f}, with margin "
        f"${sum(e.cost_with_margin for e in used):.4f}",
        f"ledger: lifetime spent ${status.lifetime_spent:.4f} of ${status.lifetime_cap:.2f} "
        f"(remaining ${status.lifetime_remaining:.4f}); today ${status.daily_spent:.4f} of "
        f"${status.daily_cap:.2f} (remaining ${status.daily_remaining:.4f})",
    ]
    return "\n".join(lines)


# -- config loading (runtime only) -------------------------------------------------------------------------------
def models_only_config(root: Path) -> GatewayConfig:
    """For `--estimate`: take only the two model names from `.env` and the process env. The API key is never
    loaded, and the process environment is left untouched."""
    values: dict[str, str] = {}
    env_file = root / ".env"
    if env_file.is_file():
        from dotenv import dotenv_values

        values.update({k: v for k, v in dotenv_values(env_file).items() if k in MODEL_VARS and v})
    values.update({k: os.environ[k] for k in MODEL_VARS if os.environ.get(k)})
    return _with_ledger(config_from_env(values), root)


def full_config(root: Path) -> GatewayConfig:
    """For a spending run: the explicit loader (reads `.env` and the process env, including the key)."""
    return _with_ledger(load_config(root / ".env"), root)


def _with_ledger(config: GatewayConfig, root: Path) -> GatewayConfig:
    return replace(config, ledger_path=root / "data" / "spend.sqlite")


def default_gateway_factory(config: GatewayConfig, mode: str, fixtures: FixtureStore | None) -> Gateway:
    assert mode in ("live", "record")
    return Gateway(config, mode=mode, fixtures=fixtures)  # type: ignore[arg-type]


def scrub(text: str, secret: str | None) -> str:
    return text.replace(secret, "[redacted]") if secret else text


# -- running -----------------------------------------------------------------------------------------------------
class _ErrorSpy:
    """Notes gateway failures that mean a run is unusable (spend cap, retries exhausted); per-investigation token
    exhaustion is normal and is not one of them."""

    def __init__(self, inner: ModelClient, errors: list[GatewayError], guard: Callable[[], None] | None = None) -> None:
        self._inner, self._errors, self._guard = inner, errors, guard

    def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            if self._guard:
                self._guard()  # mid-scenario cumulative spend check, before the next gateway call
            return self._inner.complete(request)
        except FATAL as exc:
            self._errors.append(exc)
            raise


@dataclass
class RunState:
    cumulative_usd: float = 0.0
    ran: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    results: list[ScenarioResult] = field(default_factory=list)
    stopped: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


def _model_label(config: GatewayConfig) -> str:
    inv, rev = config.investigator_model, config.reviewer_model
    return inv if inv == rev else f"{inv} + {rev}"


def _run_one(
    gateway: Gateway,
    data_dir: Path,
    label: FailureLabel,
    held_out: bool,
    config: GatewayConfig,
    state: RunState,
    limit_usd: float | None = None,
) -> tuple[ScenarioResult | None, float, int]:
    """Run one scenario through the gateway. Returns (result or None if unusable, real cost, model calls).

    `limit_usd` is the run-wide spend limit: before every model call, spend so far (earlier scenarios plus this
    scenario's recorded calls) at or past it raises BudgetExceeded, which the spy records as a gateway failure."""
    before = len(gateway.calls)
    errors: list[GatewayError] = []

    def guard() -> None:
        if limit_usd is None:
            return
        spent = state.cumulative_usd + sum(c.cost_usd for c in gateway.calls[before:])
        if spent >= limit_usd:
            raise BudgetExceeded("run cap", limit_usd, spent, 0.0)

    result = e2e.run_with_gateway(
        data_dir, label, held_out, gateway, _model_label(config), wrap_client=lambda c: _ErrorSpy(c, errors, guard)
    )
    new = gateway.calls[before:]
    cost = sum(c.cost_usd for c in new)
    state.input_tokens += sum(c.input_tokens + c.cache_read_tokens + c.cache_creation_tokens for c in new)
    state.output_tokens += sum(c.output_tokens for c in new)
    return (None if errors else result), cost, len(new)


def _room(est: ScenarioEstimate, state: RunState, status: LedgerStatus, cap: float, reserve: float) -> str:
    """'' if the scenario fits, else why not. Uses the with-margin estimate (fail closed)."""
    need = est.cost_with_margin
    if state.cumulative_usd >= cap:
        return f"cumulative spend ${state.cumulative_usd:.4f} reached the cap ${cap:.2f}"
    if state.cumulative_usd + need > cap:
        return f"estimate ${need:.4f} would pass the cap ${cap:.2f} (spent ${state.cumulative_usd:.4f})"
    life = status.lifetime_remaining - state.cumulative_usd
    day = status.daily_remaining - state.cumulative_usd
    if need > day:
        return f"estimate ${need:.4f} exceeds today's remaining ${max(day, 0):.4f} of the daily cap"
    if need > life - reserve:
        return (
            f"estimate ${need:.4f} would eat into the ${reserve:.2f} reserve (lifetime remaining ${max(life, 0):.4f})"
        )
    return ""


def run_record(
    args: argparse.Namespace,
    *,
    root: Path,
    out: TextIO,
    config: GatewayConfig,
    truth: GroundTruth,
    estimates: list[ScenarioEstimate],
    gateway_factory: GatewayFactory,
    clock: Clock,
) -> int:
    data_dir, fixtures_dir = args.data, args.fixtures
    fingerprint = prompt_fingerprint()
    by_id = {e.scenario_id: e for e in estimates}
    labels = {(label.scenario_id or ""): (label, ho) for ho in (False, True) for label in truth.scenarios(held_out=ho)}
    if args.all:
        order = [e.scenario_id for e in estimates if e.calls > 0]
    else:
        order = [args.scenario or default_scenario(truth, estimates)]
    unknown = [sid for sid in order if sid not in labels]
    if unknown:
        print(f"unknown scenario(s): {', '.join(unknown)}", file=out)
        return 2
    if not args.all and by_id[order[0]].calls == 0:
        print(f"{order[0]} is closed by the pre-filter and uses no model; nothing to record", file=out)
        return 2
    if args.only_stale:
        order = [sid for sid in order if scenario_status(fixtures_dir, sid, fingerprint) != FRESH]
    if not order:
        print("nothing to record: every selected scenario is fresh (use --force to re-record)", file=out)
        return 0
    status = ledger_status(config, clock)
    print(f"recording {len(order)} scenario(s) with {_model_label(config)}: {', '.join(order)}", file=out)
    print(f"prompt fingerprint {fingerprint}; fixtures -> {fixtures_dir}; cap ${args.cap_usd:.2f}", file=out)
    gateway = gateway_factory(config, "record", FixtureStore(fixtures_dir, fingerprint=fingerprint))
    state = RunState()
    for sid in order:
        label, held_out = labels[sid]
        why = _room(by_id[sid], state, status, args.cap_usd, 0.0)
        if why:
            state.stopped = f"stopped before {sid}: {why}"
            break
        try:
            result, cost, calls = _run_one(gateway, data_dir, label, held_out, config, state, args.cap_usd)
        except Exception as exc:  # SDK or transport failure: report it (scrubbed) and stop
            state.stopped = f"error in {sid}: {type(exc).__name__}: {scrub(str(exc), config.api_key)}"
            break
        state.cumulative_usd += cost
        if result is None:
            state.stopped = (
                f"{sid} hit a gateway failure (spend cap or retries); its fixtures are partial, not recorded"
            )
            print(f"{sid}: UNUSABLE, real cost ${cost:.4f}, cumulative ${state.cumulative_usd:.4f}", file=out)
            break
        state.ran.append(sid)
        state.results.append(result)
        save_manifest_entry(
            fixtures_dir,
            sid,
            {
                "fingerprint": fingerprint,
                "models": {"investigator": config.investigator_model, "reviewer": config.reviewer_model},
                "model_calls": calls,
                "cost_usd": round(cost, 6),
                "status": result.row.status,
            },
        )
        print(
            f"{sid}: {result.row.status} ({result.row.actual}); {calls} model calls, "
            f"real cost ${cost:.4f}, cumulative ${state.cumulative_usd:.4f} of cap ${args.cap_usd:.2f}",
            file=out,
        )
    after = ledger_status(config, clock)
    print(
        f"ledger: lifetime ${after.lifetime_spent:.4f} of ${after.lifetime_cap:.2f}; "
        f"today ${after.daily_spent:.4f} of ${after.daily_cap:.2f}",
        file=out,
    )
    if state.stopped:
        print(state.stopped, file=out)
        return 3
    print("done. Replay them with `python tasks.py e2e` (no key needed).", file=out)
    return 0


def run_eval_live(
    args: argparse.Namespace,
    *,
    root: Path,
    out: TextIO,
    config: GatewayConfig,
    truth: GroundTruth,
    estimates: list[ScenarioEstimate],
    gateway_factory: GatewayFactory,
    clock: Clock,
) -> int:
    status = ledger_status(config, clock)
    cap = args.cap_usd if args.cap_usd is not None else status.lifetime_remaining
    gateway = gateway_factory(config, "live", None)
    by_id = {e.scenario_id: e for e in estimates}
    held = [(label, True) for label in truth.scenarios(held_out=True)]
    dev = [(label, False) for label in truth.scenarios(held_out=False)]
    state = RunState()
    stopped = False
    print(f"eval-live with {_model_label(config)}; reserve ${args.reserve_usd:.2f}; held-out first, then dev", file=out)
    for label, held_out in [*held, *dev]:
        sid = label.scenario_id or ""
        est = by_id[sid]
        if est.calls > 0:
            why = "an earlier gateway failure stopped the run" if stopped else ""
            why = why or _room(est, state, status, cap, args.reserve_usd)
            if why:
                state.skipped.append(sid)
                print(f"{sid}: not run ({why})", file=out)
                continue
        try:
            limit = min(cap, status.lifetime_remaining - args.reserve_usd, status.daily_remaining)
            result, cost, calls = _run_one(gateway, args.data, label, held_out, config, state, limit)
        except Exception as exc:
            print(f"{sid}: error {type(exc).__name__}: {scrub(str(exc), config.api_key)}", file=out)
            state.skipped.append(sid)
            stopped = True
            continue
        state.cumulative_usd += cost
        if result is None:
            state.skipped.append(sid)
            stopped = True
            print(f"{sid}: UNUSABLE (gateway failure), real cost ${cost:.4f}; excluded from the report", file=out)
            continue
        state.ran.append(sid)
        state.results.append(result)
        print(
            f"{sid}: {result.row.status} ({result.row.actual}); {calls} model calls, real cost ${cost:.4f}, "
            f"cumulative ${state.cumulative_usd:.4f}",
            file=out,
        )
    info = LiveInfo(
        models={"investigator": config.investigator_model, "reviewer": config.reviewer_model},
        spent_usd=state.cumulative_usd,
        reserve_usd=args.reserve_usd,
        ran=state.ran,
        not_run=state.skipped,
        total_input_tokens=state.input_tokens,
        total_output_tokens=state.output_tokens,
    )
    data = build_report_from_results(args.data, state.results, mode="live", with_drills=False, live=info)
    md, js = write_report(data, args.out)
    print(render_markdown(data), file=out)
    print(f"wrote {md} and {js} (the offline report.md is untouched)", file=out)
    return 3 if state.skipped else 0


# -- command line ------------------------------------------------------------------------------------------------
def _parser(prog: str, root: Path) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=f"python tasks.py {prog}", description=__doc__)
    ap.add_argument("--estimate", action="store_true", help="print the cost estimate and exit (no network, no key)")
    ap.add_argument("--yes-spend", action="store_true", help="actually call the API and spend money")
    ap.add_argument("--data", type=Path, default=root / "data")
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN, help="estimate multiplier (default %(default)s)")
    if prog == "record":
        ap.add_argument("--scenario", help="scenario id (default: the cheapest dev Tier 0 case)")
        ap.add_argument("--all", action="store_true", help="every scenario that uses the model")
        ap.add_argument("--only-stale", action="store_true", help="skip scenarios recorded under the current prompts")
        ap.add_argument("--force", action="store_true", help="re-record even if fresh (default behaviour; explicit)")
        ap.add_argument("--cap-usd", type=float, default=DEFAULT_RECORD_CAP_USD, help="cumulative spend cap")
        ap.add_argument("--fixtures", type=Path, default=root / "fixtures" / "replay")
    else:
        ap.add_argument("--cap-usd", type=float, default=None, help="spend cap (default: ledger lifetime remaining)")
        ap.add_argument("--reserve-usd", type=float, default=MIN_RESERVE_USD, help="kept unspent (minimum 0.25)")
        ap.add_argument("--out", type=Path, default=root / "data" / "eval")
    return ap


def main(
    argv: list[str],
    *,
    root: Path | None = None,
    out: TextIO | None = None,
    models_loader: Callable[[Path], GatewayConfig] = models_only_config,
    config_loader: Callable[[Path], GatewayConfig] = full_config,
    gateway_factory: GatewayFactory = default_gateway_factory,
    clock: Clock | None = None,
) -> int:
    """`argv[0]` is `record` or `eval-live`. All collaborators are injectable so tests use a stub SDK client."""
    out = out or sys.stdout
    root = root or Path.cwd()
    clock = clock or SystemClock()
    if not argv or argv[0] not in ("record", "eval-live"):
        print("usage: python -m driftgate.eval.live (record|eval-live) [options]", file=out)
        return 2
    prog = argv[0]
    if (root / ".autonomous").exists():
        print(f"refusing: '{prog}' is human-only and .autonomous exists", file=out)
        return 1
    args = _parser(prog, root).parse_args(argv[1:])
    if prog == "eval-live" and args.reserve_usd < MIN_RESERVE_USD:
        print(f"--reserve-usd cannot be below ${MIN_RESERVE_USD:.2f}", file=out)
        return 2
    if prog == "record" and args.all and args.scenario:
        print("use either --all or --scenario, not both", file=out)
        return 2
    if not (args.data / "executions.json").exists():
        print(f"no dataset at {args.data}; run `python tasks.py data` first", file=out)
        return 2

    try:
        est_config = models_loader(root)
        truth = load_ground_truth(args.data)
        estimates = estimate_all(args.data, truth, est_config, args.margin)
    except UnknownModel as exc:
        print(
            f"cannot estimate: {exc}. Add a price entry in llm/config.py (verified against the pricing page).", file=out
        )
        return 2

    if prog == "record":
        selected = (
            [e.scenario_id for e in estimates if e.calls > 0]
            if args.all
            else [args.scenario or default_scenario(truth, estimates)]
        )
        title = "record"
    else:
        selected = [e.scenario_id for e in estimates if e.calls > 0]
        title = "eval-live"
    if args.estimate:
        status = ledger_status(est_config, clock)
        print(format_estimate(estimates, selected, est_config, status, title), file=out)
        if prog == "eval-live":
            room = status.lifetime_remaining - MIN_RESERVE_USD
            total = sum(e.cost_with_margin for e in estimates if e.calls > 0)
            print(
                f"eval-live keeps a ${MIN_RESERVE_USD:.2f} reserve: budget available "
                f"${max(room, 0):.4f}; full run with margin ${total:.4f} -> "
                + (
                    "fits"
                    if total <= min(room, status.daily_remaining)
                    else "will NOT fit; later scenarios are skipped"
                ),
                file=out,
            )
        return 0

    if not args.yes_spend:
        sel_cost = sum(e.cost_with_margin for e in estimates if e.scenario_id in selected)
        print(
            f"not spending: pass --yes-spend to call the API (selected {len(selected)} scenario(s), estimate with "
            f"margin ${sel_cost:.4f}). Run with --estimate first.",
            file=out,
        )
        return 2

    try:
        config = config_loader(root)
        if gateway_factory is default_gateway_factory and not config.api_key:
            print("no ANTHROPIC_API_KEY found in the environment or .env (nothing was called)", file=out)
            return 2
        runner = run_record if prog == "record" else run_eval_live
        return runner(
            args,
            root=root,
            out=out,
            config=config,
            truth=truth,
            estimates=estimates,
            gateway_factory=gateway_factory,
            clock=clock,
        )
    except UnknownModel as exc:
        print(f"refusing: {exc}", file=out)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
