"""Alpaca WebSocket tick stream with exponential backoff reconnect.

Single responsibility: subscribe to live trade ticks and forward them to
the executor callback. Uses Alpaca IEX WebSocket for equities.
Also runs a TradingStream for order update events so hard stop fills are
detected via push rather than polling.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from alpaca.data import DataFeed
from alpaca.data.live import StockDataStream
from alpaca.trading.stream import TradingStream

log = logging.getLogger(__name__)

_MAX_RETRIES = 5


async def _run_equity_stream(
    api_key: str,
    secret_key: str,
    tickers: list[str],
    on_tick_callback: Callable[..., Any],
) -> None:
    """Run Alpaca IEX equity stream with exponential backoff retry."""
    async def handler(data: Any) -> None:
        try:
            await on_tick_callback(
                data.symbol,
                float(data.price),
                float(data.size),
                data.timestamp,
            )
        except Exception as e:
            log.error("Equity tick handler error for %s: %s", data.symbol, e)

    retries = 0
    while retries < _MAX_RETRIES:
        stream = None
        try:
            stream = StockDataStream(api_key, secret_key, feed=DataFeed.IEX)
            stream.subscribe_trades(handler, *tickers)
            log.info("Equity stream connected. Subscribed to: %s", tickers)
            retries = 0
            await stream._run_forever()
        except Exception as e:
            retries += 1
            # "connection limit exceeded" means Alpaca still has a prior session
            # open — a short retry delay just triggers the same error repeatedly.
            # Give Alpaca 30 s to expire the old connection before re-attempting.
            if "connection limit" in str(e).lower():
                wait = 30
            else:
                wait = 5 * (2 ** retries)
            log.error(
                "Equity stream disconnected: %s. Retrying in %ds (%d/%d)",
                e, wait, retries, _MAX_RETRIES,
            )
            if stream is not None:
                try:
                    await stream.stop_ws()
                except Exception:
                    pass
            await asyncio.sleep(wait)

    log.critical("Equity stream failed after %d retries. Manual restart required.", _MAX_RETRIES)


async def _run_trading_stream(
    api_key: str,
    secret_key: str,
    portfolio: Any,
    on_reconnect: Callable[[], Any] | None = None,
    paper: bool = True,
) -> None:
    """Subscribe to Alpaca order update events via TradingStream.

    On fill/partial_fill for a stop-loss order, calls portfolio.record_close()
    so the position is removed immediately via push — no polling required.
    On reconnect (after a disconnect), calls on_reconnect() if provided so
    callers can trigger reconcile_positions().
    """
    async def on_trade_update(data: Any) -> None:
        try:
            event = getattr(data, "event", None)
            if event not in ("fill", "partial_fill"):
                return
            order = getattr(data, "order", None)
            if order is None:
                log.warning("TradingStream fill event has no order object: %s", data)
                return
            order_type = str(getattr(order, "order_type", "") or "").lower()
            order_class = str(getattr(order, "order_class", "") or "").lower()
            # Detect stop-loss fills: stop/stop_limit types, or legs of a bracket
            is_stop_fill = (
                "stop" in order_type
                or order_class in ("bracket",)
                and str(getattr(order, "legs", None) or "").lower().find("stop") != -1
            )
            ticker = str(order.symbol)
            fill_price = getattr(order, "filled_avg_price", None)
            log.info(
                "TradingStream %s event: %s order_type=%s fill_price=%s order_id=%s",
                event, ticker, order_type, fill_price, getattr(order, "id", "?"),
            )
            if is_stop_fill and portfolio.has_position(ticker):
                log.info(
                    "Hard stop fill detected for %s via TradingStream — recording close",
                    ticker,
                )
                portfolio.record_close(order)
        except Exception as e:
            log.error("TradingStream trade update handler error: %s", e, exc_info=True)

    retries = 0
    while retries < _MAX_RETRIES:
        ts = None
        try:
            ts = TradingStream(api_key, secret_key, paper=paper)
            ts.subscribe_trade_updates(on_trade_update)
            log.info("TradingStream connected — listening for order fill events")
            retries = 0
            await ts._run_forever()
        except Exception as e:
            retries += 1
            if "connection limit" in str(e).lower():
                wait = 30
            else:
                wait = 5 * (2 ** retries)
            log.error(
                "TradingStream disconnected: %s. Retrying in %ds (%d/%d)",
                e, wait, retries, _MAX_RETRIES,
            )
            if ts is not None:
                try:
                    await ts.stop_ws()
                except Exception:
                    pass
            await asyncio.sleep(wait)
            if on_reconnect is not None:
                try:
                    await on_reconnect()
                except Exception as rc_err:
                    log.error("on_reconnect callback failed: %s", rc_err)

    log.critical("TradingStream failed after %d retries. Manual restart required.", _MAX_RETRIES)


async def start(
    tickers: list[str],
    on_tick_callback: Callable[..., Any],
    api_key: str = "",
    secret_key: str = "",
    portfolio: Any = None,
    on_reconnect: Callable[[], Any] | None = None,
    paper: bool = True,
) -> None:
    """Subscribe to live trade ticks for all tickers via Alpaca IEX WebSocket.

    Also starts a TradingStream to detect hard stop fills via push events.
    Pass portfolio and an optional on_reconnect callback; if omitted the
    TradingStream is not started (backwards-compatible for tests/smoke runs).
    """
    tasks = []

    if tickers:
        tasks.append(_run_equity_stream(api_key, secret_key, tickers, on_tick_callback))

    if portfolio is not None and api_key:
        tasks.append(_run_trading_stream(api_key, secret_key, portfolio, on_reconnect, paper=paper))

    if not tasks:
        log.error("No tickers to subscribe to.")
        return

    await asyncio.gather(*tasks)
