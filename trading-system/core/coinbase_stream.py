"""Coinbase Exchange public WebSocket stream for crypto tick data.

Single responsibility: subscribe to the 'matches' channel for crypto pairs
and forward normalized ticks to the executor callback. No API key required —
Coinbase's exchange feed is public and unauthenticated.

'match' messages fire on every executed trade (~real-time, US-accessible).
Latency is typically 10-80ms from Coinbase's matching engine.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import websockets

log = logging.getLogger(__name__)

_MAX_RETRIES = 5
_URL = "wss://ws-feed.exchange.coinbase.com"

# Config symbol → Coinbase product ID
_SYMBOL_TO_PRODUCT: dict[str, str] = {
    "BTC/USD": "BTC-USD",
    "ETH/USD": "ETH-USD",
    "SOL/USD": "SOL-USD",
    "AVAX/USD": "AVAX-USD",
    "DOGE/USD": "DOGE-USD",
}

# Reverse: Coinbase product ID → config symbol
_PRODUCT_TO_SYMBOL: dict[str, str] = {v: k for k, v in _SYMBOL_TO_PRODUCT.items()}


def _subscribe_msg(tickers: list[str]) -> str:
    products = []
    for ticker in tickers:
        product = _SYMBOL_TO_PRODUCT.get(ticker)
        if product is None:
            log.warning("No Coinbase product mapping for %s — skipping", ticker)
            continue
        products.append(product)

    if not products:
        raise ValueError("No valid Coinbase products found for given tickers")

    return json.dumps({
        "type": "subscribe",
        "product_ids": products,
        "channels": ["matches"],
    })


async def _connect_and_stream(
    tickers: list[str],
    on_tick_callback: Callable[..., Any],
) -> None:
    """Open one WebSocket connection and dispatch match messages until disconnect."""
    sub_msg = _subscribe_msg(tickers)

    async with websockets.connect(_URL, ping_interval=20, ping_timeout=10) as ws:
        await ws.send(sub_msg)
        log.info("Coinbase crypto stream connected. Subscribed to: %s", tickers)

        async for raw in ws:
            try:
                msg = json.loads(raw)
                msg_type = msg.get("type")

                # "match" = live trade; "last_match" = snapshot on subscribe
                if msg_type not in ("match", "last_match"):
                    continue

                product_id = msg["product_id"]
                ticker = _PRODUCT_TO_SYMBOL.get(product_id)
                if ticker is None:
                    log.debug("Unknown Coinbase product in stream: %s", product_id)
                    continue

                price = float(msg["price"])
                volume = float(msg["size"])
                # ISO 8601 timestamp with microseconds e.g. "2024-01-15T14:23:01.123456Z"
                timestamp = datetime.fromisoformat(msg["time"].replace("Z", "+00:00"))

                await on_tick_callback(ticker, price, volume, timestamp)

            except Exception as e:
                log.error("Coinbase tick parse error: %s | raw=%s", e, raw[:200])


async def start(
    tickers: list[str],
    on_tick_callback: Callable[..., Any],
) -> None:
    """Subscribe to Coinbase match stream for crypto tickers.

    Single persistent WebSocket connection for all pairs. Retries with
    exponential backoff on disconnect, counter resets on clean connects.
    """
    retries = 0
    while retries < _MAX_RETRIES:
        try:
            retries = 0  # reset on each clean attempt
            await _connect_and_stream(tickers, on_tick_callback)
            log.warning("Coinbase stream closed cleanly. Reconnecting...")
        except ValueError as e:
            # Bad config — no point retrying
            log.error("Coinbase stream config error: %s", e)
            return
        except Exception as e:
            retries += 1
            wait = 2 ** retries
            log.error(
                "Coinbase stream error: %s. Retrying in %ds (%d/%d)",
                e, wait, retries, _MAX_RETRIES,
            )
            await asyncio.sleep(wait)

    log.critical("Coinbase stream failed after %d retries. Manual restart required.", _MAX_RETRIES)
