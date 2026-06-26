"""Stateless technical indicator functions.

Single responsibility: transform a OHLCV DataFrame into indicator values.
All functions are pure — no state, no I/O, no side effects.
"""

import logging

import numpy as np
import pandas as pd
import pandas_ta as ta

log = logging.getLogger(__name__)

_REQUIRED_OHLCV = {"open", "high", "low", "close", "volume"}
_REQUIRED_CLOSE = {"close"}
_REQUIRED_HLC = {"high", "low", "close"}


def _validate(df: pd.DataFrame, required_cols: set[str], min_rows: int, fn_name: str) -> bool:
    """Return True if df passes validation. Log warning and return False otherwise."""
    if not isinstance(df.index, pd.DatetimeIndex):
        log.warning("%s: index must be DatetimeIndex, got %s", fn_name, type(df.index).__name__)
        return False
    missing = required_cols - set(df.columns)
    if missing:
        log.warning("%s: missing columns %s", fn_name, missing)
        return False
    if len(df) < min_rows:
        log.warning("%s: need >= %d rows, got %d", fn_name, min_rows, len(df))
        return False
    return True


def ema(df: pd.DataFrame, period: int) -> pd.Series | None:
    """Exponential moving average of close prices."""
    if not _validate(df, _REQUIRED_CLOSE, period, "ema"):
        return None
    return df["close"].ewm(span=period, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series | None:
    """Session VWAP — resets daily, only uses bars from the current date.

    Returns NaN for bars outside today's session to prevent prior-session bleed.
    """
    if not _validate(df, _REQUIRED_OHLCV, 1, "vwap"):
        return None
    last_date = df.index[-1].date()
    session = df[df.index.date == last_date].copy()
    if session.empty:
        log.warning("vwap: no bars for session date %s", last_date)
        return None
    typical = (session["high"] + session["low"] + session["close"]) / 3
    cum_tp_vol = (typical * session["volume"]).cumsum()
    cum_vol = session["volume"].cumsum()
    vwap_series = cum_tp_vol / cum_vol
    # Reindex to full df index; bars outside today's session get NaN (no ffill)
    return vwap_series.reindex(df.index)


def vwap_bands(df: pd.DataFrame, deviations: list[float]) -> dict[str, pd.Series] | None:
    """VWAP ± N standard-deviation bands.

    Returns dict keyed by "+1.0", "-1.0", "+2.0" etc.
    """
    if not _validate(df, _REQUIRED_OHLCV, 1, "vwap_bands"):
        return None
    last_date = df.index[-1].date()
    session = df[df.index.date == last_date].copy()
    if session.empty:
        log.warning("vwap_bands: no bars for session date %s", last_date)
        return None
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
        upper = (vw_mean + d * std).reindex(df.index).ffill()
        lower = (vw_mean - d * std).reindex(df.index).ffill()
        result[f"+{d}"] = upper
        result[f"-{d}"] = lower
    return result


def atr(df: pd.DataFrame, period: int = 14) -> float | None:
    """Average True Range — returns current scalar value."""
    # ATR needs period + 1 rows for a meaningful result
    if not _validate(df, _REQUIRED_HLC, period + 1, "atr"):
        return None
    result = ta.atr(df["high"], df["low"], df["close"], length=period)
    if result is None or result.dropna().empty:
        return None
    val = float(result.iloc[-1])
    if np.isnan(val):
        return None
    return val


def rsi(df: pd.DataFrame, period: int = 14) -> float | None:
    """RSI — returns current scalar value."""
    # RSI needs period + 1 rows minimum
    if not _validate(df, _REQUIRED_CLOSE, period + 1, "rsi"):
        return None
    result = ta.rsi(df["close"], length=period)
    if result is None or result.dropna().empty:
        return None
    val = float(result.iloc[-1])
    if np.isnan(val):
        return None
    return val


def macd(df: pd.DataFrame) -> tuple[float, float, float] | tuple[None, None, None]:
    """MACD — returns (macd_line, signal_line, histogram) as scalars."""
    # MACD default: fast=12, slow=26, signal=9 → need at least 26+9=35 rows
    if not _validate(df, _REQUIRED_CLOSE, 35, "macd"):
        return None, None, None
    result = ta.macd(df["close"])
    if result is None or result.dropna().empty:
        return None, None, None
    row = result.iloc[-1]
    cols = result.columns.tolist()
    macd_col = next((c for c in cols if c.startswith("MACD_")), None)
    hist_col = next((c for c in cols if c.startswith("MACDh_")), None)
    signal_col = next((c for c in cols if c.startswith("MACDs_")), None)
    if macd_col is None or hist_col is None or signal_col is None:
        return None, None, None
    mv, sv, hv = float(row[macd_col]), float(row[signal_col]), float(row[hist_col])
    if any(np.isnan(x) for x in (mv, sv, hv)):
        return None, None, None
    return mv, sv, hv


def rvol(df: pd.DataFrame, lookback: int = 20) -> float:
    """Relative volume: current bar volume vs average volume at this time of day.

    Approximated as last bar volume / mean of last `lookback` bar volumes.
    Always returns a float (defaults to 1.0 on degenerate input).
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

    Returns (orb_high, orb_low) or (None, None) if the ORB window has not
    yet closed, or if input is invalid. Uses vectorized boolean indexing.
    """
    if not _validate(df, {"high", "low"}, 1, "orb"):
        return None, None

    from datetime import time as dtime, timedelta, datetime as _dt
    import pytz

    try:
        et = pytz.timezone("America/New_York")
        if df.index.tz is None:
            df = df.copy()
            df.index = df.index.tz_localize("UTC")
        idx_et = df.index.tz_convert(et)
        last_date = idx_et[-1].date()

        open_time = dtime(9, 30)
        base = _dt(2000, 1, 1, 9, 30)
        close_time = (base + timedelta(minutes=window_minutes)).time()

        # Check whether the ORB window has actually closed for the last bar
        last_time_et = idx_et[-1].time()
        if last_time_et < close_time:
            return None, None

        # Vectorized filter: same date and within [9:30, 9:30+window).
        # Convert index to minute-of-day integers to avoid a Python-level loop.
        same_day = idx_et.date == last_date
        open_min = open_time.hour * 60 + open_time.minute          # 570
        close_min = close_time.hour * 60 + close_time.minute       # 570 + window
        idx_minutes = idx_et.hour * 60 + idx_et.minute             # vectorized numpy op
        in_window = (idx_minutes >= open_min) & (idx_minutes < close_min)
        mask = same_day & in_window

        orb_df = df[mask]
        if orb_df.empty:
            return None, None

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


def detect_swing_high(df: pd.DataFrame, lookback: int = 20) -> float | None:
    """Highest high in the last `lookback` bars."""
    if not _validate(df, {"high"}, 1, "detect_swing_high"):
        return None
    recent = df["high"].iloc[-lookback:] if len(df) >= lookback else df["high"]
    return float(recent.max())


def detect_swing_low(df: pd.DataFrame, lookback: int = 20) -> float | None:
    """Lowest low in the last `lookback` bars."""
    if not _validate(df, {"low"}, 1, "detect_swing_low"):
        return None
    recent = df["low"].iloc[-lookback:] if len(df) >= lookback else df["low"]
    return float(recent.min())
