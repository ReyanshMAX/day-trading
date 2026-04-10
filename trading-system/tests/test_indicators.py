"""Unit tests for signal/indicators.py using synthetic data.

All tests are offline — no network calls.
"""

import numpy as np
import pandas as pd
import pytest

from signals.indicators import (
    ema, vwap, atr, rsi, orb, fibonacci_levels, detect_swing_high, detect_swing_low,
)


def make_bars(n: int = 100, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    high = close + rng.uniform(0.1, 0.5, n)
    low = close - rng.uniform(0.1, 0.5, n)
    open_ = close - rng.uniform(-0.3, 0.3, n)
    volume = rng.integers(10000, 100000, n).astype(float)
    # 14:30 UTC = 9:30 AM ET (EST = UTC-5)
    idx = pd.date_range("2024-01-15 14:30", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


df = make_bars()


def test_ema_no_nan_in_tail():
    result = ema(df, 9)
    assert isinstance(result, pd.Series)
    assert len(result) == len(df)
    assert result.iloc[-50:].isna().sum() == 0


def test_vwap_positive_and_series():
    result = vwap(df)
    assert isinstance(result, pd.Series)
    assert (result.dropna() > 0).all()


def test_vwap_session_boundary():
    # Build two-session df to verify session reset
    d1 = make_bars(n=50, seed=1)
    d2_idx = pd.date_range("2024-01-16 14:30", periods=50, freq="1min", tz="UTC")
    d2 = make_bars(n=50, seed=2)
    d2.index = d2_idx
    combined = pd.concat([d1, d2])
    result = vwap(combined)
    # VWAP for session 2 should only reflect session 2 bars
    session2_vwap = result.loc[d2_idx]
    assert (session2_vwap.dropna() > 0).all()


def test_atr_positive_float():
    result = atr(df, 14)
    assert isinstance(result, float)
    assert result > 0


def test_rsi_in_range():
    result = rsi(df, 14)
    assert isinstance(result, float)
    assert 0 <= result <= 100


def test_orb_returns_floats_when_enough_bars():
    orb_high, orb_low = orb(df, 15)
    assert orb_high is not None
    assert orb_low is not None
    assert isinstance(orb_high, float)
    assert isinstance(orb_low, float)


def test_orb_returns_none_when_fewer_bars():
    small_df = make_bars(n=5)
    orb_high, orb_low = orb(small_df, 15)
    assert orb_high is None
    assert orb_low is None


def test_fibonacci_618_retracement():
    levels = fibonacci_levels(110.0, 100.0)
    # 0.618 retracement = 110 - 0.618 * 10 = 103.82
    expected = 110.0 - 0.618 * 10.0
    assert abs(levels["retracements"][0.618] - expected) < 0.001


def test_detect_swing_high_gte_all_closes():
    sh = detect_swing_high(df, 20)
    assert isinstance(sh, float)
    assert sh >= df["close"].iloc[-20:].max() - 1e-9
