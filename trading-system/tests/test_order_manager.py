"""Unit tests for core/order_manager.py.

Offline — no broker calls.
"""

import pytest
from core.order_manager import OrderManager, snap_to_fib, compute_base_size, BracketParams
from core.config import (
    Config, UniverseConfig, AccountConfig, RiskConfig, SignalConfig, RegimeConfig, RRProfile
)


def make_config() -> Config:
    return Config(
        universe=UniverseConfig(tickers=["NVDA"]),
        account=AccountConfig(paper=True, nav=100_000.0),
        risk=RiskConfig(
            max_trade_risk_pct=0.01,
            max_portfolio_heat_pct=0.06,
            max_position_pct=0.10,
            max_sector_positions=4,
            daily_loss_limit_pct=0.03,
        ),
        signal=SignalConfig(
            entry_threshold=0.55, atr_period=14, ema_fast=9, ema_slow=21, rsi_period=14,
            vwap_deviation_bands=[1.0, 2.0, 2.5], orb_window_minutes=15,
        ),
        regime=RegimeConfig(news_poll_interval_seconds=120, min_conviction_to_trade=3),
        rr_profiles={
            "trending": RRProfile(1.5, 3.0, {1: 0.25, 2: 0.5, 3: 0.75, 4: 1.0, 5: 1.25}),
            "ranging": RRProfile(1.0, 1.5, {1: 0.0, 2: 0.25, 3: 0.5, 4: 0.75, 5: 1.0}),
        },
        alpaca_api_key="test", alpaca_secret_key="test", groq_api_key="test",
    )


def test_long_bracket_invariant():
    om = OrderManager(make_config())
    params = om.build_bracket("NVDA", 0.8, "trending", 4, atr=2.0, current_price=910.0)
    assert params.stop < 910.0 < params.target


def test_short_bracket_invariant():
    om = OrderManager(make_config())
    params = om.build_bracket("NVDA", -0.8, "trending", 4, atr=2.0, current_price=910.0)
    assert params.target < 910.0 < params.stop


def test_fibonacci_snap_within_tolerance():
    fib_levels = [100.0, 105.0, 110.0]
    # 100.29 is within 0.3% of 100.0
    result = snap_to_fib(100.29, fib_levels, tolerance_pct=0.003)
    assert result == 100.0


def test_fibonacci_no_snap_far_from_levels():
    fib_levels = [100.0, 105.0, 110.0]
    # 102.0 is 2% away from 100.0 and 103.0 away from 105 — no snap
    result = snap_to_fib(102.0, fib_levels, tolerance_pct=0.003)
    assert result == 102.0


def test_wide_stop_returns_qty_one():
    # stop_distance = 1.5 * 70 = 105 → risk_dollars = 1000, qty = floor(1000/105) = 9, then *mult
    # to get qty=1, we need a very wide stop: e.g. atr=2000
    om = OrderManager(make_config())
    params = om.build_bracket("NVDA", 0.8, "trending", 1, atr=2000.0, current_price=910.0)
    assert params.qty == 1


def test_conviction_one_size_multiplier():
    om = OrderManager(make_config())
    # conviction=1 in trending → multiplier=0.25
    params1 = om.build_bracket("NVDA", 0.8, "trending", 1, atr=2.0, current_price=100.0)
    params4 = om.build_bracket("NVDA", 0.8, "trending", 4, atr=2.0, current_price=100.0)
    # conviction 4 should yield more shares than conviction 1
    assert params4.qty > params1.qty


def test_bracket_with_fib_levels_does_not_violate_invariant():
    from signals.indicators import fibonacci_levels
    fib = fibonacci_levels(950.0, 880.0)
    om = OrderManager(make_config())
    params = om.build_bracket("NVDA", 0.8, "trending", 4, atr=5.0, current_price=910.0, fib_levels=fib)
    assert params.stop < 910.0 < params.target
