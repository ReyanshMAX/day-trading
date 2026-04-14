"""Executor — hot path tick handler.

Single responsibility: on each tick, run signal engine, check risk gate,
build bracket, and submit order. All dependencies are injected.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from core.broker import AlpacaBroker
from core.config import Config
from core.order_manager import OrderManager
from core.portfolio import Portfolio
from signals.engine import SignalEngine
from signals.indicators import fibonacci_levels, detect_swing_high, detect_swing_low

log = logging.getLogger(__name__)

_LATENCY_WARN_SECONDS = 0.1  # 100 ms


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
        self._risk_gate = risk_gate  # callable: (ticker, direction, qty, stop_distance, portfolio, config) -> GateResult
        self._config = config
        # Cache populated by refresh_asset_cache() background coroutine.
        # Defaults to True (tradable) when a ticker has not yet been evaluated
        # so that early ticks are not silently dropped before the first refresh.
        self._asset_tradable_cache: dict[str, bool] = {}

    async def refresh_asset_cache(self) -> None:
        """Background coroutine: refresh tradability for all config tickers every 60 s."""
        tickers = self._config.universe.tickers
        while True:
            for ticker in tickers:
                try:
                    tradable = await self._broker.is_tradable(ticker)
                    self._asset_tradable_cache[ticker] = tradable
                except Exception as e:
                    log.warning("refresh_asset_cache: is_tradable(%s) failed: %s", ticker, e)
            await asyncio.sleep(60)

    async def on_tick(self, ticker: str, price: float, volume: float, timestamp: datetime) -> None:
        """Process a single market tick. Errors are caught and logged — never propagated."""
        t0 = time.monotonic()
        try:
            # Tradability check — O(1) cache lookup, no REST call on the hot path.
            # Default True so early ticks before first cache refresh are not dropped.
            if not self._asset_tradable_cache.get(ticker, True):
                return

            # 0. Soft take-profit for crypto positions.
            # Alpaca locks the full crypto balance behind the pending stop-loss GTC
            # order, so a second hard take-profit order would be rejected. Instead,
            # we monitor price against pos.current_soft_target on every tick and
            # submit a market close when the target is reached.
            _is_crypto = "/" in ticker or (ticker.endswith("USD") and len(ticker) > 4)
            if _is_crypto:
                pos = self._portfolio.positions.get(ticker)
                if pos is not None:
                    # --- Trailing soft target update ---
                    # Compute a candidate new target from the current price plus
                    # the same ATR offset that was used at entry (stored on pos).
                    # For longs the target trails upward only; for shorts downward only.
                    # Minimum increment: 0.1 * atr — suppress noise updates.
                    if pos.atr > 0:
                        min_increment = 0.1 * pos.atr
                        if pos.side == "long":
                            # New candidate: price + same distance as original bracket
                            original_dist = pos.target - pos.avg_entry
                            candidate_target = price + original_dist
                            # Guard: target must never move backward (downward for longs)
                            new_soft_target = max(candidate_target, pos.current_soft_target)
                            # Only apply update if improvement is at least min_increment
                            if new_soft_target - pos.current_soft_target >= min_increment:
                                log.debug(
                                    "Trailing soft target updated for %s: %.5f -> %.5f (price=%.5f)",
                                    ticker, pos.current_soft_target, new_soft_target, price,
                                )
                                pos.current_soft_target = new_soft_target
                        else:  # short
                            original_dist = pos.avg_entry - pos.target
                            candidate_target = price - original_dist
                            # Guard: target must never move backward (upward for shorts)
                            new_soft_target = min(candidate_target, pos.current_soft_target)
                            if pos.current_soft_target - new_soft_target >= min_increment:
                                log.debug(
                                    "Trailing soft target updated for %s: %.5f -> %.5f (price=%.5f)",
                                    ticker, pos.current_soft_target, new_soft_target, price,
                                )
                                pos.current_soft_target = new_soft_target

                    hit_target = (
                        (pos.side == "long" and price >= pos.current_soft_target) or
                        (pos.side == "short" and price <= pos.current_soft_target)
                    )
                    if hit_target:
                        # Guard: re-check atomically. If the hard stop already
                        # filled via TradingStream, has_position returns False
                        # and we skip the redundant market close.
                        if not self._portfolio.has_position(ticker):
                            log.info(
                                "Soft take-profit skipped for %s: position already closed (hard stop filled)",
                                ticker,
                            )
                            return
                        log.info(
                            "Soft take-profit triggered for %s: price=%.5f soft_target=%.5f — closing position",
                            ticker, price, pos.current_soft_target,
                        )
                        try:
                            # Cancel the live GTC stop-loss order before submitting
                            # the market close, otherwise Alpaca will try to execute
                            # both and may open an unintended short after the position
                            # is already gone.
                            if pos.stop_order_id:
                                try:
                                    await self._broker.cancel_order(pos.stop_order_id)
                                    log.info(
                                        "Cancelled stop-loss order %s for %s before soft take-profit close",
                                        pos.stop_order_id, ticker,
                                    )
                                except Exception as cancel_err:
                                    log.error(
                                        "Failed to cancel stop-loss order %s for %s: %s",
                                        pos.stop_order_id, ticker, cancel_err,
                                    )
                            # Atomically re-check: hard stop may have filled while
                            # we were awaiting cancel_order above.
                            if not self._portfolio.has_position(ticker):
                                log.info(
                                    "Soft take-profit skipped for %s: position closed during cancel_order await",
                                    ticker,
                                )
                                return
                            close_side = "sell" if pos.side == "long" else "buy"
                            # Submit market close with one retry on failure.
                            try:
                                close_order = await self._broker.submit_market_order(
                                    ticker, pos.qty, close_side
                                )
                                self._portfolio.record_close(close_order)
                            except Exception as first_err:
                                log.error(
                                    "Soft take-profit close failed for %s (attempt 1/2): %s — retrying in 0.5s",
                                    ticker, first_err,
                                )
                                await asyncio.sleep(0.5)
                                try:
                                    close_order = await self._broker.submit_market_order(
                                        ticker, pos.qty, close_side
                                    )
                                    self._portfolio.record_close(close_order)
                                except Exception as second_err:
                                    log.critical(
                                        "Soft take-profit close FAILED after 2 attempts for %s: %s "
                                        "| position state: side=%s qty=%.4f entry=%.4f "
                                        "stop=%.4f soft_target=%.4f — position left open",
                                        ticker, second_err,
                                        pos.side, pos.qty, pos.avg_entry,
                                        pos.stop, pos.current_soft_target,
                                    )
                        except Exception as tp_err:
                            log.error("Soft take-profit close failed for %s: %s", ticker, tp_err)
                        return

            # 1. Skip if already in position (before running signal engine for efficiency)
            if self._portfolio.has_position(ticker):
                return

            # 2. Run signal engine
            signal = self._signal_engine.on_tick(ticker, price, volume, timestamp)
            if signal is None:
                return

            # 3. Fast pre-check: skip bracket build if portfolio heat already maxed
            heat = self._portfolio.open_risk_pct()
            if heat >= self._config.risk.max_portfolio_heat_pct:
                log.debug("%s — skipping: portfolio heat %.2f%% >= %.0f%%",
                          ticker, heat * 100, self._config.risk.max_portfolio_heat_pct * 100)
                return

            # 4. Compute Fibonacci levels from swing data via public method
            df = self._signal_engine.get_bars(ticker, 50)
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

            # 6. Risk gate — call via injected callable, not module-level import
            gate = self._risk_gate(
                ticker,
                signal.direction,
                bracket.qty,
                bracket.stop_distance,
                self._portfolio,
                self._config,
            )
            if gate.set_loss_limit:
                self._portfolio.daily_loss_limit_hit = True
            if not gate.approved:
                log.debug("Gate rejected %s: %s", ticker, gate.reason)
                elapsed = time.monotonic() - t0
                if elapsed > _LATENCY_WARN_SECONDS:
                    log.warning("on_tick latency %s: %.1f ms (suppressed at gate)", ticker, elapsed * 1000)
                return

            # 7. Submit order
            order = await self._broker.submit_bracket_order(
                ticker,
                bracket.qty,
                signal.direction,
                bracket.stop,
                bracket.target,
            )
            # For crypto, retrieve the stop order ID stashed by broker so we can
            # cancel it later when the soft take-profit fires.
            stop_order_id = self._broker.pop_crypto_stop_order_id(ticker) if _is_crypto else None
            self._portfolio.record_fill(
                order,
                stop=bracket.stop,
                target=bracket.target,
                entry_price=price,
                stop_order_id=stop_order_id,
                atr=signal.atr,
            )
            elapsed = time.monotonic() - t0
            if elapsed > _LATENCY_WARN_SECONDS:
                log.warning("on_tick latency %s: %.1f ms (order submitted)", ticker, elapsed * 1000)
            log.info(
                "Order fired: %s %s qty=%s stop=%s target=%s score=%.3f regime=%s conviction=%d",
                ticker, signal.direction, bracket.qty,
                f"{bracket.stop:.5f}".rstrip("0").rstrip("."),
                f"{bracket.target:.5f}".rstrip("0").rstrip("."),
                signal.score, signal.regime, signal.conviction,
            )

        except Exception as e:
            log.error("Executor error for %s: %s", ticker, e, exc_info=True)

    async def check_position_durations(self) -> None:
        """Periodically close positions that have exceeded max_position_duration_minutes."""
        max_minutes = self._config.risk.max_position_duration_minutes
        while True:
            await asyncio.sleep(300)
            now = datetime.now(timezone.utc)
            for ticker, pos in list(self._portfolio.positions.items()):
                elapsed_seconds = (now - pos.entry_time).total_seconds()
                elapsed_minutes = elapsed_seconds / 60.0
                if elapsed_minutes > max_minutes:
                    side = "sell" if pos.side == "long" else "buy"
                    log.info(
                        "Max duration exceeded for %s: entry_time=%s elapsed=%.1f min — closing position",
                        ticker, pos.entry_time.isoformat(), elapsed_minutes,
                    )
                    try:
                        close_order = await self._broker.submit_market_order(ticker, pos.qty, side)
                        self._portfolio.record_close(close_order)
                    except Exception as e:
                        log.error("Duration close failed for %s: %s", ticker, e, exc_info=True)
