"""N3 scoring: the report's `suspected_change` against ground truth (only `eval/` reads ground truth, invariant 8).

Two metrics, kept separate and never folded into any MVP 1 metric:
- suspected-change accuracy: over cases whose label has a true causal change, the share where the report named a
  causal change. A report that names nothing, or a wrong change, is a miss.
- decoy blame rate: over reports that NAMED a suspected_change, the share that named a decoy (a change in the
  label's decoy list). Naming a change that is neither causal nor a labelled decoy counts in the denominator only.

How a `suspected_change` is matched to a timeline entry (`resolve_change`): `ref` equals a `change_id`, or a commit
`sha` (or a prefix of at least 7 characters); failing that, `(kind, ref, at)` equal to one entry. The tool tells the
model to cite the `change_id`. An unresolvable reference names no known change.

With the scripted model these numbers validate this code only; they say nothing about a real model.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from driftgate.domain import SuspectedChange
from driftgate.eval.metrics import Rate

MIN_SHA_PREFIX = 7


@dataclass(frozen=True)
class ChangeCase:
    """One scored case: what the label says about changes, and what the report named."""

    scenario_id: str
    held_out: bool
    causal_ids: tuple[str, ...]
    decoy_ids: tuple[str, ...]
    named: bool  # the report carried a suspected_change at all
    named_id: str | None  # the timeline entry it resolved to (None: unresolvable)


def resolve_change(sc: SuspectedChange | None, timeline: Iterable[dict[str, Any]]) -> str | None:
    """The `change_id` of the timeline entry a suspected_change refers to, or None."""
    if sc is None:
        return None
    entries = list(timeline)
    ref = sc.ref.strip()
    for c in entries:
        if ref and ref == c["change_id"]:
            return str(c["change_id"])
    for c in entries:
        sha = str(c.get("sha") or "")
        if sha and ref and (ref == sha or (len(ref) >= MIN_SHA_PREFIX and sha.startswith(ref))):
            return str(c["change_id"])
    for c in entries:
        if (c["kind"], c["ref"], c["at"]) == (sc.kind, sc.ref, sc.at):
            return str(c["change_id"])
    return None


def change_case(
    scenario_id: str,
    held_out: bool,
    causal_ids: Iterable[str],
    decoy_ids: Iterable[str],
    suspected: SuspectedChange | None,
    timeline: Iterable[dict[str, Any]],
) -> ChangeCase:
    return ChangeCase(
        scenario_id,
        held_out,
        tuple(causal_ids),
        tuple(decoy_ids),
        suspected is not None,
        resolve_change(suspected, timeline),
    )


@dataclass(frozen=True)
class ChangeMetrics:
    suspected_change_accuracy: Rate  # correct causal change named / cases with a true causal change
    decoy_blame_rate: Rate  # reports blaming a decoy / reports that named a suspected_change
    named_other: int  # named a change that is neither causal nor a labelled decoy (denominator of blame only)


def compute_change_metrics(cases: Iterable[ChangeCase]) -> ChangeMetrics:
    rows = list(cases)
    with_cause = [c for c in rows if c.causal_ids]
    named = [c for c in rows if c.named]
    correct = sum(c.named_id is not None and c.named_id in c.causal_ids for c in with_cause)
    blamed = sum(c.named_id is not None and c.named_id in c.decoy_ids and c.named_id not in c.causal_ids for c in named)
    other = sum(c.named_id is None or (c.named_id not in c.causal_ids and c.named_id not in c.decoy_ids) for c in named)
    return ChangeMetrics(Rate(correct, len(with_cause)), Rate(blamed, len(named)), other)
