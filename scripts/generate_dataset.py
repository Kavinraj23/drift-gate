"""CLI: python -m scripts.generate_dataset --seed 42 --out data/synthetic"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
from collections import Counter
from pathlib import Path

from drift_gate.dataset_io import save_population
from drift_gate.domain import ExecutionStatus
from drift_gate.generator.population import DEFAULT_ANCHOR_END, build_population


def _force_remove_readonly(func, path, exc_info):
    """git marks its object files read-only; rmtree can't delete those on Windows
    without clearing the flag first."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("data/synthetic"))
    args = parser.parse_args()

    if args.out.exists():
        shutil.rmtree(args.out, onerror=_force_remove_readonly)

    population = build_population(seed=args.seed, anchor_end=DEFAULT_ANCHOR_END, out_dir=args.out)
    save_population(population, args.out)

    n = len(population.executions)
    n_failed = sum(1 for e in population.executions if e.status == ExecutionStatus.FAILED)
    by_class = Counter(g.correct_classification.value for g in population.ground_truth)
    n_flake = sum(1 for g in population.ground_truth if g.is_flake)
    n_dup = sum(1 for g in population.ground_truth if g.is_duplicate_of is not None)

    print(f"executions: {n} ({n_failed} failed, {n - n_failed} succeeded, "
          f"{100 * (n - n_failed) / n:.1f}% success)")
    print(f"failure classification: {dict(by_class)}")
    print(f"flaky fail-then-pass: {n_flake}")
    print(f"burst duplicates: {n_dup}")
    print(f"events: {len(population.events)}")
    print(f"wrote dataset to {args.out}")


if __name__ == "__main__":
    main()
