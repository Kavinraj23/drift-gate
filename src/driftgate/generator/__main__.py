"""`python -m driftgate.generator [--seed N] [--out DIR]` generates and validates the synthetic dataset."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .population import DEFAULT_SEED, generate
from .writer import DatasetValidationError, validate_dataset, write_dataset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="driftgate.generator")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, default=Path("data"))
    args = parser.parse_args(argv)
    dataset = generate(args.seed)
    write_dataset(dataset, args.out)
    try:
        validate_dataset(args.out)
    except DatasetValidationError as exc:
        print(f"schema validation FAILED: {exc}")
        return 1
    s = dataset.stats
    held = sum(1 for sc in dataset.scenarios if sc["held_out"])
    print(f"dataset written to {args.out} (seed {args.seed}); schema validation passed")
    print(
        f"executions={s['executions']} success={s['success']} failed={s['failed']} "
        f"approval_rejected={s['approval_rejected']} bursts={s['bursts']} changes={s['changes']}"
    )
    print(f"failures by class: {s['failures_by_class']}")
    print(f"scenarios={len(dataset.scenarios)} held_out={held}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
