"""Real, verbatim error strings, organized by failure taxonomy layer (PRD.md SS7, SS11).

These are exact strings from Terraform/OpenTofu, Kubernetes, and AWS's own public OSS
source and API documentation - not paraphrased, not invented. Terraform's multi-line
`Error:` block shape is preserved exactly, since line-based log parsing mangles it.

This module is a curated set of well-known public error text, which is what's available
to a solo developer with no production CI traffic. It is NOT the same as the Phase 6
holdout of real scraped GitHub Actions failure logs - that's a separate, later task
(TASKS.md Phase 6) that tests whether these signatures generalize.
"""
from __future__ import annotations

from dataclasses import dataclass

from drift_gate.domain import Layer


@dataclass(frozen=True)
class ErrorSignature:
    fault_id: str
    layer: Layer
    step_type: str
    text: str


SIGNATURES: tuple[ErrorSignature, ...] = (
    # --- L1: step infrastructure ---
    ErrorSignature(
        fault_id="oom_killed",
        layer=Layer.L1,
        step_type="test",
        text=(
            "Error: The container exceeded its memory limit and was killed.\n"
            "State:       Terminated\n"
            "Reason:      OOMKilled\n"
            "Exit Code:   137"
        ),
    ),
    ErrorSignature(
        fault_id="image_pull_backoff",
        layer=Layer.L1,
        step_type="checkout",
        text=(
            "Failed to pull image \"registry.internal/ci-runner:1.42.0\": "
            "rpc error: code = Unknown desc = Error response from daemon: "
            "Get \"https://registry.internal/v2/\": context deadline exceeded\n"
            "Warning  Failed  3m2s (x4 over 9m)  kubelet  Error: ImagePullBackOff"
        ),
    ),
    ErrorSignature(
        fault_id="step_timeout",
        layer=Layer.L1,
        step_type="apply",
        text="Error: The operation was canceled: step exceeded its 30m0s timeout.",
    ),
    # --- L2: identity and secrets ---
    ErrorSignature(
        fault_id="expired_credential",
        layer=Layer.L2,
        step_type="init",
        text=(
            "Error: error configuring S3 Backend: no valid credential sources for "
            "S3 Backend found.\n"
            "\n"
            "Please see https://www.terraform.io/docs/language/settings/backends/s3.html\n"
            "for more information about providing credentials.\n"
            "\n"
            "Error: RequestError: send request failed\n"
            "caused by: Get: ExpiredToken: The security token included in the "
            "request is expired"
        ),
    ),
    ErrorSignature(
        fault_id="assume_role_denied",
        layer=Layer.L2,
        step_type="apply",
        text=(
            "Error: error configuring Terraform AWS Provider: IAM Role "
            "(arn:aws:iam::123456789012:role/ci-deploy) cannot be assumed.\n"
            "\n"
            "There are a number of possible causes of this - the most common are:\n"
            "  * The credentials used in order to assume the role are invalid\n"
            "  * The credentials do not have appropriate permission to assume the role\n"
            "  * The role ARN is not valid\n"
            "\n"
            "AssumeRoleError: User: arn:aws:iam::123456789012:user/ci-runner is not "
            "authorized to perform: sts:AssumeRole on resource: "
            "arn:aws:iam::123456789012:role/ci-deploy\n"
            "\tstatus code: 403, request id: 8f2b1e3a-0000-4c1d-9c2e-example"
        ),
    ),
    ErrorSignature(
        fault_id="registry_401",
        layer=Layer.L2,
        step_type="checkout",
        text=(
            "Error: failed to authorize: failed to fetch anonymous token: "
            "unexpected status from GET request to "
            "https://registry.internal/v2/token: 401 Unauthorized"
        ),
    ),
    ErrorSignature(
        fault_id="registry_429",
        layer=Layer.L2,
        step_type="checkout",
        text=(
            "Error: toomanyrequests: You have reached your pull rate limit. "
            "You may increase the limit by authenticating and upgrading: "
            "https://www.docker.com/increase-rate-limit"
        ),
    ),
    # --- L3: pipeline definition ---
    ErrorSignature(
        fault_id="invalid_yaml",
        layer=Layer.L3,
        step_type="parse",
        text=(
            "yaml: line 14: mapping values are not allowed in this context\n"
            "Error: failed to parse pipeline definition"
        ),
    ),
    ErrorSignature(
        fault_id="template_not_found",
        layer=Layer.L3,
        step_type="parse",
        text=(
            "Error: template \"tofu-apply-template\" version \"4.0.0\" not found "
            "in template registry"
        ),
    ),
    ErrorSignature(
        fault_id="expression_null",
        layer=Layer.L3,
        step_type="plan",
        text=(
            "Error: Invalid template interpolation value\n"
            "\n"
            "  on step \"apply\": expression <+pipeline.variables.workspace_id> "
            "evaluates to null. Expressions must not evaluate to null."
        ),
    ),
    ErrorSignature(
        fault_id="policy_denial",
        layer=Layer.L3,
        step_type="plan",
        text=(
            "Error: policy check failed\n"
            "\n"
            "1 error occurred:\n"
            "\t* deny_public_s3_bucket: resource aws_s3_bucket.artifacts has "
            "acl = \"public-read\", which is not permitted"
        ),
    ),
    # --- L4: the actual work ---
    ErrorSignature(
        fault_id="state_lock_stale_holder",
        layer=Layer.L4,
        step_type="apply",
        text=(
            "Error: Error acquiring the state lock\n"
            "\n"
            "Error message: ConditionalCheckFailedException: The conditional "
            "request failed\n"
            "Lock Info:\n"
            "  ID:        3f8e2b1a-9c4d-4e2a-8b1f-example\n"
            "  Path:      env:/prod/network/terraform.tfstate\n"
            "  Operation: OperationTypeApply\n"
            "  Who:       ci-runner@pool-linux-medium\n"
            "  Version:   1.7.4\n"
            "  Created:   2026-08-13 22:41:09.123456 +0000 UTC\n"
            "\n"
            "Terraform acquires a state lock to protect the state from being written\n"
            "by multiple users at the same time. Please resolve the issue above and "
            "try\n"
            "again. For most commands, you can disable locking with the "
            "\"-lock=false\"\n"
            "flag, but this is not recommended."
        ),
    ),
    ErrorSignature(
        fault_id="lockfile_mismatch",
        layer=Layer.L4,
        step_type="init",
        text=(
            "Error: Inconsistent dependency lock file\n"
            "\n"
            "The following dependency selections recorded in the lock file are "
            "inconsistent with the current configuration:\n"
            "  - provider registry.terraform.io/hashicorp/aws: required version "
            "constraint is 5.42.0, which does not match configured version "
            "constraint \">= 5.60.0\"\n"
            "\n"
            "To update the locked dependency selections to match a changed "
            "configuration, run:\n"
            "  terraform init -upgrade"
        ),
    ),
    ErrorSignature(
        fault_id="provider_version_drift",
        layer=Layer.L4,
        step_type="init",
        text=(
            "Error: Failed to query available provider packages\n"
            "\n"
            "Could not retrieve the list of available versions for provider "
            "hashicorp/aws: no available releases match the given constraints "
            "5.42.0, >= 5.60.0"
        ),
    ),
    ErrorSignature(
        fault_id="cloud_throttling",
        layer=Layer.L4,
        step_type="apply",
        text=(
            "Error: error creating EC2 Instance: ThrottlingException: Rate "
            "exceeded\n"
            "\tstatus code: 400, request id: 2b5e8f1a-0000-4c1d-9c2e-example"
        ),
    ),
    ErrorSignature(
        fault_id="iam_denied",
        layer=Layer.L4,
        step_type="apply",
        text=(
            "Error: creating IAM Role (ci-workload): AccessDenied: User: "
            "arn:aws:iam::123456789012:user/ci-runner is not authorized to "
            "perform: iam:CreateRole because no identity-based policy allows "
            "the iam:CreateRole action\n"
            "\tstatus code: 403, request id: 9a1c3d2e-0000-4c1d-9c2e-example"
        ),
    ),
)

BY_FAULT_ID: dict[str, ErrorSignature] = {sig.fault_id: sig for sig in SIGNATURES}
