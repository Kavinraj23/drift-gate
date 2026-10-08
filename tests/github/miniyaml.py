"""A tiny parser for the YAML subset the playground workflows use.

PyYAML is not an allowed dependency (CLAUDE.md), so this reads block mappings, block sequences, `|` literal blocks,
full-line comments and plain/quoted scalars. Anything else (tabs, flow collections, anchors, bad indentation) raises
`YamlError`, so a workflow that leaves the subset fails the test instead of being silently misread.
"""

from __future__ import annotations

import re
from typing import Any

_KEY = re.compile(r"([A-Za-z0-9_.-]+):(?: (.*))?")


class YamlError(ValueError):
    pass


class _Parser:
    def __init__(self, text: str) -> None:
        if "\t" in text:
            raise YamlError("tabs are not allowed")
        self.lines = text.splitlines()
        self.pos = 0

    def _skip(self) -> None:
        while self.pos < len(self.lines):
            s = self.lines[self.pos].strip()
            if s and not s.startswith("#"):
                return
            self.pos += 1

    def _peek(self) -> tuple[int, str] | None:
        self._skip()
        if self.pos >= len(self.lines):
            return None
        line = self.lines[self.pos]
        return len(line) - len(line.lstrip(" ")), line.strip()

    def parse(self) -> Any:
        nxt = self._peek()
        if nxt is None:
            raise YamlError("empty document")
        value = self._node(nxt[0])
        if self._peek() is not None:
            raise YamlError(f"line {self.pos + 1}: unexpected content")
        return value

    def _node(self, indent: int) -> Any:
        nxt = self._peek()
        assert nxt is not None
        return self._seq(indent) if nxt[1].startswith("-") else self._map(indent)

    def _scalar(self, text: str) -> Any:
        if text[:1] in "[{&*!%@`" or text.startswith("- "):
            raise YamlError(f"unsupported scalar: {text[:30]!r}")
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
            return text[1:-1]
        if text in ("true", "false"):
            return text == "true"
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        return text

    def _map(self, indent: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while (nxt := self._peek()) is not None and nxt[0] == indent and not nxt[1].startswith("-"):
            m = _KEY.fullmatch(nxt[1])
            if not m:
                raise YamlError(f"line {self.pos + 1}: not a mapping entry: {nxt[1][:40]!r}")
            key, rest = m.group(1), (m.group(2) or "").strip()
            if key in out:
                raise YamlError(f"line {self.pos + 1}: duplicate key {key!r}")
            self.pos += 1
            if rest in ("|", "|-", "|+"):
                out[key] = self._literal(indent)
            elif rest:
                out[key] = self._scalar(rest)
            else:
                child = self._peek()
                if child is not None and (child[0] > indent or (child[0] == indent and child[1].startswith("-"))):
                    out[key] = self._node(child[0])
                else:
                    out[key] = None
        if nxt is not None and nxt[0] > indent:
            raise YamlError(f"line {self.pos + 1}: bad indentation")
        return out

    def _literal(self, parent_indent: int) -> str:
        block: list[str] = []
        base: int | None = None
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.strip():
                ind = len(line) - len(line.lstrip(" "))
                if ind <= parent_indent:
                    break
                base = ind if base is None else min(base, ind)
            block.append(line)
            self.pos += 1
        if base is None:
            return ""
        return "\n".join(line[base:] for line in block).rstrip("\n") + "\n"

    def _seq(self, indent: int) -> list[Any]:
        out: list[Any] = []
        while (nxt := self._peek()) is not None and nxt[0] == indent and nxt[1].startswith("-"):
            content = nxt[1][1:].strip()
            if not content:
                self.pos += 1
                child = self._peek()
                if child is None or child[0] <= indent:
                    raise YamlError(f"line {self.pos}: empty sequence item")
                out.append(self._node(child[0]))
            elif _KEY.fullmatch(content) and not content.startswith(('"', "'")):
                # "- key: value" opens a mapping whose keys line up two columns in from the dash
                self.lines[self.pos] = " " * (indent + 2) + content
                out.append(self._map(indent + 2))
            else:
                self.pos += 1
                out.append(self._scalar(content))
        return out


def load(text: str) -> Any:
    return _Parser(text).parse()
