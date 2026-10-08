"""The synthetic fleet: 12 pipelines with the shared dependencies that make fleet correlation possible."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PipelineDef:
    name: str
    eco: str  # node | python | tf | canary
    connector: str
    runner_pool: str
    template: str  # template name without version
    template_version: str
    infra_def: str
    runs_per_day: int
    flaky: bool = False

    def refs(self, template_version: str | None = None) -> dict[str, str]:
        return {
            "connector": self.connector,
            "template": f"{self.template}@{template_version or self.template_version}",
            "runner_pool": self.runner_pool,
            "infra_def": self.infra_def,
        }


TF_TEMPLATE = "terraform-plan"
TF_GOOD_VERSION = "v2.0.0"
TF_BAD_VERSION = "v2.1.0"

PIPELINES: tuple[PipelineDef, ...] = (
    PipelineDef("web-build", "node", "ghcr-conn", "pool-small", "node-build", "v1.4.0", "none", 5),
    PipelineDef("api-build", "node", "ghcr-conn", "pool-small", "node-build", "v1.4.0", "none", 5),
    PipelineDef("docs-site", "node", "ghcr-conn", "pool-small", "node-build", "v1.4.0", "none", 5),
    PipelineDef("data-etl", "python", "aws-shared", "pool-medium", "py-job", "v3.2.1", "etl-stack", 4),
    PipelineDef("ml-train", "python", "aws-data", "pool-small", "py-job", "v3.2.1", "ml-stack", 4),
    PipelineDef("infra-network", "tf", "aws-prod", "pool-large", TF_TEMPLATE, TF_GOOD_VERSION, "tf-network", 3),
    PipelineDef("infra-db", "tf", "aws-prod", "pool-large", TF_TEMPLATE, TF_GOOD_VERSION, "tf-db", 3),
    PipelineDef("infra-iam", "tf", "aws-shared", "pool-large", TF_TEMPLATE, TF_GOOD_VERSION, "tf-iam", 3),
    PipelineDef("infra-eks", "tf", "aws-prod", "pool-large", TF_TEMPLATE, TF_GOOD_VERSION, "tf-eks", 3),
    PipelineDef("infra-observability", "tf", "aws-shared", "pool-large", TF_TEMPLATE, TF_GOOD_VERSION, "tf-obs", 3),
    PipelineDef("canary-deploy-a", "canary", "gh-default", "pool-medium", "canary", "v1.0.0", "canary-a", 3, True),
    PipelineDef("canary-deploy-b", "canary", "gh-default", "pool-medium", "canary", "v1.0.0", "canary-b", 3, True),
)

PIPELINE_BY_NAME: dict[str, PipelineDef] = {p.name: p for p in PIPELINES}

# Steps per ecosystem, grouped into stages: (stage name, step names).
STAGES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "node": (("Build", ("Checkout", "Setup Node", "Install dependencies", "Run tests", "Build image")),),
    "python": (("Job", ("Checkout", "Setup Python", "Configure AWS credentials", "Install dependencies", "Run job")),),
    "tf": (
        ("Plan", ("Checkout", "Configure AWS credentials", "Terraform Init", "Terraform Plan")),
        ("Approval", ("Manual approval",)),
        ("Apply", ("Terraform Apply",)),
    ),
    "canary": (("Canary", ("Checkout", "Canary check")),),
}
