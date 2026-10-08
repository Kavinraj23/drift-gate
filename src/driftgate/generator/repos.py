"""Synthetic repos: real files per pipeline, plus fault injection that returns the faulty and corrected trees."""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field

from .pipelines import TF_BAD_VERSION, TF_GOOD_VERSION, PipelineDef

TEMPLATE_REPO = "acme/ci-templates/.github/workflows"


@dataclass
class RepoFault:
    faulty: dict[str, str]
    fixed: dict[str, str]
    params: dict[str, str] = field(default_factory=dict)

    def fix_paths(self) -> list[str]:
        return sorted(p for p in self.fixed if self.fixed[p] != self.faulty.get(p))

    def fix_diff(self) -> str:
        return unified_diff(self.faulty, self.fixed)


def unified_diff(before: dict[str, str], after: dict[str, str]) -> str:
    chunks: list[str] = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        chunks.extend(
            difflib.unified_diff(
                (old or "").splitlines(keepends=True),
                (new or "").splitlines(keepends=True),
                fromfile=f"a/{path}" if old is not None else "/dev/null",
                tofile=f"b/{path}" if new is not None else "/dev/null",
            )
        )
    return "".join(chunks)


def _package_json(name: str, extra_dep: bool) -> str:
    deps = {"express": "^4.19.2"}
    if extra_dep:
        deps["dayjs"] = "^1.11.10"
    return (
        json.dumps(
            {"name": name, "version": "1.0.0", "scripts": {"test": "node --test"}, "dependencies": deps}, indent=2
        )
        + "\n"
    )


def _package_lock(name: str, with_dayjs: bool) -> str:
    root_deps = {"express": "^4.19.2"}
    packages: dict[str, object] = {
        "node_modules/express": {
            "version": "4.19.2",
            "resolved": "https://registry.npmjs.org/express/-/express-4.19.2.tgz",
        }
    }
    if with_dayjs:
        root_deps["dayjs"] = "^1.11.10"
        packages["node_modules/dayjs"] = {
            "version": "1.11.10",
            "resolved": "https://registry.npmjs.org/dayjs/-/dayjs-1.11.10.tgz",
        }
    lock = {
        "name": name,
        "version": "1.0.0",
        "lockfileVersion": 3,
        "requires": True,
        "packages": {"": {"name": name, "version": "1.0.0", "dependencies": root_deps}, **packages},
    }
    return json.dumps(lock, indent=2) + "\n"


def _node_files(p: PipelineDef) -> dict[str, str]:
    return {
        ".github/workflows/ci.yml": (
            "name: ci\non:\n  push:\n    branches: [main]\njobs:\n  build:\n"
            f"    runs-on: [self-hosted, {p.runner_pool}]\n    steps:\n"
            "      - uses: actions/checkout@v4\n      - uses: actions/setup-node@v4\n        with:\n"
            "          node-version: 20\n      - run: npm ci\n      - run: npm test\n"
            "      - run: docker build -t app .\n"
        ),
        "package.json": _package_json(p.name, False),
        "package-lock.json": _package_lock(p.name, False),
        "Dockerfile": (
            "FROM node:20-slim\nWORKDIR /app\nCOPY package*.json ./\nRUN npm ci --omit=dev\n"
            'COPY . .\nCMD ["node", "index.js"]\n'
        ),
        "README.md": f"# {p.name}\n\nBuild and test pipeline.\n",
    }


def _python_files(p: PipelineDef) -> dict[str, str]:
    return {
        ".github/workflows/job.yml": (
            "name: job\non:\n  schedule:\n    - cron: '0 * * * *'\njobs:\n  run:\n"
            f"    runs-on: [self-hosted, {p.runner_pool}]\n    steps:\n"
            "      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n        with:\n"
            "          python-version: '3.11'\n      - run: pip install -r requirements.txt\n"
            "      - run: python -m jobs.run\n"
        ),
        "requirements.txt": "requests==2.31.0\npandas==2.1.4\n",
        "config/job.yaml": f"job: {p.name}\nbatch_size: 500\ntimeout_seconds: 900\n",
        "README.md": f"# {p.name}\n\nScheduled data job.\n",
    }


def _tf_files(p: PipelineDef) -> dict[str, str]:
    return {
        ".github/workflows/plan.yml": (
            "name: plan\non:\n  push:\n    branches: [main]\njobs:\n  plan:\n"
            f"    uses: {TEMPLATE_REPO}/terraform-plan.yml@{TF_GOOD_VERSION}\n"
            f"    with:\n      working-directory: .\n      runner-pool: {p.runner_pool}\n"
        ),
        "scripts/plan.sh": (
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            'aws ec2 describe-instances --filters "Name=tag:Stack,Values=${STACK}" '
            "--query 'Reservations[].Instances[].InstanceId'\n"
            "terraform plan -input=false -out=tfplan\n"
        ),
        "versions.tf": (
            'terraform {\n  required_version = ">= 1.5.0"\n  required_providers {\n    aws = {\n'
            '      source  = "hashicorp/aws"\n      version = "~> 5.0"\n    }\n  }\n}\n'
        ),
        "variables.tf": (
            'variable "region" {\n  type    = string\n  default = "us-east-1"\n}\n\n'
            'variable "environment" {\n  type = string\n}\n'
        ),
        "main.tf": (
            'provider "aws" {\n  region = var.region\n}\n\n'
            f'resource "aws_s3_bucket" "main" {{\n  bucket = "acme-{p.name}-${{var.environment}}"\n\n'
            "  tags = {\n    Environment = var.environment\n  }\n}\n"
        ),
        "terraform.tfvars": 'environment = "prod"\n',
    }


def base_files(p: PipelineDef) -> dict[str, str]:
    return {"node": _node_files, "python": _python_files, "tf": _tf_files}[p.eco](p)


def _line_of(text: str, needle: str) -> tuple[int, str]:
    for i, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return i, line
    raise ValueError(needle)


def inject(fault_id: str, p: PipelineDef, base: dict[str, str]) -> RepoFault:
    """Return the faulty tree, its corrected tree and any parameters the error text needs."""
    faulty, fixed = dict(base), dict(base)
    params: dict[str, str] = {}
    if fault_id == "user_lockfile_mismatch":
        faulty["package.json"] = _package_json(p.name, True)
        fixed["package.json"] = _package_json(p.name, True)
        fixed["package-lock.json"] = _package_lock(p.name, True)
        params["npm_log_path"] = "/home/runner/.npm/_logs/2026-01-01T00_00_00_000Z-debug-0.log"
    elif fault_id == "user_provider_pin":
        faulty["versions.tf"] = base["versions.tf"].replace('"~> 5.0"', '">= 99.0.0"')
        params["constraint"] = ">= 99.0.0"
    elif fault_id == "user_undefined_variable":
        faulty["main.tf"] = base["main.tf"].replace(
            "    Environment = var.environment\n",
            "    Environment = var.environment\n    CostCenter  = var.cost_center\n",
        )
        fixed["main.tf"] = faulty["main.tf"]
        fixed["variables.tf"] = (
            base["variables.tf"] + '\nvariable "cost_center" {\n  type    = string\n  default = "platform"\n}\n'
        )
        line, text = _line_of(faulty["main.tf"], "var.cost_center")
        params.update(
            file="main.tf",
            line=str(line),
            context='resource "aws_s3_bucket" "main"',
            source_line=text,
            var_name="cost_center",
        )
    elif fault_id == "user_pip_missing_dist":
        faulty["requirements.txt"] = base["requirements.txt"].replace("requests==", "requets==")
        fixed["requirements.txt"] = base["requirements.txt"]
        params["requirement"] = "requets==2.31.0"
    elif fault_id == "platform_template_bump":
        path = ".github/workflows/plan.yml"
        faulty[path] = base[path].replace(f"@{TF_GOOD_VERSION}", f"@{TF_BAD_VERSION}")
        fixed[path] = base[path]
        params.update(
            file=".template/backend.tf",
            line="4",
            context="terraform",
            source_line="  bucket = var.state_bucket",
            var_name="state_bucket",
        )
    else:
        raise KeyError(fault_id)
    return RepoFault(faulty=faulty, fixed=fixed, params=params)
