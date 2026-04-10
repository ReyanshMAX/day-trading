"""Rolling 1-minute OHLCV bar buffer per ticker.

Single responsibility: aggregate raw ticks into closed 1-min bars
and provide a DataFrame view for the signal engine.
"""

import logging
from collections import deque
from datetime import datetime, timezone

import pandas as pd

log = logging.getLogger(__name__)

Bar = dict  # keys: open, high, low, close, volume, timestamp


class BarStore:
    """Maintains a rolling buffer of up to 200 closed 1-min bars per ticker."""

    def __init__(self) -> None:
        self._bars: dict[str, deque[Bar]] = {}
        self._current: dict[str, Bar | None] = {}

    def _ensure_ticker(self, ticker: str) -> None:
        if ticker not in self._bars:
            self._bars[ticker] = deque(maxlen=200)
            self._current[ticker] = None

    def update(self, ticker: str, price: float, volume: float, timestamp: datetime) -> None:
        """Aggregate a tick into the current in-progress bar, closing it on minute boundary."""
        self._ensure_ticker(ticker)
        cur = self._current[ticker]

        if cur is None:
            self._current[ticker] = {
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
                "timestamp": timestamp,
            }
            return

        if timestamp.minute != cur["timestamp"].minute or timestamp.hour != cur["timestamp"].hour:
            # Minute rolled over — close the current bar
            self._bars[ticker].append(cur)
            log.debug("Bar closed for %s @ %s close=%.2f", ticker, cur["timestamp"], cur["close"])
            self._current[ticker] = {
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
                "timestamp": timestamp,
            }
        else:
            cur["high"] = max(cur["high"], price)
            cur["low"] = min(cur["low"], price)
            cur["close"] = price
            cur["volume"] += volume

    def get_bars(self, ticker: str, n: int) -> pd.DataFrame:
        """Return last n closed bars as a DataFrame with UTC DatetimeIndex."""
        self._ensure_ticker(ticker)
        bars = list(self._bars[ticker])
        bars = bars[-n:] if len(bars) >= n else bars
        if not bars:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame(bars)
        df = df.set_index("timestamp")
        df.index = pd.to_datetime(df.index, utc=True)
        return df

    def backfill(self, ticker: str, df: pd.DataFrame) -> None:
        """Load historical bars from broker.get_bars() into the deque."""
        self._ensure_ticker(ticker)
        for ts, row in df.iterrows():
            bar: Bar = {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "timestamp": ts,
            }
            self._bars[ticker].append(bar)
        log.debug("Backfilled %d bars for %s", len(df), ticker)

    def get_current_price(self, ticker: str) -> float | None:
        """Return the last seen price from the in-progress bar."""
        self._ensure_ticker(ticker)
        cur = self._current[ticker]
        return cur["close"] if cur is not None else None
