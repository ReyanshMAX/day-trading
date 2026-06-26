"""Persistent trade journal backed by SQLite.

Single responsibility: record trade entries and exits with full context
for post-session analysis and strategy tuning.
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL,
    exit_price REAL,
    qty REAL NOT NULL,
    entry_time TEXT,
    exit_time TEXT,
    regime TEXT,
    conviction INTEGER,
    signal_score REAL,
    stop REAL,
    target REAL,
    pnl REAL,
    pnl_pct REAL,
    exit_reason TEXT
);
"""


class TradeJournal:
    """Async SQLite-backed trade journal."""

    def __init__(self, db_path: str = "logs/trades.db") -> None:
        self._db_path = db_path
        self._db = None

    async def open(self) -> None:
        """Open (or create) the database and apply schema."""
        try:
            import aiosqlite
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(self._db_path)
            await self._db.execute(_SCHEMA)
            await self._db.commit()
            log.info("Trade journal opened: %s", self._db_path)
        except ImportError:
            log.warning("aiosqlite not installed — trade journal disabled")
        except Exception as e:
            log.error("Failed to open trade journal: %s", e)

    async def record_entry(
        self,
        ticker: str,
        side: str,
        qty: float,
        entry_price: float,
        stop: float,
        target: float,
        regime: str,
        conviction: int,
        signal_score: float,
    ) -> int | None:
        """Insert a new trade entry row. Returns the row id or None on failure."""
        if self._db is None:
            return None
        try:
            entry_time = datetime.now(timezone.utc).isoformat()
            cursor = await self._db.execute(
                """INSERT INTO trades
                   (ticker, side, qty, entry_price, stop, target,
                    regime, conviction, signal_score, entry_time)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ticker, side, qty, entry_price, stop, target,
                 regime, conviction, signal_score, entry_time),
            )
            await self._db.commit()
            trade_id = cursor.lastrowid
            log.debug("Trade journal entry: id=%d %s %s qty=%.4f entry=%.4f",
                      trade_id, ticker, side, qty, entry_price)
            return trade_id
        except Exception as e:
            log.error("Failed to record trade entry for %s: %s", ticker, e)
            return None

    async def record_exit(
        self,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
        pnl: float | None = None,
        pnl_pct: float | None = None,
    ) -> None:
        """Update an existing trade row with exit data."""
        if self._db is None or trade_id is None:
            return
        try:
            exit_time = datetime.now(timezone.utc).isoformat()
            await self._db.execute(
                """UPDATE trades
                   SET exit_price=?, exit_time=?, exit_reason=?, pnl=?, pnl_pct=?
                   WHERE id=?""",
                (exit_price, exit_time, exit_reason, pnl, pnl_pct, trade_id),
            )
            await self._db.commit()
            log.debug("Trade journal exit: id=%d exit=%.4f reason=%s pnl_pct=%s",
                      trade_id, exit_price, exit_reason, pnl_pct)
        except Exception as e:
            log.error("Failed to record trade exit for id=%s: %s", trade_id, e)

    async def daily_summary(self) -> dict:
        """Return today's trade summary: count, wins, losses, total_pnl."""
        if self._db is None:
            return {}
        try:
            today = datetime.now(timezone.utc).date().isoformat()
            cursor = await self._db.execute(
                """SELECT COUNT(*), SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
                          SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END), SUM(pnl)
                   FROM trades
                   WHERE entry_time LIKE ? AND exit_price IS NOT NULL""",
                (f"{today}%",),
            )
            row = await cursor.fetchone()
            if row is None:
                return {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
            return {
                "trades": row[0] or 0,
                "wins": row[1] or 0,
                "losses": row[2] or 0,
                "total_pnl": row[3] or 0.0,
            }
        except Exception as e:
            log.error("Failed to compute daily summary: %s", e)
            return {}

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None
