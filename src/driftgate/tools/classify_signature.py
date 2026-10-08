"""classify_signature: deterministic signature match of the failed step against the error-catalog families."""

from __future__ import annotations

from typing import Any

from .base import FailureAnalysis, ToolContext, ToolSpec
from .signatures import BY_ID


def describe(analysis: FailureAnalysis) -> dict[str, Any]:
    p = analysis.primary
    return {
        "execution_id": analysis.execution_id,
        "pipeline": analysis.pipeline,
        "step": analysis.step,
        "node_id": analysis.node_id,
        "signature": p.id,
        "deterministic_match": analysis.deterministic,
        "layer": p.layer,
        "classification": p.classification,
        "tier0_rule_exists": p.tier0_rule,
        "all_signatures": [
            {
                "id": sid,
                "layer": BY_ID[sid].layer,
                "classification": BY_ID[sid].classification,
                "tier0_rule_exists": BY_ID[sid].tier0_rule,
            }
            for sid in analysis.signature_ids
        ],
        "fingerprint": analysis.fingerprint,
    }


def classify_signature(ctx: ToolContext, execution_id: str) -> dict[str, Any]:
    analysis = ctx.analyzer.analyze(execution_id)
    if analysis is None:
        return {
            "execution_id": execution_id,
            "signature": None,
            "deterministic_match": False,
            "note": "execution did not fail; there is nothing to classify",
        }
    return describe(analysis)


def _handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return classify_signature(ctx, args["execution_id"])


SPEC = ToolSpec(
    name="classify_signature",
    description=(
        "Deterministic signature match on the failed step: signature id, layer, classification, whether a "
        "Tier 0 known-transient rule exists, and the failure fingerprint. A bare non-zero exit code is "
        "reported as exit_code_nonzero with deterministic_match=false: it names no cause. You may confirm "
        "or overturn the layer with evidence. Cheap."
    ),
    input_schema={
        "type": "object",
        "properties": {"execution_id": {"type": "string"}},
        "required": ["execution_id"],
        "additionalProperties": False,
    },
    handler=_handle,
)
