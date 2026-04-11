"""Alpaca WebSocket tick stream with exponential backoff reconnect.

Single responsibility: subscribe to live trade ticks and forward them to
the executor callback. Equity tickers use Alpaca IEX WebSocket; crypto
tickers are routed to Binance's public aggTrade stream (see binance_stream.py).
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from alpaca.data import DataFeed
from alpaca.data.live import StockDataStream

from core import coinbase_stream

log = logging.getLogger(__name__)

_MAX_RETRIES = 5


def _is_crypto(ticker: str) -> bool:
    return "/" in ticker or (ticker.endswith("USD") and len(ticker) > 4)


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
            wait = 2 ** retries
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


async def start(
    tickers: list[str],
    on_tick_callback: Callable[..., Any],
    api_key: str = "",
    secret_key: str = "",
) -> None:
    """Subscribe to live trade ticks for all tickers.

    Equities → Alpaca IEX WebSocket.
    Crypto   → Binance public aggTrade WebSocket (no API key, lower latency).
    Both feed into the same on_tick_callback signature.
    """
    equity_tickers = [t for t in tickers if not _is_crypto(t)]
    crypto_tickers = [t for t in tickers if _is_crypto(t)]

    tasks = []

    if equity_tickers:
        tasks.append(_run_equity_stream(api_key, secret_key, equity_tickers, on_tick_callback))

    if crypto_tickers:
        tasks.append(coinbase_stream.start(crypto_tickers, on_tick_callback))

    if not tasks:
        log.error("No tickers to subscribe to.")
        return

    await asyncio.gather(*tasks)
