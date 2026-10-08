"""Fault catalog: every failure family the generator can inject, with its ground-truth attributes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FaultSpec:
    fault_id: str
    classification: str  # platform | user | transient | governance
    layer: str
    steps: dict[str, str]  # ecosystem -> failing step name
    command: dict[str, str]  # ecosystem -> command shown in the log
    catalog_ids: tuple[str, ...]
    exit_code: int | None
    tier: int | None  # correct remediation tier when the case is remediable
    action: str | None
    disposition: str  # remediate | escalate | close
    tier0_path: str | None = None  # known_transient_rule | flake_precedent
    repo_fault: bool = False
    weight: float = 1.0  # background selection weight; 0 means injected specially


_PLAN = "./scripts/plan.sh"
_DESCRIBE = "kubectl describe pod ci-job"

FAULTS: dict[str, FaultSpec] = {
    f.fault_id: f
    for f in (
        FaultSpec(
            "user_lockfile_mismatch",
            "user",
            "L4",
            {"node": "Install dependencies"},
            {"node": "npm ci"},
            ("npm_ci_lock_out_of_sync",),
            1,
            3,
            "regenerate_lockfile",
            "remediate",
            repo_fault=True,
            weight=3,
        ),
        FaultSpec(
            "user_provider_pin",
            "user",
            "L4",
            {"tf": "Terraform Init"},
            {"tf": "terraform init"},
            ("tf_provider_constraints",),
            1,
            3,
            "pin_provider_version",
            "remediate",
            repo_fault=True,
            weight=3,
        ),
        FaultSpec(
            "user_undefined_variable",
            "user",
            "L3",
            {"tf": "Terraform Plan"},
            {"tf": _PLAN},
            ("tf_undeclared_variable",),
            1,
            3,
            "fix_undefined_variable",
            "remediate",
            repo_fault=True,
            weight=3,
        ),
        FaultSpec(
            "user_pip_missing_dist",
            "user",
            "L4",
            {"python": "Install dependencies"},
            {"python": "pip install -r requirements.txt"},
            ("pip_no_matching_distribution",),
            1,
            3,
            "fix_requirement_pin",
            "remediate",
            repo_fault=True,
            weight=3,
        ),
        FaultSpec(
            "user_script_failure",
            "user",
            "L4",
            {"node": "Run tests", "python": "Run job", "tf": "Terraform Apply"},
            {"node": "npm test", "python": "python -m jobs.run", "tf": "terraform apply -input=false tfplan"},
            (),
            1,
            None,
            None,
            "escalate",
            weight=4,
        ),
        FaultSpec(
            "transient_throttling",
            "transient",
            "L4",
            {"tf": "Terraform Plan", "python": "Run job"},
            {"tf": _PLAN, "python": "aws glue get-tables --database-name analytics"},
            ("aws_throttling",),
            254,
            0,
            "rerun_failed_job",
            "remediate",
            tier0_path="known_transient_rule",
            weight=3,
        ),
        FaultSpec(
            "transient_registry_rate_limit",
            "transient",
            "L2",
            {"node": "Build image"},
            {"node": "docker build -t app ."},
            ("docker_pull_rate_limit",),
            1,
            0,
            "rerun_failed_job",
            "remediate",
            tier0_path="known_transient_rule",
            weight=3,
        ),
        FaultSpec(
            "transient_image_pull",
            "transient",
            "L1",
            {"node": "Run tests", "python": "Run job"},
            {"node": _DESCRIBE, "python": _DESCRIBE},
            ("k8s_image_pull_backoff",),
            1,
            0,
            "rerun_failed_job",
            "remediate",
            tier0_path="flake_precedent",
            weight=3,
        ),
        FaultSpec(
            "transient_flaky_test",
            "transient",
            "L4",
            {"node": "Run tests"},
            {"node": "npm test"},
            (),
            1,
            0,
            "rerun_failed_job",
            "remediate",
            tier0_path="flake_precedent",
            weight=0,
        ),
        FaultSpec(
            "platform_state_lock",
            "platform",
            "L4",
            {"tf": "Terraform Plan"},
            {"tf": _PLAN},
            ("tf_state_lock",),
            1,
            2,
            "force_unlock_state",
            "escalate",
            weight=3,
        ),
        FaultSpec(
            "platform_oom_killed",
            "platform",
            "L1",
            {"node": "Run tests", "python": "Run job"},
            {"node": _DESCRIBE, "python": _DESCRIBE},
            ("k8s_oom_killed",),
            137,
            1,
            "bump_pool_capacity",
            "escalate",
            weight=3,
        ),
        FaultSpec(
            "platform_expired_token",
            "platform",
            "L2",
            {"tf": "Configure AWS credentials", "python": "Configure AWS credentials"},
            {"tf": "aws sts get-caller-identity", "python": "aws sts get-caller-identity"},
            ("aws_expired_token",),
            254,
            1,
            "refresh_connector_token",
            "escalate",
            weight=3,
        ),
        FaultSpec(
            "platform_template_bump",
            "platform",
            "L3",
            {"tf": "Terraform Plan"},
            {"tf": _PLAN},
            ("tf_undeclared_variable",),
            1,
            3,
            "revert_template_bump",
            "remediate",
            repo_fault=True,
            weight=0,
        ),
        FaultSpec(
            "platform_throttle_masks_lock",
            "platform",
            "L4",
            {"tf": "Terraform Plan"},
            {"tf": _PLAN},
            ("aws_throttling", "tf_state_lock"),
            1,
            2,
            "force_unlock_state",
            "escalate",
            weight=0,
        ),
        FaultSpec(
            "governance_approval_rejected",
            "governance",
            "L5",
            {"tf": "Manual approval"},
            {"tf": "approval gate"},
            (),
            None,
            None,
            None,
            "close",
            weight=0,
        ),
    )
}
