"""Alpaca WebSocket tick stream with exponential backoff reconnect.

Single responsibility: subscribe to live trade ticks and forward them to
the executor callback. Never touches signal or order logic.
"""

import asyncio
import logging

from alpaca.data.live import StockDataStream

log = logging.getLogger(__name__)

_MAX_RETRIES = 5


async def start(tickers: list[str], on_tick_callback, api_key: str = "", secret_key: str = "") -> None:
    """Subscribe to trade ticks for all tickers, reconnect on failure."""
    retries = 0
    while retries < _MAX_RETRIES:
        try:
            stream = StockDataStream(api_key, secret_key)

            async def handler(data):
                try:
                    await on_tick_callback(
                        data.symbol,
                        float(data.price),
                        float(data.size),
                        data.timestamp,
                    )
                except Exception as e:
                    log.error("Tick handler error for %s: %s", data.symbol, e)

            stream.subscribe_trades(handler, *tickers)
            log.info("Stream connected. Subscribed to %d tickers.", len(tickers))
            retries = 0  # reset on successful connect
            await stream.run()

        except Exception as e:
            retries += 1
            wait = 2 ** retries
            log.error(
                "Stream disconnected: %s. Retrying in %ds (%d/%d)",
                e, wait, retries, _MAX_RETRIES,
            )
            await asyncio.sleep(wait)

    log.critical("Stream failed after %d retries. Manual restart required.", _MAX_RETRIES)
