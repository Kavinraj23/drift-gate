"""Persistent SQLite spend ledger. One connection per operation, so it survives restarts and threads."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def utc_day(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).strftime("%Y-%m-%d")


class SpendLedger:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS spend ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, day TEXT NOT NULL, "
                "model TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, "
                "cache_read_tokens INTEGER NOT NULL, cache_creation_tokens INTEGER NOT NULL, "
                "cost_usd REAL NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=10)

    def record(
        self,
        ts: float,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_creation_tokens: int,
        cost_usd: float,
    ) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO spend (ts, day, model, input_tokens, output_tokens, cache_read_tokens, "
                "cache_creation_tokens, cost_usd) VALUES (?,?,?,?,?,?,?,?)",
                (
                    ts,
                    utc_day(ts),
                    model,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_creation_tokens,
                    cost_usd,
                ),
            )

    def lifetime_spend(self) -> float:
        with closing(self._connect()) as db:
            return float(db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM spend").fetchone()[0])

    def daily_spend(self, day: str) -> float:
        with closing(self._connect()) as db:
            row = db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM spend WHERE day = ?", (day,)).fetchone()
            return float(row[0])
