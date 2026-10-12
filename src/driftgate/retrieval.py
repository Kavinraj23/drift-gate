"""Past-incident index for the optional `similar_history` tool: SQLite FTS5 with BM25 ranking (stdlib only).

What it holds: only fields the agent itself produced or could see for a finished incident (error signature,
fingerprint, key error excerpt, hypothesis, action taken, verification outcome) plus the incident's finish time.
It has no column for a label of any kind, and nothing here opens the labelled datasets (invariants 8 and 16).

No leakage (invariant 16): `query` requires the case's id and start time (`as_of`). An incident is returned only if
it finished strictly before `as_of` and is not the case itself, enforced inside the SQL statement, whatever the
query text says. Query text is user log text: it is reduced to quoted alphanumeric terms, so it can never be read
as FTS5 syntax (operators, column filters, NEAR, prefix, parentheses).

The index is written by the orchestrator after an incident finishes (`Orchestrator.record_incident`); the agent's
tool only reads. Nothing in this module imports a RemediationTarget implementation.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from driftgate.redaction import redact

MAX_RESULTS = 3
MAX_QUERY_TERMS = 32
MIN_TERM_LENGTH = 3
MAX_TERM_LENGTH = 40
MAX_FIELD_CHARS = 600  # per stored text field, after redaction; keeps the index and the tool output small
_TERM = re.compile(r"[^\W_]+")  # letters and digits only: no quote, colon, star, minus, caret or parenthesis


class FTS5Unavailable(RuntimeError):
    """This Python's SQLite was built without FTS5, so `similar_history` cannot run."""


@dataclass(frozen=True)
class IncidentRecord:
    """One finished incident, as the agent saw it. `finished_at` is epoch seconds (UTC)."""

    execution_id: str
    finished_at: float
    signature: str
    fingerprint: str
    excerpt: str
    hypothesis: str
    action: str
    outcome: str


@dataclass(frozen=True)
class Match:
    execution_id: str
    finished_at: str  # ISO 8601, UTC
    signature: str
    fingerprint: str
    excerpt: str
    hypothesis: str
    action: str
    outcome: str
    score: float  # bm25 as reported by SQLite: more negative is a better match

    def to_dict(self) -> dict[str, object]:
        return {
            "execution_id": self.execution_id,
            "finished_at": self.finished_at,
            "signature": self.signature,
            "fingerprint": self.fingerprint,
            "key_error_excerpt": self.excerpt,
            "hypothesis": self.hypothesis,
            "action_taken": self.action,
            "outcome": self.outcome,
            "bm25_score": round(self.score, 4),
        }


def fts5_available() -> bool:
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return True


def match_query(text: str, *, max_terms: int = MAX_QUERY_TERMS) -> str:
    """An FTS5 MATCH expression built from `text`: distinct quoted terms joined by OR; empty when nothing usable.

    Every term is wrapped in double quotes and contains only letters and digits, so it is always a plain string
    token, including the words AND, OR, NOT and NEAR. Terms keep their first-seen order, so callers put the most
    specific text first.
    """
    seen: dict[str, None] = {}
    for term in _TERM.findall(text.lower()):
        if MIN_TERM_LENGTH <= len(term) <= MAX_TERM_LENGTH:
            seen.setdefault(term)
        if len(seen) >= max_terms:
            break
    return " OR ".join(f'"{t}"' for t in seen)


def _clip(value: str) -> str:
    return redact(value)[:MAX_FIELD_CHARS]


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class HistoryIndex:
    """FTS5 index over finished incidents. `path` may be ":memory:" (tests and the synthetic eval)."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        if not fts5_available():
            raise FTS5Unavailable("SQLite FTS5 is not available in this Python build; similar_history cannot run")
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(path))
        self._con.executescript(
            """
            CREATE TABLE IF NOT EXISTS incident_meta (
                id INTEGER PRIMARY KEY,
                execution_id TEXT NOT NULL UNIQUE,
                finished_at REAL NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS incident_text USING fts5(
                signature, fingerprint, excerpt, hypothesis, action, outcome
            );
            """
        )

    def close(self) -> None:
        self._con.close()

    def __len__(self) -> int:
        return int(self._con.execute("SELECT COUNT(*) FROM incident_meta").fetchone()[0])

    def add(self, record: IncidentRecord) -> None:
        """Add (or replace) a finished incident. Text is redacted and clipped on the way in."""
        with self._con:
            row = self._con.execute(
                "SELECT id FROM incident_meta WHERE execution_id = ?", (record.execution_id,)
            ).fetchone()
            if row is not None:
                self._con.execute("DELETE FROM incident_text WHERE rowid = ?", (row[0],))
                self._con.execute("DELETE FROM incident_meta WHERE id = ?", (row[0],))
            cur = self._con.execute(
                "INSERT INTO incident_meta (execution_id, finished_at) VALUES (?, ?)",
                (record.execution_id, float(record.finished_at)),
            )
            self._con.execute(
                "INSERT INTO incident_text (rowid, signature, fingerprint, excerpt, hypothesis, action, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    cur.lastrowid,
                    _clip(record.signature),
                    _clip(record.fingerprint),
                    _clip(record.excerpt),
                    _clip(record.hypothesis),
                    _clip(record.action),
                    _clip(record.outcome),
                ),
            )

    def add_all(self, records: Iterable[IncidentRecord]) -> None:
        for r in records:
            self.add(r)

    def query(self, text: str, *, as_of: float, case_id: str, limit: int = MAX_RESULTS) -> list[Match]:
        """Top matches by BM25 among incidents that finished strictly before `as_of`, never the case itself."""
        expr = match_query(text)
        if not expr:
            return []
        rows = self._con.execute(
            """
            SELECT m.execution_id, m.finished_at, t.signature, t.fingerprint, t.excerpt, t.hypothesis,
                   t.action, t.outcome, bm25(incident_text) AS score
            FROM incident_text AS t JOIN incident_meta AS m ON m.id = t.rowid
            WHERE incident_text MATCH ? AND m.finished_at < ? AND m.execution_id <> ?
            ORDER BY score, m.finished_at DESC, m.execution_id
            LIMIT ?
            """,
            (expr, float(as_of), case_id, max(0, min(int(limit), MAX_RESULTS))),
        ).fetchall()
        return [Match(r[0], _iso(r[1]), r[2], r[3], r[4], r[5], r[6], r[7], r[8]) for r in rows]
