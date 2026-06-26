"""OANDA practice account broker wrapper.

Single responsibility: all REST communication with the OANDA practice API.
Operates in units (not lots). Prices are bid/ask mid-points.
"""

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta, timezone

import aiohttp
import pandas as pd

from core.config import Config

log = logging.getLogger(__name__)

_BASE_URL = "https://api-fxpractice.oanda.com/v3"


class OANDABroker:
    """Wraps OANDA practice REST API for forex trading."""

    def __init__(self, api_key: str, account_id: str) -> None:
        self._api_key = api_key
        self._account_id = account_id
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{_BASE_URL}{path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._headers, params=params) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def _post(self, path: str, body: dict) -> dict:
        url = f"{_BASE_URL}{path}"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self._headers, data=json.dumps(body)) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def get_account(self) -> dict:
        """Return NAV, unrealized P&L, and margin used."""
        data = await self._get(f"/accounts/{self._account_id}/summary")
        acct = data["account"]
        return {
            "nav": float(acct["NAV"]),
            "unrealized_pnl": float(acct["unrealizedPL"]),
            "margin_used": float(acct["marginUsed"]),
        }

    async def get_bars(self, pair: str, granularity: str, count: int) -> pd.DataFrame:
        """Fetch candles for a forex pair. granularity: M1, M5, H1, D.

        Returns DataFrame with open/high/low/close/volume columns and UTC DatetimeIndex.
        """
        # Convert pair format EUR_USD → OANDA instrument
        instrument = pair.replace("/", "_")
        params = {
            "count": str(count),
            "granularity": granularity,
            "price": "M",  # midpoint candles
        }
        data = await self._get(f"/instruments/{instrument}/candles", params=params)
        candles = data.get("candles", [])
        if not candles:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        rows = []
        for c in candles:
            mid = c["mid"]
            rows.append({
                "open": float(mid["o"]),
                "high": float(mid["h"]),
                "low": float(mid["l"]),
                "close": float(mid["c"]),
                "volume": float(c.get("volume", 0)),
                "timestamp": pd.Timestamp(c["time"], tz="UTC"),
            })
        df = pd.DataFrame(rows).set_index("timestamp")
        df.index = pd.to_datetime(df.index, utc=True)
        return df

    async def submit_order(
        self,
        pair: str,
        units: int,
        side: str,
        stop_price: float,
        target_price: float,
    ) -> dict:
        """Submit a market order with stop-loss and take-profit.

        units: positive integer. side: "long" or "short".
        For long: units is positive. For short: units is negative.
        """
        signed_units = units if side == "long" else -units
        instrument = pair.replace("/", "_")
        body = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(signed_units),
                "stopLossOnFill": {"price": f"{stop_price:.5f}"},
                "takeProfitOnFill": {"price": f"{target_price:.5f}"},
                "timeInForce": "FOK",  # fill or kill
                "positionFill": "DEFAULT",
            }
        }
        data = await self._post(f"/accounts/{self._account_id}/orders", body)
        log.info(
            "Forex order submitted: %s %s units=%d stop=%.5f target=%.5f",
            pair, side, units, stop_price, target_price,
        )
        return data

    async def get_open_positions(self) -> list[dict]:
        """Return all open forex positions."""
        data = await self._get(f"/accounts/{self._account_id}/openPositions")
        positions = []
        for pos in data.get("positions", []):
            instrument = pos["instrument"].replace("_", "/")
            long_units = float(pos.get("long", {}).get("units", 0))
            short_units = float(pos.get("short", {}).get("units", 0))
            if long_units != 0:
                positions.append({
                    "pair": instrument,
                    "side": "long",
                    "units": long_units,
                    "avg_price": float(pos["long"].get("averagePrice", 0)),
                    "unrealized_pnl": float(pos["long"].get("unrealizedPL", 0)),
                })
            if short_units != 0:
                positions.append({
                    "pair": instrument,
                    "side": "short",
                    "units": abs(short_units),
                    "avg_price": float(pos["short"].get("averagePrice", 0)),
                    "unrealized_pnl": float(pos["short"].get("unrealizedPL", 0)),
                })
        return positions

    async def close_position(self, pair: str, side: str = "ALL") -> dict:
        """Close an open position. side: 'ALL', 'LONG', or 'SHORT'."""
        instrument = pair.replace("/", "_")
        body = {}
        if side in ("LONG", "SHORT"):
            body[side.lower() + "Units"] = "ALL"
        else:
            body["longUnits"] = "ALL"
            body["shortUnits"] = "ALL"
        try:
            data = await self._post(
                f"/accounts/{self._account_id}/positions/{instrument}/close",
                body,
            )
            log.info("Forex position closed: %s", pair)
            return data
        except Exception as e:
            log.error("Failed to close forex position %s: %s", pair, e)
            return {}

    def compute_units(
        self,
        nav: float,
        risk_pct: float,
        stop_distance_pips: float,
        pip_value: float = 10.0,
    ) -> int:
        """Compute units to risk risk_pct of NAV with stop_distance_pips.

        pip_value: dollars per pip per standard lot (100k units).
        Default 10.0 is correct for USD-denominated pairs (EUR/USD, GBP/USD).
        """
        if stop_distance_pips <= 0:
            return 1000  # minimum 1 micro lot
        risk_dollars = nav * risk_pct
        # pip_value per standard lot * (units / 100000) = pnl per pip
        # units = risk_dollars / (stop_pips * pip_value / 100000)
        units = int(risk_dollars / (stop_distance_pips * pip_value / 100_000))
        return max(1000, units)  # minimum 1 micro lot = 1000 units
