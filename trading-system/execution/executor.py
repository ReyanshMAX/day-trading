"""Executor — hot path tick handler.

Single responsibility: on each tick, run signal engine, check risk gate,
build bracket, and submit order. All dependencies are injected.
"""

import logging
from datetime import datetime

from core.broker import AlpacaBroker
from core.config import Config
from core.order_manager import OrderManager
from core.portfolio import Portfolio
from risk.gate import check as gate_check
from signals.engine import SignalEngine
from signals.indicators import fibonacci_levels, detect_swing_high, detect_swing_low

log = logging.getLogger(__name__)


class Executor:
    """Processes ticks and manages the full order lifecycle."""

    def __init__(
        self,
        broker: AlpacaBroker,
        portfolio: Portfolio,
        signal_engine: SignalEngine,
        order_manager: OrderManager,
        risk_gate,
        config: Config,
    ) -> None:
        self._broker = broker
        self._portfolio = portfolio
        self._signal_engine = signal_engine
        self._order_manager = order_manager
        self._risk_gate = risk_gate
        self._config = config

    async def on_tick(self, ticker: str, price: float, volume: float, timestamp: datetime) -> None:
        """Process a single market tick. Errors are caught and logged — never propagated."""
        try:
            # Tradability check
            if not await self._broker.is_tradable(ticker):
                return

            # 1. Run signal engine
            signal = self._signal_engine.on_tick(ticker, price, volume, timestamp)
            if signal is None:
                return

            # 2. Skip if already in position
            if self._portfolio.has_position(ticker):
                return

            # 3. Fast pre-check: skip bracket build if portfolio heat already maxed
            heat = self._portfolio.open_risk_pct()
            if heat >= self._config.risk.max_portfolio_heat_pct:
                log.debug("%s — skipping: portfolio heat %.2f%% >= %.0f%%",
                          ticker, heat * 100, self._config.risk.max_portfolio_heat_pct * 100)
                return

            # 4. Compute Fibonacci levels from swing data
            df = self._signal_engine._bar_store.get_bars(ticker, 50)
            fib = None
            if len(df) >= 20:
                sh = detect_swing_high(df, 20)
                sl = detect_swing_low(df, 20)
                if sh > sl:
                    fib = fibonacci_levels(sh, sl)

            # 5. Build bracket
            bracket = self._order_manager.build_bracket(
                ticker,
                signal.score,
                signal.regime,
                signal.conviction,
                signal.atr,
                price,
                fib_levels=fib,
            )

            # 6. Risk gate
            gate = gate_check(
                ticker,
                signal.direction,
                bracket.qty,
                bracket.stop_distance,
                self._portfolio,
                self._config,
            )
            if not gate.approved:
                log.debug("Gate rejected %s: %s", ticker, gate.reason)
                return

            # 7. Submit order
            order = await self._broker.submit_bracket_order(
                ticker,
                bracket.qty,
                signal.direction,
                bracket.stop,
                bracket.target,
            )
            self._portfolio.record_fill(order, stop=bracket.stop, target=bracket.target, entry_price=price)
            log.info(
                "Order fired: %s %s qty=%s stop=%s target=%s score=%.3f regime=%s conviction=%d",
                ticker, signal.direction, bracket.qty,
                f"{bracket.stop:.5f}".rstrip("0").rstrip("."),
                f"{bracket.target:.5f}".rstrip("0").rstrip("."),
                signal.score, signal.regime, signal.conviction,
            )

        except Exception as e:
            log.error("Executor error for %s: %s", ticker, e, exc_info=True)
