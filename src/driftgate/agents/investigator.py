"""Investigator agent: a tool-use loop that gathers evidence, forms a hypothesis and proposes a remediation.

Written directly against the `ModelClient` protocol, with no framework. The agent is read-only: it can call
the registered read-only tools and `submit_report`, and nothing else. What it proposes is a *proposal*; the
orchestrator re-derives every fact the proposal depends on and SafetyGate decides.

Code-enforced limits (none of them rely on the prompt):
- every tool execution first calls `budget.consume_tool_call()`; the 9th call is refused and the run abstains
  with `budget_truncated=True` and the partial evidence bundle;
- the token limit is checked before every model call and after every response;
- tool call ids must be unique within a run; a repeated id is rejected, never dispatched;
- evidence integrity (invariant 7): after the model reports, every evidence item whose `source` is not the id
  of a tool call of this run is dropped here, in code, and counted. No surviving evidence means abstain.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from driftgate.domain import CLASSIFICATIONS, LAYERS, TIERS, Evidence, Remediation, Report, RunStats, SuspectedChange
from driftgate.gates import TIER_GATE
from driftgate.llm.budget import CallRecord, InvestigationBudget
from driftgate.llm.errors import FixtureMissing, GatewayError, InvestigationBudgetExhausted
from driftgate.llm.types import ModelClient, ModelRequest, ModelResponse
from driftgate.remediation_catalog import CATALOG
from driftgate.tools import ToolContext, ToolResult, dispatch, tool_definitions

SUBMIT_REPORT = "submit_report"
DEFAULT_RESPONSE_TOKENS = 1500
EXTRA_TURNS = 4  # turns beyond the tool-call limit: duplicate-only turns and the one nudge to report
DEFAULT_LAYER = "L4"


def _catalog_text() -> str:
    return "\n".join(f"  Tier {tier}: {', '.join(actions)}" for tier, actions in CATALOG.items())


SYSTEM_PROMPT = f"""You are DriftGate's investigator, acting like an on-call platform engineer for a failed CI \
pipeline execution. You only read; you never change anything. Everything you propose is re-checked by \
deterministic code that can refuse it, and you cannot override that.

How to work
- Start with the cheapest tools (get_execution, classify_signature, fleet_correlate, flake_history). Use the \
medium-cost tools (get_step_logs, read_repo_file) only when the cheap ones leave the cause open. You have at \
most 8 tool calls; spend them deliberately.
- The first error in a log is often not the operative one. Read all error blocks before you decide.
- Cite evidence by tool call id. Each evidence item must carry the id of a tool call you actually made in this \
investigation. Items without a valid id are deleted before anyone sees them.
- Fleet-wide failures (many executions on a shared connector, template or runner pool) are not yours to fix: \
report them and do not propose a remediation.
- Governance outcomes (approval rejected or expired) and user aborts are normal and never remediated.

Proposing a remediation (optional)
Propose one only when the evidence supports a specific action from this catalog, with its tier:
{_catalog_text()}
- Tier 0 re-runs are only ever allowed by deterministic code when the failure has a deterministic signature \
match AND either a fail-then-pass history (flake_history) or a Tier 0 known-transient rule \
(classify_signature). If those are not both satisfied, do not propose Tier 0.
- Tier 2 is only ever allowed when the lock holder is provably dead; you cannot prove that.
- Tier 3 is a code change delivered as a pull request that a human merges. Name the files to change in `paths` \
(relative paths inside the repository) and put the change itself in `diff`: a unified diff (a/ and b/ paths, \
hunks) against those files as read at the failing commit, minimal and limited to the named files. When it spans \
several files, list additions before removals. Never delete files, and never put credentials in a diff. An \
independent reviewer sees only the diff, the files and your hypothesis.
- Your confidence is recorded. It is never sufficient for any tier. State it honestly.
- If you cannot support an action, propose none and say why in escalation_reason: escalating is a good outcome.

Finishing
Finish by calling {SUBMIT_REPORT} exactly once with your classification, layer (L0-L6), confidence, hypothesis, \
cited evidence, and optional remediation. If you cannot reach a conclusion, call it with abstain=true."""


def submit_report_definition() -> dict[str, Any]:
    """The structured final tool. It has no side effects: it only carries the report out of the loop."""
    evidence_item = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "The id of a tool call you made in this investigation."},
            "finding": {"type": "string"},
            "supports": {"type": "string", "description": "What the finding supports (e.g. the hypothesis)."},
        },
        "required": ["source", "finding", "supports"],
    }
    return {
        "name": SUBMIT_REPORT,
        "description": (
            "Finish the investigation with your report. Call it exactly once. Evidence must cite tool call ids "
            "from this investigation. Propose a remediation only if the evidence supports one; otherwise leave it "
            "out and give an escalation_reason."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "classification": {"enum": list(CLASSIFICATIONS)},
                "layer": {"enum": list(LAYERS)},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "hypothesis": {"type": "string"},
                "evidence": {"type": "array", "items": evidence_item},
                "suspected_change": {
                    "type": "object",
                    "properties": {k: {"type": "string"} for k in ("kind", "ref", "at", "basis")},
                    "required": ["kind", "ref", "at", "basis"],
                },
                "remediation": {
                    "type": "object",
                    "properties": {
                        "tier": {"enum": list(TIERS)},
                        "action": {"type": "string"},
                        "rationale": {"type": "string"},
                        "reversible": {"type": "boolean"},
                        "paths": {"type": "array", "items": {"type": "string"}},
                        "diff": {"type": "string", "description": "Tier 3 only: the unified diff of the change."},
                        "params": {"type": "object"},
                    },
                    "required": ["tier", "action", "rationale", "reversible"],
                },
                "abstain": {"type": "boolean"},
                "escalation_reason": {"type": "string"},
            },
            "required": ["classification", "layer", "confidence", "hypothesis", "evidence"],
        },
    }


def agent_tool_definitions() -> list[dict[str, Any]]:
    """The read-only tools plus the final-report tool, in a stable order (cache friendly)."""
    return [*tool_definitions(), submit_report_definition()]


@dataclass
class Proposal:
    """What the model proposed, before any gate. `paths` and `params` are inputs to the orchestrator's checks."""

    remediation: Remediation
    paths: tuple[str, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)
    diff: str = ""  # Tier 3: the unified diff text; parsed and checked by tier3.check_diff, not trusted here


@dataclass
class Investigation:
    report: Report  # evidence already filtered; remediation (if any) is the pre-gate proposal
    tool_results: list[ToolResult]
    proposal: Proposal | None
    budget: InvestigationBudget
    stop_reason: str  # report | budget_tool_calls | budget_tokens | model_unavailable | no_report | malformed_report
    evidence_submitted: int = 0
    evidence_dropped: int = 0
    duplicate_ids_rejected: int = 0
    model_calls: int = 0

    @property
    def tool_call_ids(self) -> list[str]:
        return [r.source for r in self.tool_results]


def initial_message(execution_id: str, context: str = "") -> str:
    text = (
        f"Investigate failed CI execution {execution_id}. Gather evidence with the read-only tools, then finish "
        f"with {SUBMIT_REPORT}."
    )
    return f"{text}\n\n{context}" if context else text


def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """A model that ends with plain JSON instead of calling the tool: accept a single JSON object."""
    candidates = [text.strip()]
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    for c in candidates:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, dict) and "classification" in obj:
            return obj
    return None


def _summarize(r: ToolResult) -> str:
    d = r.data
    if not r.ok:
        return f"{r.tool} failed: {r.error}"
    if r.tool == "classify_signature":
        return f"signature {d.get('signature')} (layer {d.get('layer')}, deterministic={d.get('deterministic_match')})"
    if r.tool == "fleet_correlate":
        return f"fleet_wide={d.get('fleet_wide')}, executions_affected={d.get('executions_affected')}"
    if r.tool == "flake_history":
        return f"flake precedent={d.get('precedent')}"
    return f"{r.tool} returned {len(d)} fields"


class Investigator:
    def __init__(
        self,
        client: ModelClient,
        ctx: ToolContext,
        budget: InvestigationBudget,
        *,
        model: str = "",
        max_response_tokens: int = DEFAULT_RESPONSE_TOKENS,
    ) -> None:
        self._client = client
        self._ctx = ctx
        self._budget = budget
        self._model = model
        self._max_tokens = max_response_tokens
        self._tools = agent_tool_definitions()

    # -- public ------------------------------------------------------------------------------
    def investigate(self, execution_id: str, context: str = "") -> Investigation:
        results: list[ToolResult] = []
        seen: set[str] = set()
        messages: list[dict[str, Any]] = [{"role": "user", "content": initial_message(execution_id, context)}]
        st = _Run(execution_id, results)
        nudged = False
        for _ in range(self._budget.limits.max_tool_calls + EXTRA_TURNS):
            if self._budget.tokens_exhausted:
                return self._truncated(st, "budget_tokens")
            try:
                resp = self._call(messages)
            except InvestigationBudgetExhausted:
                return self._truncated(st, "budget_tokens")
            except FixtureMissing:
                raise  # a replay setup problem, not something to escalate silently
            except GatewayError as e:  # spend cap, retries exhausted: fail fast and escalate
                return self._abstained(st, "model_unavailable", f"model unavailable: {type(e).__name__}")
            st.model_calls += 1

            final = self._final_input(resp)
            if final is not None:
                return self._finalize(st, final)
            if self._budget.tokens_exhausted:
                return self._truncated(st, "budget_tokens")
            if not resp.tool_calls:
                messages.append({"role": "assistant", "content": resp.text or "(no content)"})
                if nudged:
                    return self._abstained(st, "no_report", "the model ended without submitting a report")
                nudged = True
                messages.append({"role": "user", "content": f"Finish by calling {SUBMIT_REPORT}."})
                continue

            messages.append({"role": "assistant", "content": self._assistant_blocks(resp)})
            blocks: list[dict[str, Any]] = []
            for call in resp.tool_calls:
                if not call.id or call.id in seen:
                    st.duplicates += 1
                    blocks.append(_tool_result_block(call.id, "rejected: tool call ids must be unique", True))
                    continue
                if not self._budget.consume_tool_call():
                    return self._truncated(st, "budget_tool_calls")
                seen.add(call.id)
                result = dispatch(self._ctx, call.name, call.input, call.id)
                results.append(result)
                blocks.append(_tool_result_block(call.id, _render(result), not result.ok))
            messages.append({"role": "user", "content": blocks})
        return self._abstained(st, "no_report", "turn limit reached without a report")

    # -- model I/O ---------------------------------------------------------------------------
    def _call(self, messages: list[dict[str, Any]]) -> ModelResponse:
        request = ModelRequest(
            system=SYSTEM_PROMPT,
            messages=list(messages),
            tools=self._tools,
            model=self._model,
            max_tokens=self._max_tokens,
        )
        before = len(self._budget.calls)
        resp = self._client.complete(request)
        if len(self._budget.calls) == before:  # the client does not log calls (the scripted fake does not)
            u = resp.usage
            self._budget.add_call(
                CallRecord(
                    model=self._model or "unrecorded",
                    input_tokens=u.input_tokens,
                    output_tokens=u.output_tokens,
                    cache_read_tokens=u.cache_read_tokens,
                    cache_creation_tokens=u.cache_creation_tokens,
                    cost_usd=0.0,
                    latency_s=0.0,
                    limiter_wait_s=0.0,
                    mode="unmetered",
                )
            )
        return resp

    @staticmethod
    def _assistant_blocks(resp: ModelResponse) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if resp.text:
            blocks.append({"type": "text", "text": resp.text})
        blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in resp.tool_calls]
        return blocks

    @staticmethod
    def _final_input(resp: ModelResponse) -> dict[str, Any] | None:
        for call in resp.tool_calls:
            if call.name == SUBMIT_REPORT:
                return dict(call.input)
        if not resp.tool_calls:
            return _extract_json_object(resp.text)
        return None

    # -- outcomes ----------------------------------------------------------------------------
    def _stats(self) -> RunStats:
        return RunStats(**self._budget.run_fields())

    def _investigation(
        self, st: _Run, report: Report, proposal: Proposal | None, stop: str, submitted: int = 0, dropped: int = 0
    ) -> Investigation:
        report.run = self._stats()
        return Investigation(
            report=report,
            tool_results=st.results,
            proposal=proposal,
            budget=self._budget,
            stop_reason=stop,
            evidence_submitted=submitted,
            evidence_dropped=dropped,
            duplicate_ids_rejected=st.duplicates,
            model_calls=st.model_calls,
        )

    def _partial_report(self, st: _Run, reason: str, *, truncated: bool) -> Report:
        """An abstaining report that carries whatever the tools returned, cited by their real ids."""
        evidence = [Evidence(r.source, _summarize(r), "partial evidence bundle") for r in st.results]
        sig = next((r for r in st.results if r.tool == "classify_signature" and r.ok and r.data.get("signature")), None)
        return Report(
            execution_id=st.execution_id,
            fingerprint=str(sig.data.get("fingerprint") or "") if sig else "",
            classification="unknown",
            layer=str(sig.data["layer"]) if sig and sig.data.get("layer") in LAYERS else DEFAULT_LAYER,
            confidence=0.0,
            hypothesis=f"abstained: {reason}",
            evidence=evidence,
            abstained=True,
            budget_truncated=truncated,
            escalation_reason=reason,
        )

    def _truncated(self, st: _Run, stop: str) -> Investigation:
        what = "tool call" if stop == "budget_tool_calls" else "token"
        reason = f"investigation {what} budget exhausted; partial evidence attached"
        return self._investigation(st, self._partial_report(st, reason, truncated=True), None, stop)

    def _abstained(self, st: _Run, stop: str, reason: str) -> Investigation:
        return self._investigation(st, self._partial_report(st, reason, truncated=False), None, stop)

    def _finalize(self, st: _Run, data: dict[str, Any]) -> Investigation:
        valid_ids = {r.source for r in st.results}
        raw_items = data.get("evidence")
        items = raw_items if isinstance(raw_items, list) else []
        kept: list[Evidence] = []
        for item in items:
            if (
                isinstance(item, dict)
                and _is_str(item.get("source"))
                and item["source"] in valid_ids
                and _is_str(item.get("finding"))
                and _is_str(item.get("supports"))
            ):
                kept.append(Evidence(item["source"], item["finding"], item["supports"]))
        submitted, dropped = len(items), len(items) - len(kept)

        problem = _validate(data)
        if problem:
            inv = self._abstained(st, "malformed_report", f"malformed report: {problem}")
            inv.report.evidence = kept
            inv.evidence_submitted, inv.evidence_dropped = submitted, dropped
            return inv
        if data.get("abstain") is True or not kept:
            reason = str(data.get("escalation_reason") or "")
            if data.get("abstain") is not True:
                reason = f"no cited evidence survived ({dropped} of {submitted} items dropped); abstaining"
            report = self._partial_report(st, reason or "the agent abstained", truncated=False)
            report.evidence = kept
            return self._investigation(st, report, None, "report", submitted, dropped)

        proposal = _proposal(data.get("remediation"))
        sc = data.get("suspected_change")
        report = Report(
            execution_id=st.execution_id,
            fingerprint=_fingerprint(st.results),
            classification=data["classification"],
            layer=data["layer"],
            confidence=float(data["confidence"]),
            hypothesis=data["hypothesis"],
            evidence=kept,
            suspected_change=SuspectedChange(sc["kind"], sc["ref"], sc["at"], sc["basis"]) if sc else None,
            remediation=proposal.remediation if proposal else None,
            escalation_reason="" if proposal else str(data.get("escalation_reason") or ""),
        )
        return self._investigation(st, report, proposal, "report", submitted, dropped)


@dataclass
class _Run:
    execution_id: str
    results: list[ToolResult]
    model_calls: int = 0
    duplicates: int = 0


def _fingerprint(results: list[ToolResult]) -> str:
    sig = next((r for r in results if r.tool == "classify_signature" and r.ok and r.data.get("fingerprint")), None)
    return str(sig.data["fingerprint"]) if sig else ""


def _tool_result_block(call_id: str, content: str, is_error: bool) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": call_id, "content": content, "is_error": is_error}


def _render(r: ToolResult) -> str:
    return json.dumps({"error": r.error} if not r.ok else r.data, separators=(",", ":"), default=str)


def _validate(data: dict[str, Any]) -> str:
    """Empty string when the report's scalar fields are usable; otherwise what is wrong (names only)."""
    if data.get("classification") not in CLASSIFICATIONS:
        return "classification"
    if data.get("layer") not in LAYERS:
        return "layer"
    conf = data.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, int | float) or not 0 <= conf <= 1:
        return "confidence"
    if not _is_str(data.get("hypothesis")):
        return "hypothesis"
    sc = data.get("suspected_change")
    if sc is not None and not (
        isinstance(sc, dict) and all(_is_str(sc.get(k)) for k in ("kind", "ref", "at", "basis"))
    ):
        return "suspected_change"
    rem = data.get("remediation")
    if rem is not None:
        if not isinstance(rem, dict):
            return "remediation"
        tier = rem.get("tier")
        if isinstance(tier, bool) or tier not in TIERS:
            return "remediation.tier"
        if not (_is_str(rem.get("action")) and rem["action"] and _is_str(rem.get("rationale"))):
            return "remediation.action"
        if not isinstance(rem.get("reversible"), bool):
            return "remediation.reversible"
        paths = rem.get("paths", [])
        if not (isinstance(paths, list) and all(_is_str(p) for p in paths)):
            return "remediation.paths"
        if not isinstance(rem.get("params", {}), dict):
            return "remediation.params"
        if not _is_str(rem.get("diff", "")):
            return "remediation.diff"
    return ""


def _proposal(rem: dict[str, Any] | None) -> Proposal | None:
    if not rem:
        return None
    tier = int(rem["tier"])
    gate = rem.get("gate") if _is_str(rem.get("gate")) else TIER_GATE[tier]
    remediation = Remediation(
        tier=tier,
        action=rem["action"],
        rationale=rem["rationale"],
        reversible=rem["reversible"],
        gate=gate,
        gate_decision="allowed",  # the pre-gate default; only SafetyGate changes it
    )
    return Proposal(remediation, tuple(rem.get("paths", [])), dict(rem.get("params", {})), str(rem.get("diff", "")))
