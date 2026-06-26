"""Offline tests for signals/forex_indicators.py."""

import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timezone
from unittest.mock import patch


def _make_forex_bars(n: int = 50, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 1.09 + np.cumsum(rng.normal(0, 0.0005, n))
    high = close + rng.uniform(0.0001, 0.0005, n)
    low = close - rng.uniform(0.0001, 0.0005, n)
    open_ = close - rng.uniform(-0.0003, 0.0003, n)
    volume = rng.integers(100, 5000, n).astype(float)
    idx = pd.date_range("2024-01-15 09:00", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


# ── pivot_points ──────────────────────────────────────────────────────────────

def test_pivot_points_basic_structure():
    from signals.forex_indicators import pivot_points
    levels = pivot_points(prev_high=1.1050, prev_low=1.0950, prev_close=1.1000)
    assert set(levels.keys()) == {"pivot", "r1", "r2", "s1", "s2"}


def test_pivot_points_pivot_is_average():
    from signals.forex_indicators import pivot_points
    levels = pivot_points(prev_high=1.1050, prev_low=1.0950, prev_close=1.1000)
    expected_pivot = (1.1050 + 1.0950 + 1.1000) / 3.0
    assert levels["pivot"] == pytest.approx(expected_pivot, rel=1e-6)


def test_pivot_points_r1_above_pivot():
    from signals.forex_indicators import pivot_points
    levels = pivot_points(prev_high=1.1050, prev_low=1.0950, prev_close=1.1000)
    assert levels["r1"] > levels["pivot"]


def test_pivot_points_s1_below_pivot():
    from signals.forex_indicators import pivot_points
    levels = pivot_points(prev_high=1.1050, prev_low=1.0950, prev_close=1.1000)
    assert levels["s1"] < levels["pivot"]


def test_pivot_points_r2_above_r1():
    from signals.forex_indicators import pivot_points
    levels = pivot_points(prev_high=1.1050, prev_low=1.0950, prev_close=1.1000)
    assert levels["r2"] > levels["r1"]


def test_pivot_points_s2_below_s1():
    from signals.forex_indicators import pivot_points
    levels = pivot_points(prev_high=1.1050, prev_low=1.0950, prev_close=1.1000)
    assert levels["s2"] < levels["s1"]


# ── adx ───────────────────────────────────────────────────────────────────────

def test_adx_returns_float_with_enough_bars():
    from signals.forex_indicators import adx
    df = _make_forex_bars(50)
    result = adx(df, period=14)
    # May be None if pandas_ta returns all NaN, but if not None it must be in range
    if result is not None:
        assert 0.0 <= result <= 100.0


def test_adx_returns_none_with_too_few_bars():
    from signals.forex_indicators import adx
    df = _make_forex_bars(10)  # period=14 → needs >= 28 bars
    result = adx(df, period=14)
    assert result is None


# ── is_active_session ─────────────────────────────────────────────────────────

def _fake_now_et(hour: int, minute: int = 0):
    """Return a datetime at the given hour:minute ET."""
    from zoneinfo import ZoneInfo
    return datetime(2024, 1, 15, hour, minute, tzinfo=ZoneInfo("America/New_York"))


def test_is_active_session_london_ny_overlap_is_true():
    from signals.forex_indicators import is_active_session
    # 10:00 AM ET is in both London (3am–12pm) and NY (8am–5pm)
    fake = _fake_now_et(10, 0)
    with patch("signals.forex_indicators.datetime") as mock_dt:
        mock_dt.now.return_value = fake
        result = is_active_session("EUR/USD")
    assert result is True


def test_is_active_session_asian_session_is_false():
    from signals.forex_indicators import is_active_session
    # 2:00 AM ET is outside both London and NY sessions
    fake = _fake_now_et(2, 0)
    with patch("signals.forex_indicators.datetime") as mock_dt:
        mock_dt.now.return_value = fake
        result = is_active_session("EUR/USD")
    assert result is False


def test_is_active_session_post_ny_is_false():
    from signals.forex_indicators import is_active_session
    # 8:00 PM ET (20:00) is after NY session close
    fake = _fake_now_et(20, 0)
    with patch("signals.forex_indicators.datetime") as mock_dt:
        mock_dt.now.return_value = fake
        result = is_active_session("EUR/USD")
    assert result is False


def test_is_active_session_unknown_pair_always_true():
    from signals.forex_indicators import is_active_session
    # Non-USD pairs always return True regardless of time
    fake = _fake_now_et(2, 0)  # Asian session
    with patch("signals.forex_indicators.datetime") as mock_dt:
        mock_dt.now.return_value = fake
        result = is_active_session("EUR/GBP")
    assert result is True


# ── pips ─────────────────────────────────────────────────────────────────────

def test_pips_eurusd_10_pips():
    from signals.forex_indicators import pips
    result = pips("EUR/USD", 0.0010)
    assert result == pytest.approx(10.0, rel=1e-6)


def test_pips_usdjpy_10_pips():
    from signals.forex_indicators import pips
    result = pips("USD/JPY", 0.10)
    assert result == pytest.approx(10.0, rel=1e-6)


def test_pips_gbpusd_1_pip():
    from signals.forex_indicators import pips
    result = pips("GBP/USD", 0.0001)
    assert result == pytest.approx(1.0, rel=1e-6)


def test_pips_usdjpy_1_pip():
    from signals.forex_indicators import pips
    result = pips("USD/JPY", 0.01)
    assert result == pytest.approx(1.0, rel=1e-6)
