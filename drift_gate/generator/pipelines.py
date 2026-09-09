"""The ~12 synthetic pipeline definitions (PRD.md SS11).

Connector/template/pool refs are deliberately shared across several pipelines - those
shared dimensions are exactly what fleet correlation (PRD.md SS8 step 3) and the
generator's correlated bursts key off of.
"""
from __future__ import annotations

from dataclasses import dataclass

from drift_gate.domain import TemplateRef

TOFU_APPLY = TemplateRef("tofu-apply-template", "3.4.0")
TOFU_PLAN = TemplateRef("tofu-plan-template", "2.1.0")
K8S_DEPLOY = TemplateRef("k8s-deploy-template", "1.9.0")
TEST_TEMPLATE = TemplateRef("test-template", "1.0.0")


@dataclass(frozen=True)
class PipelineDef:
    id: str
    connector_ref: str
    template_ref: TemplateRef
    runner_pool: str
    infra_ref: str
    step_chain: tuple[str, ...]
    flaky: bool = False


PIPELINES: tuple[PipelineDef, ...] = (
    PipelineDef("network-apply", "conn-aws-prod-01", TOFU_APPLY, "pool-linux-medium",
                "infra-def-42", ("checkout", "init", "plan", "apply")),
    PipelineDef("network-plan-only", "conn-aws-prod-01", TOFU_PLAN, "pool-linux-small",
                "infra-def-42", ("checkout", "init", "plan")),
    PipelineDef("database-apply", "conn-aws-prod-01", TOFU_APPLY, "pool-linux-medium",
                "infra-def-7", ("checkout", "init", "plan", "apply")),
    PipelineDef("compute-apply", "conn-aws-prod-02", TOFU_APPLY, "pool-linux-large",
                "infra-def-13", ("checkout", "init", "plan", "apply")),
    PipelineDef("compute-plan-only", "conn-aws-prod-02", TOFU_PLAN, "pool-linux-small",
                "infra-def-13", ("checkout", "init", "plan")),
    PipelineDef("staging-apply", "conn-aws-staging-01", TOFU_APPLY, "pool-linux-medium",
                "infra-def-42", ("checkout", "init", "plan", "apply")),
    PipelineDef("gcp-apply", "conn-gcp-01", TOFU_APPLY, "pool-linux-medium",
                "infra-def-13", ("checkout", "init", "plan", "apply")),
    PipelineDef("k8s-deploy-network", "conn-aws-prod-01", K8S_DEPLOY, "pool-linux-medium",
                "infra-def-7", ("checkout", "deploy")),
    PipelineDef("k8s-deploy-compute", "conn-aws-prod-02", K8S_DEPLOY, "pool-linux-medium",
                "infra-def-13", ("checkout", "deploy")),
    PipelineDef("integration-tests", "conn-aws-prod-01", TEST_TEMPLATE, "pool-linux-large",
                "infra-def-42", ("checkout", "init", "test"), flaky=True),
    PipelineDef("contract-tests", "conn-aws-staging-01", TEST_TEMPLATE, "pool-linux-small",
                "infra-def-7", ("checkout", "test"), flaky=True),
    PipelineDef("teardown-cleanup", "conn-gcp-01", TOFU_APPLY, "pool-linux-small",
                "infra-def-13", ("checkout", "init", "plan", "apply")),
)

BY_ID: dict[str, PipelineDef] = {p.id: p for p in PIPELINES}
FLAKY_IDS: tuple[str, ...] = tuple(p.id for p in PIPELINES if p.flaky)
