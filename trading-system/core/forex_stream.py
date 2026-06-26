"""OANDA real-time pricing stream via SSE.

Single responsibility: stream bid/ask prices for forex pairs and call
on_tick_callback for each update.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable

import aiohttp

log = logging.getLogger(__name__)

_STREAM_URL = "https://stream-fxpractice.oanda.com/v3"


class ForexStream:
    """Streams OANDA SSE price feed and dispatches to a tick callback."""

    def __init__(self, api_key: str, account_id: str) -> None:
        self._api_key = api_key
        self._account_id = account_id
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def start(
        self,
        pairs: list[str],
        on_tick_callback: Callable,
        max_retries: int = 5,
    ) -> None:
        """Stream prices for pairs, calling on_tick_callback(pair, price, timestamp) on each tick.

        Reconnects with exponential backoff on disconnect.
        """
        instruments = ",".join(p.replace("/", "_") for p in pairs)
        url = (
            f"{_STREAM_URL}/accounts/{self._account_id}/pricing/stream"
            f"?instruments={instruments}"
        )
        retries = 0
        while retries < max_retries:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        headers=self._headers,
                        timeout=aiohttp.ClientTimeout(total=None, sock_read=30),
                    ) as resp:
                        resp.raise_for_status()
                        retries = 0  # reset on successful connect
                        log.info("Forex stream connected for pairs: %s", pairs)
                        async for line in resp.content:
                            if not line.strip():
                                continue
                            try:
                                msg = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if msg.get("type") != "PRICE":
                                continue
                            pair = msg["instrument"].replace("_", "/")
                            bids = msg.get("bids", [])
                            asks = msg.get("asks", [])
                            if not bids or not asks:
                                continue
                            mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
                            ts_str = msg.get("time", "")
                            try:
                                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            except Exception:
                                ts = datetime.now(timezone.utc)
                            await on_tick_callback(pair, mid, 1.0, ts)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                retries += 1
                wait = 2 ** retries
                log.error(
                    "Forex stream disconnected: %s. Retry %d/%d in %ds",
                    e, retries, max_retries, wait,
                )
                await asyncio.sleep(wait)

        log.critical("Forex stream failed after %d retries. Manual restart required.", max_retries)
