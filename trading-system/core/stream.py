"""Alpaca WebSocket tick stream with exponential backoff reconnect.

Single responsibility: subscribe to live trade ticks and forward them to
the executor callback. Handles equity and crypto tickers on separate streams.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from alpaca.data import DataFeed
from alpaca.data.live import StockDataStream, CryptoDataStream

log = logging.getLogger(__name__)

_MAX_RETRIES = 5


def _is_crypto(ticker: str) -> bool:
    return "/" in ticker or (ticker.endswith("USD") and len(ticker) > 4)


async def _run_with_retry(
    stream_factory: Callable,
    handler: Callable,
    tickers: list[str],
    label: str,
) -> None:
    """Run a stream with exponential backoff retry, resetting on clean connect."""
    retries = 0
    while retries < _MAX_RETRIES:
        stream = None
        try:
            stream = stream_factory()
            stream.subscribe_trades(handler, *tickers)
            log.info("%s stream connected. Subscribed to: %s", label, tickers)
            retries = 0  # reset counter on clean connect
            await stream._run_forever()
        except Exception as e:
            retries += 1
            wait = 2 ** retries
            log.error(
                "%s stream disconnected: %s. Retrying in %ds (%d/%d)",
                label, e, wait, retries, _MAX_RETRIES,
            )
            if stream is not None:
                try:
                    await stream.stop_ws()
                except Exception:
                    pass
            await asyncio.sleep(wait)

    log.critical("%s stream failed after %d retries. Manual restart required.", label, _MAX_RETRIES)


async def start(
    tickers: list[str],
    on_tick_callback: Callable[..., Any],
    api_key: str = "",
    secret_key: str = "",
) -> None:
    """Subscribe to trade ticks for all tickers.

    Splits tickers into equity and crypto, runs each on its own stream
    concurrently. Both feed into the same on_tick_callback.
    """
    equity_tickers = [t for t in tickers if not _is_crypto(t)]
    crypto_tickers = [t for t in tickers if _is_crypto(t)]

    async def handler(data: Any) -> None:
        try:
            await on_tick_callback(
                data.symbol,
                float(data.price),
                float(data.size),
                data.timestamp,
            )
        except Exception as e:
            log.error("Tick handler error for %s: %s", data.symbol, e)

    tasks = []

    if equity_tickers:
        tasks.append(_run_with_retry(
            lambda: StockDataStream(api_key, secret_key, feed=DataFeed.IEX),
            handler,
            equity_tickers,
            "equity",
        ))

    if crypto_tickers:
        tasks.append(_run_with_retry(
            lambda: CryptoDataStream(api_key, secret_key),
            handler,
            crypto_tickers,
            "crypto",
        ))

    if not tasks:
        log.error("No tickers to subscribe to.")
        return

    await asyncio.gather(*tasks)
