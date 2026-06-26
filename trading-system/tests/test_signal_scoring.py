"""Unit tests for signals/scoring.py.

All offline, deterministic.
"""

import pytest
from signals.scoring import compute_score, IndicatorSnapshot, RegimeState


def bullish_trending_snapshot() -> IndicatorSnapshot:
    return IndicatorSnapshot(
        ema_fast=105.0,
        ema_slow=100.0,
        vwap=100.0,
        current_price=107.0,
        rsi=58.0,
        macd_line=0.5,
        macd_signal=0.2,
        rvol=2.0,
        orb_high=103.0,
        orb_low=98.0,
        atr=1.5,
        vwap_std=1.0,
    )


def neutral_snapshot() -> IndicatorSnapshot:
    return IndicatorSnapshot(
        ema_fast=100.0,
        ema_slow=100.0,
        vwap=100.0,
        current_price=100.0,
        rsi=50.0,
        macd_line=0.0,
        macd_signal=0.0,
        rvol=1.0,
        orb_high=100.0,
        orb_low=99.0,
        atr=1.0,
        vwap_std=1.0,
    )


def test_perfect_trending_long_score_above_threshold():
    snap = bullish_trending_snapshot()
    regime = RegimeState(regime="trending", conviction=4, direction="bullish")
    score = compute_score(snap, regime)
    assert score is not None
    assert score > 0.8


def test_ranging_oversold_score_above_threshold():
    # Price has re-entered from below the -2.0 band: prev bar was below, current is above.
    # vwap=100, std=1.0 → -2.0 band = 98.0; current_price=98.5 is above it.
    snap = IndicatorSnapshot(
        ema_fast=100.0,
        ema_slow=101.0,
        vwap=100.0,
        current_price=98.5,  # above -2.0 band (98.0), having re-entered from below
        rsi=30.0,
        macd_line=0.0,
        macd_signal=0.1,
        rvol=1.5,
        orb_high=102.0,
        orb_low=98.6,  # near support
        atr=1.0,
        vwap_std=1.0,
        prev_close_below_lower_band=True,   # prev bar was below -2.0 band
        current_close_above_lower_band=True,  # current bar re-entered above -2.0 band
    )
    regime = RegimeState(regime="ranging", conviction=3, direction="bullish")
    score = compute_score(snap, regime)
    assert score is not None
    assert score > 0.5


def test_neutral_indicators_near_zero():
    snap = neutral_snapshot()
    regime = RegimeState(regime="trending", conviction=3, direction="neutral")
    score = compute_score(snap, regime)
    assert score is not None
    assert abs(score) < 0.5


def test_avoid_regime_returns_none():
    snap = bullish_trending_snapshot()
    regime = RegimeState(regime="avoid", conviction=1, direction="neutral")
    result = compute_score(snap, regime)
    assert result is None


def test_no_orb_still_returns_valid_score():
    snap = bullish_trending_snapshot()
    snap.orb_high = None
    snap.orb_low = None
    regime = RegimeState(regime="trending", conviction=4, direction="bullish")
    score = compute_score(snap, regime)
    assert score is not None
    assert -1.0 <= score <= 1.0


def test_ranging_bearish_uses_short_bias_conditions():
    """Bearish ranging regime should score short-bias (overbought) conditions, not negate long."""
    # Price has re-entered from above the +2.0 band: prev bar was above, current is below.
    # vwap=100, std=1.0 → +2.0 band = 102.0; current_price=101.6 is below it.
    snap = IndicatorSnapshot(
        ema_fast=100.0,
        ema_slow=101.0,
        vwap=100.0,
        current_price=101.6,  # below +2.0 band (102.0), having re-entered from above
        rsi=70.0,             # overbought
        macd_line=0.0,
        macd_signal=0.0,
        rvol=1.5,
        orb_high=101.5,       # near resistance (within 0.5%)
        orb_low=98.0,
        atr=1.0,
        vwap_std=1.0,
        prev_close_above_upper_band=True,    # prev bar was above +2.0 band
        current_close_below_upper_band=True,  # current bar re-entered below +2.0 band
    )
    regime = RegimeState(regime="ranging", conviction=3, direction="bearish")
    score = compute_score(snap, regime)
    assert score is not None
    # Bearish ranging with overbought conditions → strong negative score
    assert score < -0.5


def test_perfect_trending_short_score_below_threshold():
    """Bearish trending regime with all bearish indicators should produce a strong negative score."""
    snap = IndicatorSnapshot(
        ema_fast=95.0,    # below ema_slow — bearish EMA stack
        ema_slow=100.0,
        vwap=100.0,
        current_price=93.0,  # below VWAP and below orb_low
        rsi=28.0,            # oversold from selling pressure — strong downtrend
        macd_line=-0.5,      # bearish MACD crossover
        macd_signal=-0.2,
        rvol=2.0,            # elevated volume confirms move
        orb_high=103.0,
        orb_low=98.0,        # price (93) is well below ORB low
        atr=1.5,
        vwap_std=1.0,
    )
    regime = RegimeState(regime="trending", conviction=4, direction="bearish")
    score = compute_score(snap, regime)
    assert score is not None
    assert score < -0.8


def test_ranging_bearish_oversold_not_high_score():
    """Oversold conditions should NOT produce a strong bearish ranging score."""
    # Oversold setup — good for long ranging, NOT for bearish ranging
    snap = IndicatorSnapshot(
        ema_fast=100.0,
        ema_slow=101.0,
        vwap=100.0,
        current_price=98.5,  # below VWAP lower band
        rsi=30.0,            # oversold — should NOT score high in bearish mode
        macd_line=0.0,
        macd_signal=0.0,
        rvol=1.5,
        orb_high=102.0,
        orb_low=98.6,
        atr=1.0,
        vwap_std=1.0,
    )
    regime = RegimeState(regime="ranging", conviction=3, direction="bearish")
    score = compute_score(snap, regime)
    # Hard gate: RSI=30 is not overbought (need RSI > 60 for ranging short) → None
    assert score is None
