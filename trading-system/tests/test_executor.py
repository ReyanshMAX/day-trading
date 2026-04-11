"""Integration tests for execution/executor.py.

All dependencies mocked. Verifies order fires only in the correct path.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from execution.executor import Executor
from signals.engine import SignalResult
from signals.scoring import IndicatorSnapshot
from risk.gate import GateResult
from core.portfolio import Portfolio


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
    bar_store = MagicMock()
    bar_store.get_bars.return_value = MagicMock(__len__=lambda s: 5)  # < 20 bars → no fib
    signal_engine._bar_store = bar_store

    order_manager = MagicMock()
    bracket = MagicMock()
    bracket.qty = 5
    bracket.stop = 905.0
    bracket.target = 925.0
    bracket.stop_distance = 5.0
    order_manager.build_bracket.return_value = bracket

    risk_gate = MagicMock()
    risk_gate.check.return_value = GateResult(approved=gate_approved, reason=None if gate_approved else "test")

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
    # Patch gate_check directly
    import risk.gate
    from unittest.mock import patch
    with patch("execution.executor.gate_check", return_value=GateResult(approved=True, reason=None)):
        await executor.on_tick("NVDA", 910.0, 50000, datetime.now(tz=timezone.utc))

    broker.submit_bracket_order.assert_called_once()


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
    import risk.gate
    from unittest.mock import patch
    executor, broker, *_ = _make_executor(
        signal_return=make_signal(), has_position=False, gate_approved=False
    )
    with patch("execution.executor.gate_check", return_value=GateResult(approved=False, reason="portfolio heat")):
        await executor.on_tick("NVDA", 910.0, 50000, datetime.now(tz=timezone.utc))
    broker.submit_bracket_order.assert_not_called()


@pytest.mark.asyncio
async def test_broker_exception_does_not_crash_executor():
    from unittest.mock import patch
    executor, broker, *_ = _make_executor(
        signal_return=make_signal(), has_position=False, broker_raises=True
    )
    with patch("execution.executor.gate_check", return_value=GateResult(approved=True, reason=None)):
        # Should not raise
        await executor.on_tick("NVDA", 910.0, 50000, datetime.now(tz=timezone.utc))


@pytest.mark.asyncio
async def test_not_tradable_broker_not_called():
    executor, broker, *_ = _make_executor(
        signal_return=make_signal(), is_tradable=False
    )
    await executor.on_tick("NVDA", 910.0, 50000, datetime.now(tz=timezone.utc))
    broker.submit_bracket_order.assert_not_called()
