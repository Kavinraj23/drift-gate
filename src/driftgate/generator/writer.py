"""Write a generated Dataset to disk and validate it against the generator's JSON schemas."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from jsonschema import Draft202012Validator

from .population import Dataset

SCHEMA_DIR = Path(__file__).with_name("schemas")
GENERATOR_DIR = Path(__file__).parent

# (data file relative to dataset root, schema file name)
VALIDATED = (
    ("manifest.json", "manifest.schema.json"),
    ("executions.json", "executions.schema.json"),
    ("changes.json", "changes.schema.json"),
    ("world/followups.json", "followups.schema.json"),
    ("ground_truth/labels.json", "labels.schema.json"),
)


class DatasetValidationError(Exception):
    pass


def write_dataset(dataset: Dataset, out_dir: Path) -> None:
    """Replace `out_dir` with the dataset (a stale tree must never mix with a new one)."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    for rel, text in dataset.files.items():
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def _validator(schema_name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / schema_name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_catalog() -> None:
    data = json.loads((GENERATOR_DIR / "error_catalog.json").read_text(encoding="utf-8"))
    errors = sorted(_validator("error_catalog.schema.json").iter_errors(data), key=str)
    if errors:
        raise DatasetValidationError(f"error_catalog.json: {errors[0].message}")


def validate_files(files: dict[str, str]) -> None:
    """Validate the in-memory dataset (relative path -> text)."""
    validate_catalog()
    for rel, schema_name in VALIDATED:
        if rel not in files:
            raise DatasetValidationError(f"missing {rel}")
        errors = sorted(_validator(schema_name).iter_errors(json.loads(files[rel])), key=lambda e: list(e.path))
        if errors:
            e = errors[0]
            raise DatasetValidationError(f"{rel}: {e.message} at {list(e.path)}")


def validate_dataset(out_dir: Path) -> None:
    files = {rel: (out_dir / rel).read_text(encoding="utf-8") for rel, _ in VALIDATED if (out_dir / rel).exists()}
    validate_files(files)
