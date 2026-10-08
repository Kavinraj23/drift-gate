"""Independent PR reviewer agent (Tier 3 only).

It judges a proposed change on its merits. It receives the diff, the current contents of the files the diff
touches, and the investigator's hypothesis, and nothing else: not the investigator's tool transcript, not its
reasoning, not its confidence, not its evidence. Its only tool is `read_repo_file` (plus `submit_review`, a
no-side-effect tool that carries the verdict out of the loop). It runs on the same `ModelClient` interface and the
same `InvestigationBudget` class as the investigator, with its own static system prompt.

Fail closed: a missing, malformed or unreachable verdict is a `reject`, which escalates. The reviewer cannot
approve its way past the deterministic diff checks; those run before it and after any revision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from driftgate.domain import REVIEW_VERDICTS, Review
from driftgate.llm.budget import CallRecord, InvestigationBudget
from driftgate.llm.errors import FixtureMissing, GatewayError, InvestigationBudgetExhausted
from driftgate.llm.types import ModelClient, ModelRequest, ModelResponse
from driftgate.tools import ToolContext, ToolResult, dispatch, tool_definitions

SUBMIT_REVIEW = "submit_review"
REVIEWER_TOOLS = ("read_repo_file",)
DEFAULT_RESPONSE_TOKENS = 1200
EXTRA_TURNS = 3
MAX_FILE_CHARS = 6000

SYSTEM_PROMPT = f"""You are DriftGate's pull request reviewer, a senior platform engineer reviewing a small \
automated fix before it becomes a pull request. You are independent: you were not part of the investigation and \
you do not see how the change was derived. Judge the change on its merits.

You are given the hypothesis about why a CI pipeline failed, the unified diff that is meant to fix it, and the \
current contents of the files it touches. You may call read_repo_file to look at other files in the repository \
at the same ref; you cannot change anything.

Check
- Does the diff fix the failure the hypothesis describes, and only that failure?
- Does it touch only files related to the failure? Reject unrelated workflow, config or documentation edits.
- Is it minimal? Reject collateral changes, such as unrelated version bumps, reformatting or extra settings.
- Does it avoid deleting files or dependencies to make the error go away? A lockfile must be regenerated, not \
deleted; a missing dependency must be declared, not removed.
- Does it add anything that looks like a secret or credential, or touch secret handling, without that being the \
fix? Reject it.
- Where several files change, are additions made before removals?

Verdicts
- approve: the change is correct, minimal and safe.
- revise: the intent is right but the diff needs a specific correction; say exactly what to change. You may ask \
for revision at most once.
- reject: the change is wrong, unsafe or unrelated; a human should look at the failure instead.

Finish by calling {SUBMIT_REVIEW} exactly once with the verdict and short, specific comments."""


def submit_review_definition() -> dict[str, Any]:
    return {
        "name": SUBMIT_REVIEW,
        "description": "Finish the review with your verdict and comments. Call it exactly once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {"enum": list(REVIEW_VERDICTS)},
                "comments": {"type": "string", "description": "Specific, actionable comments."},
            },
            "required": ["verdict", "comments"],
        },
    }


def reviewer_tool_definitions() -> list[dict[str, Any]]:
    """read_repo_file only, plus the verdict tool; stable order (cache friendly)."""
    return [*tool_definitions(REVIEWER_TOOLS), submit_review_definition()]


@dataclass(frozen=True)
class ReviewRequest:
    """Everything the reviewer is allowed to know."""

    repo: str
    ref: str
    hypothesis: str
    diff_text: str
    files: dict[str, str] = field(default_factory=dict)  # target file path -> contents before the change
    round: int = 0  # 0 for the first review, 1 for the review of a revised diff


def initial_message(req: ReviewRequest) -> str:
    parts = [
        f"Review this proposed change (review round {req.round + 1}).",
        f"Repository: {req.repo} at ref {req.ref} (use these as the repo and ref arguments of read_repo_file).",
        f"Hypothesis about the failure: {req.hypothesis}",
        "Proposed unified diff:\n```diff\n" + req.diff_text.rstrip("\n") + "\n```",
    ]
    for path, content in sorted(req.files.items()):
        shown = content[:MAX_FILE_CHARS]
        note = "\n[truncated]" if len(content) > MAX_FILE_CHARS else ""
        parts.append(f"Current contents of {path}:\n```\n{shown}{note}\n```")
    if not req.files:
        parts.append("The diff touches no file that exists yet.")
    return "\n\n".join(parts)


@dataclass
class ReviewResult:
    review: Review
    stop_reason: str  # verdict | budget_tool_calls | budget_tokens | model_unavailable | no_verdict | malformed_verdict
    budget: InvestigationBudget
    model_calls: int = 0
    tool_results: list[ToolResult] = field(default_factory=list)
    offered_tools: tuple[str, ...] = ()
    refused_tool_calls: int = 0

    @property
    def valid(self) -> bool:
        return self.stop_reason == "verdict"


class Reviewer:
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
        self._tools = reviewer_tool_definitions()

    @property
    def offered_tools(self) -> tuple[str, ...]:
        return tuple(t["name"] for t in self._tools)

    def review(self, req: ReviewRequest) -> ReviewResult:
        messages: list[dict[str, Any]] = [{"role": "user", "content": initial_message(req)}]
        res = ReviewResult(Review("reject", ""), "no_verdict", self._budget, offered_tools=self.offered_tools)
        seen: set[str] = set()
        nudged = False
        for _ in range(self._budget.limits.max_tool_calls + EXTRA_TURNS):
            if self._budget.tokens_exhausted:
                return self._fail(res, "budget_tokens", "review token budget exhausted")
            try:
                resp = self._call(messages)
            except InvestigationBudgetExhausted:
                return self._fail(res, "budget_tokens", "review token budget exhausted")
            except FixtureMissing:
                raise
            except GatewayError as e:
                return self._fail(res, "model_unavailable", f"reviewer unavailable: {type(e).__name__}")
            res.model_calls += 1

            verdict = next((c for c in resp.tool_calls if c.name == SUBMIT_REVIEW), None)
            if verdict is not None:
                return self._verdict(res, dict(verdict.input))
            if self._budget.tokens_exhausted:
                return self._fail(res, "budget_tokens", "review token budget exhausted")
            if not resp.tool_calls:
                messages.append({"role": "assistant", "content": resp.text or "(no content)"})
                if nudged:
                    return self._fail(res, "no_verdict", "the reviewer ended without a verdict")
                nudged = True
                messages.append({"role": "user", "content": f"Finish by calling {SUBMIT_REVIEW}."})
                continue

            messages.append({"role": "assistant", "content": _assistant_blocks(resp)})
            blocks: list[dict[str, Any]] = []
            for call in resp.tool_calls:
                if not call.id or call.id in seen:
                    blocks.append(_result_block(call.id, "rejected: tool call ids must be unique", True))
                    continue
                seen.add(call.id)
                if call.name not in REVIEWER_TOOLS:  # the reviewer has one read-only tool, nothing else runs
                    res.refused_tool_calls += 1
                    blocks.append(_result_block(call.id, f"unavailable tool: {call.name}", True))
                    continue
                if not self._budget.consume_tool_call():
                    return self._fail(res, "budget_tool_calls", "review tool call budget exhausted")
                result = dispatch(self._ctx, call.name, call.input, call.id)
                res.tool_results.append(result)
                payload = {"error": result.error} if not result.ok else result.data
                blocks.append(_result_block(call.id, json.dumps(payload, separators=(",", ":")), not result.ok))
            messages.append({"role": "user", "content": blocks})
        return self._fail(res, "no_verdict", "turn limit reached without a verdict")

    # -- internals -------------------------------------------------------------------------------
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
        if len(self._budget.calls) == before:  # a client that does not log calls (the scripted fake)
            u = resp.usage
            self._budget.add_call(
                CallRecord(
                    self._model or "unrecorded",
                    u.input_tokens,
                    u.output_tokens,
                    u.cache_read_tokens,
                    u.cache_creation_tokens,
                    0.0,
                    0.0,
                    0.0,
                    "unmetered",
                )
            )
        return resp

    @staticmethod
    def _fail(res: ReviewResult, stop: str, why: str) -> ReviewResult:
        res.stop_reason = stop
        res.review = Review("reject", f"no valid review ({why}); failing closed")
        return res

    @staticmethod
    def _verdict(res: ReviewResult, data: dict[str, Any]) -> ReviewResult:
        verdict, comments = data.get("verdict"), data.get("comments", "")
        if verdict not in REVIEW_VERDICTS or not isinstance(comments, str):
            return Reviewer._fail(res, "malformed_verdict", "verdict is not approve, revise or reject")
        res.stop_reason = "verdict"
        res.review = Review(verdict, comments)
        return res


def _assistant_blocks(resp: ModelResponse) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if resp.text:
        blocks.append({"type": "text", "text": resp.text})
    blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in resp.tool_calls]
    return blocks


def _result_block(call_id: str, content: str, is_error: bool) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": call_id, "content": content, "is_error": is_error}
