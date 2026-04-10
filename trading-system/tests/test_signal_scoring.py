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
    snap = IndicatorSnapshot(
        ema_fast=100.0,
        ema_slow=101.0,
        vwap=100.0,
        current_price=98.5,  # below vwap - 1*std (std=1.0 → lower=99.0)
        rsi=30.0,
        macd_line=0.0,
        macd_signal=0.1,
        rvol=1.5,
        orb_high=102.0,
        orb_low=98.6,  # near support
        atr=1.0,
        vwap_std=1.0,
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
