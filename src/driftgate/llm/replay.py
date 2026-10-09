"""Record/replay fixtures. Pure stdlib: replay never imports the SDK and needs no API key.

Layout: <fixture_dir>/<key>.json where key = sha256 of the canonical JSON of the normalized request.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from driftgate.llm.errors import FixtureMissing, FixtureStale
from driftgate.llm.types import ModelRequest, ModelResponse, ToolCall, Usage

FIXTURE_VERSION = 1


def normalize_request(request: ModelRequest, model: str) -> dict[str, Any]:
    """Everything that determines the model's answer, with the resolved model name. No cache markers."""
    return {
        "model": model,
        "max_tokens": request.max_tokens,
        "system": request.system,
        "tools": request.tools,
        "messages": request.messages,
    }


def request_key(request: ModelRequest, model: str) -> str:
    canonical = json.dumps(normalize_request(request, model), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def response_to_dict(response: ModelResponse) -> dict[str, Any]:
    return {
        "text": response.text,
        "tool_calls": [asdict(t) for t in response.tool_calls],
        "usage": asdict(response.usage),
        "stop_reason": response.stop_reason,
    }


def response_from_dict(data: dict[str, Any]) -> ModelResponse:
    return ModelResponse(
        text=data.get("text", ""),
        tool_calls=[ToolCall(id=t["id"], name=t["name"], input=t.get("input", {})) for t in data.get("tool_calls", [])],
        usage=Usage(**data.get("usage", {})),
        stop_reason=data.get("stop_reason", "end_turn"),
    )


class FixtureStore:
    """`fingerprint` is the current prompt/schema fingerprint (see agents/fingerprint.py). When set, saved fixtures
    carry it and a load of a fixture recorded under another one raises FixtureStale. When None, nothing is checked."""

    def __init__(self, directory: Path, fingerprint: str | None = None) -> None:
        self._dir = Path(directory)
        self.fingerprint = fingerprint

    def path_for(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def load(self, key: str) -> ModelResponse:
        path = self.path_for(key)
        if not path.is_file():
            raise FixtureMissing(key, str(path))
        data = json.loads(path.read_text(encoding="utf-8"))
        recorded = data.get("fingerprint")
        # A fixture with no fingerprint (hand-made or pre-fingerprint) is not judged: its key already pins the
        # request. Only a fixture recorded under a DIFFERENT fingerprint is stale.
        if self.fingerprint is not None and recorded is not None and recorded != self.fingerprint:
            raise FixtureStale(key, str(path), recorded, self.fingerprint)
        return response_from_dict(data["response"])

    def save(self, key: str, request: ModelRequest, model: str, response: ModelResponse) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(key)
        doc = {
            "fixture_version": FIXTURE_VERSION,
            "key": key,
            "request": normalize_request(request, model),
            "response": response_to_dict(response),
        }
        if self.fingerprint is not None:
            doc["fingerprint"] = self.fingerprint
        path.write_text(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        return path


# -- manifest: which scenarios were recorded, under which fingerprint --------------------------------------------------
MANIFEST_NAME = "manifest.json"
FRESH, STALE, MISSING = "fresh", "stale", "missing"


def load_manifest(directory: Path) -> dict[str, Any]:
    path = Path(directory) / MANIFEST_NAME
    if not path.is_file():
        return {"scenarios": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest_entry(directory: Path, scenario_id: str, entry: dict[str, Any]) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(directory)
    manifest.setdefault("scenarios", {})[scenario_id] = entry
    path = directory / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def scenario_status(directory: Path, scenario_id: str, fingerprint: str) -> str:
    """fresh: recorded under the current fingerprint; stale: recorded under another; missing: never recorded."""
    entry = load_manifest(directory).get("scenarios", {}).get(scenario_id)
    if entry is None:
        return MISSING
    return FRESH if entry.get("fingerprint") == fingerprint else STALE
