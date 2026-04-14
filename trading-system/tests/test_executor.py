"""Integration tests for execution/executor.py.

All dependencies mocked. Verifies order fires only in the correct path.
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from execution.executor import Executor
from signals.engine import SignalResult
from signals.scoring import IndicatorSnapshot
from risk.gate import GateResult
from core.portfolio import Portfolio, Position


def make_snapshot() -> IndicatorSnapshot:
    return IndicatorSnapshot(
        ema_fast=105.0, ema_slow=100.0, vwap=100.0, current_price=107.0,
        rsi=58.0, macd_line=0.5, macd_signal=0.2, rvol=2.0,
        orb_high=103.0, orb_low=98.0, atr=1.5, vwap_std=1.0,
    )


def make_signal() -> SignalResult:
    return SignalResult(
        ticker="NVDA", score=0.7, direction="long", atr=2.0,
        regime="trending", conviction=4, indicators=make_snapshot(),
        confidence=1.0,
    )


def _make_executor(
    signal_return=None,
    has_position=False,
    gate_approved=True,
    broker_raises=False,
    is_tradable=True,
):
    broker = MagicMock()
    broker.is_tradable = AsyncMock(return_value=is_tradable)
    broker.pop_crypto_stop_order_id = MagicMock(return_value=None)
    if broker_raises:
        broker.submit_bracket_order = AsyncMock(side_effect=RuntimeError("broker error"))
    else:
        mock_order = MagicMock()
        mock_order.symbol = "NVDA"
        mock_order.qty = 5
        mock_order.side = "buy"
        mock_order.filled_avg_price = 910.0
        broker.submit_bracket_order = AsyncMock(return_value=mock_order)

    portfolio = MagicMock(spec=Portfolio)
    portfolio.has_position.return_value = has_position
    portfolio.nav = 100_000.0
    portfolio.daily_pnl = 0.0
    portfolio.daily_loss_limit_hit = False
    portfolio.open_risk_pct.return_value = 0.0
    portfolio.sector_count.return_value = 0
    portfolio.record_fill = MagicMock()

    signal_engine = MagicMock()
    signal_engine.on_tick.return_value = signal_return
    bar_df = MagicMock()
    bar_df.__len__ = lambda s: 5  # < 20 bars → no fib
    signal_engine.get_bars.return_value = bar_df

    order_manager = MagicMock()
    bracket = MagicMock()
    bracket.qty = 5
    bracket.stop = 905.0
    bracket.target = 925.0
    bracket.stop_distance = 5.0
    order_manager.build_bracket.return_value = bracket

    # risk_gate is a plain callable returning GateResult
    risk_gate = MagicMock(return_value=GateResult(approved=gate_approved, reason=None if gate_approved else "test"))

    config = MagicMock()
    config.risk.max_trade_risk_pct = 0.01
    config.risk.max_portfolio_heat_pct = 0.06

    executor = Executor(broker, portfolio, signal_engine, order_manager, risk_gate, config)
    return executor, broker, portfolio, signal_engine, order_manager, risk_gate


@pytest.mark.asyncio
async def test_full_tick_to_order_path():
    executor, broker, portfolio, signal_engine, order_manager, risk_gate = _make_executor(
        signal_return=make_signal(),
        has_position=False,
        gate_approved=True,
    )
    await executor.on_tick("NVDA", 910.0, 50000, datetime.now(tz=timezone.utc))

    broker.submit_bracket_order.assert_called_once()
    risk_gate.assert_called_once()


@pytest.mark.asyncio
async def test_no_signal_broker_not_called():
    executor, broker, *_ = _make_executor(signal_return=None)
    await executor.on_tick("NVDA", 910.0, 50000, datetime.now(tz=timezone.utc))
    broker.submit_bracket_order.assert_not_called()


@pytest.mark.asyncio
async def test_existing_position_broker_not_called():
    executor, broker, *_ = _make_executor(
        signal_return=make_signal(), has_position=True
    )
    await executor.on_tick("NVDA", 910.0, 50000, datetime.now(tz=timezone.utc))
    broker.submit_bracket_order.assert_not_called()


@pytest.mark.asyncio
async def test_gate_reject_broker_not_called():
    executor, broker, *_ = _make_executor(
        signal_return=make_signal(), has_position=False, gate_approved=False
    )
    await executor.on_tick("NVDA", 910.0, 50000, datetime.now(tz=timezone.utc))
    broker.submit_bracket_order.assert_not_called()


@pytest.mark.asyncio
async def test_broker_exception_does_not_crash_executor():
    executor, broker, *_ = _make_executor(
        signal_return=make_signal(), has_position=False, broker_raises=True
    )
    # Should not raise
    await executor.on_tick("NVDA", 910.0, 50000, datetime.now(tz=timezone.utc))


@pytest.mark.asyncio
async def test_not_tradable_broker_not_called():
    executor, broker, *_ = _make_executor(
        signal_return=make_signal(), is_tradable=False
    )
    # Pre-populate the cache so the hot-path O(1) lookup returns False.
    executor._asset_tradable_cache["NVDA"] = False
    await executor.on_tick("NVDA", 910.0, 50000, datetime.now(tz=timezone.utc))
    broker.submit_bracket_order.assert_not_called()


@pytest.mark.asyncio
async def test_soft_target_never_moves_backward():
    """Soft target must ratchet in the correct direction only.

    For a long position:
      - tick at a high price computes a high candidate target
      - tick at a lower price must NOT lower current_soft_target
      - tick at an even higher price must advance it further

    The final current_soft_target must equal the max of all candidate
    targets ever computed (subject to the min-increment filter).
    """
    # Use a real Portfolio so positions dict is real (not mocked).
    portfolio = Portfolio(nav=100_000.0)

    # Inject a long position with a known ATR and known entry/target.
    # entry=900, target=910 (distance=10), atr=2.0, min_increment=0.2
    atr = 2.0
    entry = 900.0
    original_dist = 10.0  # target - avg_entry
    initial_target = entry + original_dist  # 910.0

    pos = Position(
        ticker="BTC/USD",
        qty=0.01,
        avg_entry=entry,
        stop=890.0,
        target=initial_target,
        side="long",
        atr=atr,
        current_soft_target=initial_target,
    )
    portfolio.positions["BTC/USD"] = pos

    broker = MagicMock()
    broker.is_tradable = AsyncMock(return_value=True)
    broker.pop_crypto_stop_order_id = MagicMock(return_value=None)
    # submit_market_order should NOT be called during these ticks (price never reaches target)
    broker.submit_market_order = AsyncMock()
    broker.submit_bracket_order = AsyncMock()

    signal_engine = MagicMock()
    signal_engine.on_tick.return_value = None  # no new signals
    bar_df = MagicMock()
    bar_df.__len__ = lambda s: 5
    signal_engine.get_bars.return_value = bar_df

    config = MagicMock()
    config.risk.max_portfolio_heat_pct = 0.06

    executor = Executor(
        broker=broker,
        portfolio=portfolio,
        signal_engine=signal_engine,
        order_manager=MagicMock(),
        risk_gate=MagicMock(return_value=GateResult(approved=False, reason="no signal")),
        config=config,
    )

    now = datetime.now(tz=timezone.utc)

    # Tick 1: price=905 → candidate_target = 905 + 10 = 915.0
    # 915 > 910 (initial), improvement = 5.0 >= 0.2 → update to 915.0
    await executor.on_tick("BTC/USD", 905.0, 1000, now)
    assert portfolio.positions["BTC/USD"].current_soft_target == pytest.approx(915.0)

    # Tick 2: price=900 → candidate_target = 900 + 10 = 910.0
    # guard: max(910, 915) = 915 → no update (910 < 915, would move backward)
    await executor.on_tick("BTC/USD", 900.0, 1000, now)
    assert portfolio.positions["BTC/USD"].current_soft_target == pytest.approx(915.0)

    # Tick 3: price=908 → candidate_target = 908 + 10 = 918.0
    # guard: max(918, 915) = 918, improvement = 3.0 >= 0.2 → update to 918.0
    await executor.on_tick("BTC/USD", 908.0, 1000, now)
    assert portfolio.positions["BTC/USD"].current_soft_target == pytest.approx(918.0)

    # Market close must not have been called — price never exceeded soft_target
    broker.submit_market_order.assert_not_called()


@pytest.mark.asyncio
async def test_check_position_durations_closes_expired_position():
    broker = MagicMock()
    broker.is_tradable = AsyncMock(return_value=True)
    close_order = MagicMock()
    close_order.symbol = "NVDA"
    broker.submit_market_order = AsyncMock(return_value=close_order)

    stale_entry_time = datetime.now(timezone.utc) - timedelta(minutes=91)
    stale_position = Position(
        ticker="NVDA",
        qty=5.0,
        avg_entry=910.0,
        stop=905.0,
        target=925.0,
        side="long",
        entry_time=stale_entry_time,
    )

    portfolio = MagicMock(spec=Portfolio)
    portfolio.positions = {"NVDA": stale_position}
    portfolio.record_close = MagicMock()

    config = MagicMock()
    config.risk.max_position_duration_minutes = 90
    config.risk.max_portfolio_heat_pct = 0.06

    executor = Executor(
        broker=broker,
        portfolio=portfolio,
        signal_engine=MagicMock(),
        order_manager=MagicMock(),
        risk_gate=MagicMock(),
        config=config,
    )

    # Patch asyncio.sleep so the first call returns normally (allowing one
    # iteration) and the second call raises StopAsyncIteration to break the loop.
    sleep_mock = AsyncMock(side_effect=[None, StopAsyncIteration()])
    with patch("execution.executor.asyncio.sleep", sleep_mock):
        with pytest.raises(StopAsyncIteration):
            await executor.check_position_durations()

    broker.submit_market_order.assert_called_once_with("NVDA", 5.0, "sell")
    portfolio.record_close.assert_called_once_with(close_order)
