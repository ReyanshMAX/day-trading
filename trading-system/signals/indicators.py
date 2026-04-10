"""Stateless technical indicator functions.

Single responsibility: transform a OHLCV DataFrame into indicator values.
All functions are pure — no state, no I/O, no side effects.
"""

import logging

import numpy as np
import pandas as pd
import pandas_ta as ta

log = logging.getLogger(__name__)


def ema(df: pd.DataFrame, period: int) -> pd.Series:
    """Exponential moving average of close prices."""
    return df["close"].ewm(span=period, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP — resets daily, only uses bars from the current date."""
    last_date = df.index[-1].date()
    session = df[df.index.date == last_date].copy()
    typical = (session["high"] + session["low"] + session["close"]) / 3
    cum_tp_vol = (typical * session["volume"]).cumsum()
    cum_vol = session["volume"].cumsum()
    vwap_series = cum_tp_vol / cum_vol
    # Reindex back to full df length, forward-filling session values
    return vwap_series.reindex(df.index, method="ffill")


def vwap_bands(df: pd.DataFrame, deviations: list[float]) -> dict[str, pd.Series]:
    """VWAP ± N standard-deviation bands.

    Returns dict keyed by "+1.0", "-1.0", "+2.0" etc.
    """
    last_date = df.index[-1].date()
    session = df[df.index.date == last_date].copy()
    typical = (session["high"] + session["low"] + session["close"]) / 3
    cum_tp_vol = (typical * session["volume"]).cumsum()
    cum_vol = session["volume"].cumsum()
    vwap_s = cum_tp_vol / cum_vol

    # Volume-weighted std dev
    vw_mean = vwap_s
    variance = ((typical - vw_mean) ** 2 * session["volume"]).cumsum() / cum_vol
    std = np.sqrt(variance)

    result: dict[str, pd.Series] = {}
    for d in deviations:
        upper = (vw_mean + d * std).reindex(df.index, method="ffill")
        lower = (vw_mean - d * std).reindex(df.index, method="ffill")
        result[f"+{d}"] = upper
        result[f"-{d}"] = lower
    return result


def atr(df: pd.DataFrame, period: int = 14) -> float:
    """Average True Range — returns current scalar value."""
    result = ta.atr(df["high"], df["low"], df["close"], length=period)
    if result is None or result.dropna().empty:
        return float("nan")
    return float(result.iloc[-1])


def rsi(df: pd.DataFrame, period: int = 14) -> float:
    """RSI — returns current scalar value."""
    result = ta.rsi(df["close"], length=period)
    if result is None or result.dropna().empty:
        return float("nan")
    return float(result.iloc[-1])


def macd(df: pd.DataFrame) -> tuple[float, float, float]:
    """MACD — returns (macd_line, signal_line, histogram) as scalars."""
    result = ta.macd(df["close"])
    if result is None or result.dropna().empty:
        return float("nan"), float("nan"), float("nan")
    row = result.iloc[-1]
    cols = result.columns.tolist()
    return float(row[cols[0]]), float(row[cols[1]]), float(row[cols[2]])


def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume."""
    result = ta.obv(df["close"], df["volume"])
    if result is None:
        return pd.Series(dtype=float)
    return result


def rvol(df: pd.DataFrame, lookback: int = 20) -> float:
    """Relative volume: current bar volume vs average volume at this time of day.

    Approximated as last bar volume / mean of last `lookback` bar volumes.
    """
    if len(df) < 2:
        return 1.0
    recent = df["volume"].iloc[-lookback:] if len(df) >= lookback else df["volume"]
    avg = recent.iloc[:-1].mean()
    if avg == 0:
        return 1.0
    return float(df["volume"].iloc[-1] / avg)


def orb(df: pd.DataFrame, window_minutes: int = 15) -> tuple[float | None, float | None]:
    """Opening Range Breakout levels from the first N minutes after 9:30 AM ET.

    Returns (orb_high, orb_low) or (None, None) if window not yet closed.
    """
    from datetime import time as dtime, timedelta, datetime as _dt
    import pytz

    try:
        et = pytz.timezone("America/New_York")
        idx = df.index
        last_date = idx[-1].astimezone(et).date()

        open_time = dtime(9, 30)
        base = _dt(2000, 1, 1, 9, 30)
        close_time = (base + timedelta(minutes=window_minutes)).time()

        orb_bars = []
        for ts, row in df.iterrows():
            ts_et = ts.astimezone(et)
            if ts_et.date() != last_date:
                continue
            t = ts_et.time()
            if open_time <= t < close_time:
                orb_bars.append(row)

        if len(orb_bars) < window_minutes:
            return None, None

        orb_df = pd.DataFrame(orb_bars)
        return float(orb_df["high"].max()), float(orb_df["low"].min())
    except Exception as e:
        log.debug("ORB computation skipped: %s", e)
        return None, None


def fibonacci_levels(swing_high: float, swing_low: float) -> dict:
    """Fibonacci retracement and extension levels from swing high/low."""
    r = swing_high - swing_low
    retracements = {
        0.236: swing_high - 0.236 * r,
        0.382: swing_high - 0.382 * r,
        0.5:   swing_high - 0.5 * r,
        0.618: swing_high - 0.618 * r,
        0.786: swing_high - 0.786 * r,
    }
    extensions = {
        1.272: swing_high + (1.272 - 1.0) * r,
        1.618: swing_high + (1.618 - 1.0) * r,
        2.618: swing_high + (2.618 - 1.0) * r,
    }
    return {"retracements": retracements, "extensions": extensions}


def detect_swing_high(df: pd.DataFrame, lookback: int = 20) -> float:
    """Highest high in the last `lookback` bars."""
    recent = df["high"].iloc[-lookback:] if len(df) >= lookback else df["high"]
    return float(recent.max())


def detect_swing_low(df: pd.DataFrame, lookback: int = 20) -> float:
    """Lowest low in the last `lookback` bars."""
    recent = df["low"].iloc[-lookback:] if len(df) >= lookback else df["low"]
    return float(recent.min())
