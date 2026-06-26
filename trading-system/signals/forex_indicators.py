"""Forex-specific technical indicators.

Single responsibility: stateless indicator functions for forex pairs.
Pivot points replace ORB. ADX measures trend strength.
"""

import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_ta as ta

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# London session: 3am–12pm ET; New York session: 8am–5pm ET
_LONDON_START = dtime(3, 0)
_LONDON_END = dtime(12, 0)
_NY_START = dtime(8, 0)
_NY_END = dtime(17, 0)


def pivot_points(prev_high: float, prev_low: float, prev_close: float) -> dict[str, float]:
    """Compute standard daily pivot points from previous session OHLC.

    Returns dict with keys: pivot, r1, r2, s1, s2.
    """
    pivot = (prev_high + prev_low + prev_close) / 3.0
    r1 = 2 * pivot - prev_low
    s1 = 2 * pivot - prev_high
    r2 = pivot + (prev_high - prev_low)
    s2 = pivot - (prev_high - prev_low)
    return {"pivot": pivot, "r1": r1, "r2": r2, "s1": s1, "s2": s2}


def adx(df: pd.DataFrame, period: int = 14) -> float | None:
    """Return the current ADX value. Values > 25 indicate a trending market."""
    if len(df) < period * 2:
        return None
    try:
        result = ta.adx(df["high"], df["low"], df["close"], length=period)
        if result is None or result.empty:
            return None
        col = f"ADX_{period}"
        if col not in result.columns:
            col = result.columns[0]
        val = float(result[col].iloc[-1])
        return val if not pd.isna(val) else None
    except Exception as e:
        log.debug("ADX computation failed: %s", e)
        return None


def is_active_session(pair: str) -> bool:
    """Return True if current ET time is within a liquid trading session for the pair.

    USD pairs (EUR/USD, GBP/USD, USD/JPY, AUD/USD): trade London or NY session.
    Other pairs: always return True (conservative — don't accidentally block them).
    """
    usd_pairs = {"EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD"}
    if pair not in usd_pairs:
        return True
    now_et = datetime.now(tz=_ET).time()
    in_london = _LONDON_START <= now_et < _LONDON_END
    in_ny = _NY_START <= now_et < _NY_END
    return in_london or in_ny


def pips(pair: str, price_distance: float) -> float:
    """Convert a price distance to pips for the given pair.

    JPY pairs: 1 pip = 0.01. All others: 1 pip = 0.0001.
    """
    if "JPY" in pair:
        return price_distance / 0.01
    return price_distance / 0.0001
