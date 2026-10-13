"""Helpers for the N5 trace report tests: the sample location and an HTML inspector."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

SAMPLE = Path(__file__).resolve().parents[2] / "fixtures" / "trace_sample"


class Inspector(HTMLParser):
    """Collects every tag, attribute and id of a page, so tests can assert on structure instead of substrings."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attrs: list[tuple[str, str, str]] = []  # (tag, name, value)
        self.ids: set[str] = set()
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        for name, value in attrs:
            self.attrs.append((tag, name, value or ""))
            if name == "id":
                self.ids.add(value or "")

    def handle_data(self, data: str) -> None:
        self.text.append(data)

    @property
    def hrefs(self) -> list[str]:
        return [v for t, n, v in self.attrs if n == "href"]


def inspect(page: str) -> Inspector:
    p = Inspector()
    p.feed(page)
    return p
