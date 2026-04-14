"""Smoke test: full crypto trading pipeline without live API calls.

Uses real BarStore, SignalEngine, RegimeStore, OrderManager, Portfolio, and
risk gate. Only the broker is mocked. Verifies that synthetic BTC/USD ticks
can drive an order through the entire pipeline.
"""

import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from execution.executor import Executor
from signals.bar_store import BarStore
from signals.engine import SignalEngine
from signals.scoring import RegimeState
from regime.regime_store import RegimeStore
from core.portfolio import Portfolio
from core.order_manager import OrderManager
from risk.gate import check as gate_check

TICKER = "BTC/USD"


# ---------------------------------------------------------------------------
# Config fixture
# ---------------------------------------------------------------------------

def make_config() -> SimpleNamespace:
    trending = SimpleNamespace(
        stop_atr_mult=1.5,
        target_atr_mult=3.0,
        size_multiplier_by_conviction={1: 0.25, 2: 0.5, 3: 0.75, 4: 1.0, 5: 1.25},
    )
    ranging = SimpleNamespace(
        stop_atr_mult=1.0,
        target_atr_mult=1.5,
        size_multiplier_by_conviction={1: 0.0, 2: 0.25, 3: 0.5, 4: 0.75, 5: 1.0},
    )
    return SimpleNamespace(
        signal=SimpleNamespace(
            ema_fast=9, ema_slow=21, atr_period=14, rsi_period=14,
            vwap_deviation_bands=[1.0, 2.0, 2.5],
            orb_window_minutes=15,
            entry_threshold=0.55,
            min_bars=30,
            confidence_threshold=0.6,
        ),
        regime=SimpleNamespace(min_conviction_to_trade=3),
        risk=SimpleNamespace(
            max_portfolio_heat_pct=0.06,
            max_trade_risk_pct=0.01,
            max_position_pct=0.10,
            max_sector_positions=4,
            daily_loss_limit_pct=0.03,
        ),
        account=SimpleNamespace(nav=100_000),
        rr_profiles={"trending": trending, "ranging": ranging},
        execution=SimpleNamespace(
            order_retry_sleep_seconds=0.5,
            latency_warn_seconds=0.1,
            min_trail_increment_atr_fraction=0.1,
        ),
        llm=SimpleNamespace(stale_regime_minutes=120),
    )


# ---------------------------------------------------------------------------
# Bar factories
# ---------------------------------------------------------------------------

def make_uptrend_bars(n: int = 100) -> pd.DataFrame:
    """100 1-min bars: flat for 30, then strong uptrend for 70.

    Designed to guarantee:
      EMA(9) > EMA(21), price > VWAP, RSI 40-70, MACD > signal, RVOL > 1.5
    """
    rng = np.random.default_rng(99)
    closes = []
    base = 83_000.0
    for i in range(n):
        if i < 30:
            closes.append(base + rng.normal(0, 20))
        else:
            closes.append(base + (i - 30) * 150 + rng.normal(0, 10))

    close = np.array(closes)
    high = close + rng.uniform(5, 30, n)
    low = close - rng.uniform(5, 30, n)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    # Volume: spike on last 5 bars → RVOL > 1.5
    volume = rng.uniform(0.5, 2.0, n)
    volume[-5:] *= 4.0

    now = datetime.now(tz=timezone.utc)
    idx = pd.date_range(now - timedelta(minutes=n), periods=n, freq="1min")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def make_flat_bars(n: int = 100) -> pd.DataFrame:
    """100 bars of sideways noise — score stays below entry_threshold."""
    rng = np.random.default_rng(7)
    base = 83_000.0
    close = base + rng.normal(0, 50, n)
    high = close + rng.uniform(5, 20, n)
    low = close - rng.uniform(5, 20, n)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    volume = rng.uniform(0.5, 1.2, n)  # Low RVOL — no volume confirmation

    now = datetime.now(tz=timezone.utc)
    idx = pd.date_range(now - timedelta(minutes=n), periods=n, freq="1min")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def make_executor(bars: pd.DataFrame, regime: RegimeState):
    config = make_config()

    bar_store = BarStore()
    bar_store.backfill(TICKER, bars)

    regime_store = RegimeStore()
    regime_store.set(TICKER, regime)

    signal_engine = SignalEngine(config, bar_store, regime_store)
    order_manager = OrderManager(config)
    portfolio = Portfolio(nav=100_000)

    mock_order = MagicMock()
    mock_order.symbol = TICKER
    mock_order.qty = 1
    mock_order.side = "buy"
    mock_order.filled_avg_price = float(bars["close"].iloc[-1])

    broker = MagicMock()
    broker.is_tradable = AsyncMock(return_value=True)
    broker.submit_bracket_order = AsyncMock(return_value=mock_order)

    executor = Executor(broker, portfolio, signal_engine, order_manager, gate_check, config)
    return executor, broker


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crypto_fires_order_on_strong_uptrend():
    """Uptrend bars + trending bullish regime → bracket order submitted for BTC/USD."""
    bars = make_uptrend_bars()
    regime = RegimeState(
        regime="trending", direction="bullish", conviction=4, catalyst="smoke test"
    )
    executor, broker = make_executor(bars, regime)

    tick_price = float(bars["close"].iloc[-1]) + 200.0
    await executor.on_tick(TICKER, tick_price, 3.0, datetime.now(tz=timezone.utc))

    broker.submit_bracket_order.assert_called_once()
    ticker_arg, qty_arg, direction_arg, stop_arg, target_arg = (
        broker.submit_bracket_order.call_args.args
    )
    assert ticker_arg == TICKER
    assert direction_arg == "long"
    assert stop_arg < tick_price < target_arg, (
        f"Bracket invariant violated: stop={stop_arg:.2f} entry={tick_price:.2f} target={target_arg:.2f}"
    )


@pytest.mark.asyncio
async def test_crypto_no_order_on_flat_neutral_market():
    """Flat bars + ranging neutral regime → score below 0.55, no order fired."""
    bars = make_flat_bars()
    regime = RegimeState(
        regime="ranging", direction="neutral", conviction=3, catalyst="smoke test"
    )
    executor, broker = make_executor(bars, regime)

    tick_price = float(bars["close"].iloc[-1])
    await executor.on_tick(TICKER, tick_price, 1.0, datetime.now(tz=timezone.utc))

    broker.submit_bracket_order.assert_not_called()


@pytest.mark.asyncio
async def test_crypto_no_order_on_avoid_regime():
    """Avoid regime → signal engine short-circuits, no order regardless of bars."""
    bars = make_uptrend_bars()
    regime = RegimeState(
        regime="avoid", direction="bearish", conviction=5, catalyst="smoke test"
    )
    executor, broker = make_executor(bars, regime)

    tick_price = float(bars["close"].iloc[-1]) + 200.0
    await executor.on_tick(TICKER, tick_price, 3.0, datetime.now(tz=timezone.utc))

    broker.submit_bracket_order.assert_not_called()


@pytest.mark.asyncio
async def test_crypto_no_order_when_not_tradable():
    """Cache marks ticker not tradable → pipeline exits before signal engine."""
    bars = make_uptrend_bars()
    regime = RegimeState(
        regime="trending", direction="bullish", conviction=4, catalyst="smoke test"
    )
    executor, broker = make_executor(bars, regime)
    # Pre-populate cache — the hot path does an O(1) dict lookup, not a broker call.
    executor._asset_tradable_cache[TICKER] = False

    tick_price = float(bars["close"].iloc[-1]) + 200.0
    await executor.on_tick(TICKER, tick_price, 3.0, datetime.now(tz=timezone.utc))

    broker.submit_bracket_order.assert_not_called()
