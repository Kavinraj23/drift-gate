"""Render failed-step logs from the verbatim catalog only (invariant 9)."""

from __future__ import annotations

import random
import string
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .catalog import Catalog
from .faults import FaultSpec

ANSI_RED = "\x1b[31m"
ANSI_RESET = "\x1b[0m"


def placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def stamp(dt: datetime) -> str:
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{dt.microsecond:06d}0Z"


def common_params(rng: random.Random, pipeline: str, eco: str, start: datetime, spec: FaultSpec) -> dict[str, Any]:
    h = lambda n: "".join(rng.choice("0123456789abcdef") for _ in range(n))  # noqa: E731
    created = start - timedelta(seconds=rng.randint(30, 900))
    return {
        "lock_id": f"{h(8)}-{h(4)}-{h(4)}-{h(4)}-{h(12)}",
        "state_path": f"acme-tfstate/{pipeline}/terraform.tfstate",
        "who": f"runner@fv-az{rng.randint(100, 999)}-{rng.randint(10, 99)}",
        "tf_version": "1.5.7",
        "created": f"{created:%Y-%m-%d %H:%M:%S}.{rng.randint(0, 999999999):09d} +0000 UTC",
        "operation": "DescribeInstances" if eco == "tf" else "GetTables",
        "retries": rng.choice([2, 4]),
        "image": f"registry.example.com/ci/{pipeline}:{h(7)}",
        "code": spec.exit_code if spec.exit_code is not None else 1,
        "command": "",
    }


@dataclass
class RenderedLog:
    text: str
    entries: list[dict[str, Any]]  # catalog usage: {"entry": id, "params": {...}}
    summary: str


def render_failed_step(
    catalog: Catalog,
    spec: FaultSpec,
    eco: str,
    params: dict[str, Any],
    start: datetime,
    rng: random.Random,
    catalog_ids: tuple[str, ...] | None = None,
    ansi: bool = False,
) -> RenderedLog:
    ids = spec.catalog_ids if catalog_ids is None else catalog_ids
    command = spec.command[eco]
    sequence: list[tuple[str, dict[str, Any]]] = [("gha_group", {**params, "command": command})]
    sequence += [(i, params) for i in ids]
    if spec.exit_code is not None:
        sequence.append(("gha_exit_code", params))
    lines: list[str] = []
    used: list[dict[str, Any]] = []
    for entry_id, p in sequence:
        entry = catalog[entry_id]
        rendered = entry.render(p)
        if entry.box:
            body = ["╷"] + [f"│ {line}" for line in rendered] + ["╵"]
            if ansi:
                body = [f"{ANSI_RED}{line}{ANSI_RESET}" for line in body]
            rendered = body
        lines.extend(rendered)
        keys = set().union(*(placeholders(t) for t in entry.lines))
        used.append({"entry": entry_id, "params": {k: p[k] for k in sorted(keys)}})
    t = start + timedelta(seconds=rng.randint(2, 20))
    stamped = []
    for line in lines:
        t += timedelta(microseconds=rng.randint(1000, 90000))
        stamped.append(f"{stamp(t)} {line}")
    first_id = ids[0] if ids else "gha_exit_code"
    summary = catalog[first_id].render_summary(params)
    return RenderedLog(text="\n".join(stamped) + "\n", entries=used, summary=summary)
