"""Alpaca REST client wrapper.

Single responsibility: all communication with the Alpaca paper trading API.
No signal logic, no risk logic — pure I/O.
"""

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Order
from alpaca.trading.requests import (
    MarketOrderRequest, StopLimitOrderRequest,
    OrderClass, StopLossRequest, TakeProfitRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data import DataFeed

from core.config import Config
from core.utils import is_crypto

log = logging.getLogger(__name__)

_asset_cache: dict[str, bool] = {}
_crypto_stop_orders: dict[str, str] = {}  # ticker -> stop order ID, populated by _submit_crypto_orders


def _price_decimals(price: float) -> int:
    """Return decimal precision for order prices based on asset price magnitude.

    Assets priced below $1 (e.g. DOGE ~$0.15) need 5dp to have meaningful
    tick granularity. Higher-priced assets use 2dp.
    """
    if price < 1.0:
        return 5
    if price < 10.0:
        return 4
    return 2


async def _run_in_executor(func, *args):
    """Run a synchronous blocking call in a thread pool executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func, *args)


class AlpacaBroker:
    """Wraps Alpaca REST for trading operations and historical bar fetching."""

    def __init__(self, config: Config) -> None:
        self._trading = TradingClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
            paper=config.account.paper,
        )
        self._data = StockHistoricalDataClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
        )
        self._crypto_data = CryptoHistoricalDataClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
        )
        self._config = config

    async def get_account(self) -> dict:
        """Return NAV, buying power, and cash from the paper account."""
        acct = await _run_in_executor(self._trading.get_account)
        return {
            "nav": float(acct.portfolio_value),
            "buying_power": float(acct.buying_power),
            "cash": float(acct.cash),
        }

    async def get_positions(self) -> list:
        """Return all open positions from the broker (async, run in thread pool)."""
        return await _run_in_executor(self._trading.get_all_positions)

    async def cancel_all_orders_for(self, ticker: str) -> None:
        """Cancel all open orders for a specific ticker.

        TradingClient.cancel_orders() cancels everything; there is no built-in
        per-symbol cancel endpoint. This fetches open orders filtered by symbol
        and cancels each individually.
        """
        try:
            request = GetOrdersRequest(symbols=[ticker])
            orders = await _run_in_executor(self._trading.get_orders, request)
            for order in orders:
                try:
                    await _run_in_executor(self._trading.cancel_order_by_id, str(order.id))
                    log.info("Cancelled order %s for %s via cancel_all_orders_for", order.id, ticker)
                except Exception as e:
                    log.warning("Failed to cancel order %s for %s: %s", order.id, ticker, e)
        except Exception as e:
            log.error("cancel_all_orders_for(%s) failed: %s", ticker, e)

    def get_open_orders(self) -> list:
        """Return all open orders."""
        return self._trading.get_orders()

    async def cancel_order(self, order_id: str) -> None:
        """Cancel an order by ID."""
        await _run_in_executor(self._trading.cancel_order_by_id, order_id)
        log.info("Cancelled order %s", order_id)

    def pop_crypto_stop_order_id(self, ticker: str) -> str | None:
        """Return and remove the pending GTC stop order ID for a crypto ticker, if any."""
        return _crypto_stop_orders.pop(ticker, None)

    async def submit_bracket_order(
        self,
        ticker: str,
        qty: int,
        side: str,
        stop_price: float,
        take_profit_price: float,
    ) -> Order:
        """Submit entry + stop/target orders.

        Equities: single bracket (otoco) order.
        Crypto: market entry + separate stop-limit + limit orders (Alpaca does
        not support bracket order_class for crypto).
        """
        if is_crypto(ticker):
            return await self._submit_crypto_orders(ticker, qty, side, stop_price, take_profit_price)

        alpaca_side = OrderSide.BUY if side == "long" else OrderSide.SELL
        request = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=alpaca_side,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
            take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2)),
        )
        order = await _run_in_executor(self._trading.submit_order, request)
        log.info(
            "Bracket order submitted: %s %s qty=%d stop=%.2f target=%.2f order_id=%s",
            ticker, side, qty, stop_price, take_profit_price, order.id,
        )
        return order

    async def _submit_crypto_orders(
        self,
        ticker: str,
        qty: int,
        side: str,
        stop_price: float,
        take_profit_price: float,
    ) -> Order:
        """Crypto entry: market order + separate stop-limit + limit legs (GTC).

        Alpaca does not support bracket/otoco for crypto, so the three legs are
        independent orders. Both exit orders are sized to qty so only one fill
        is expected; the other should be cancelled by the EOD sweep or a future
        fill-event handler.
        """
        entry_side = OrderSide.BUY if side == "long" else OrderSide.SELL
        exit_side = OrderSide.SELL if side == "long" else OrderSide.BUY

        # 1. Entry
        entry_order = await _run_in_executor(
            self._trading.submit_order,
            MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=entry_side,
                time_in_force=TimeInForce.GTC,
            ),
        )
        log.info(
            "Crypto entry submitted: %s %s qty=%s order_id=%s",
            ticker, side, qty, entry_order.id,
        )

        # Alpaca deducts fees (~0.25%) from the received crypto balance, so the
        # exit qty must be <= what was actually received. Use filled_qty if the
        # paper order settled immediately; otherwise apply a 0.25% fee haircut.
        raw_filled = getattr(entry_order, "filled_qty", None)
        filled_float = float(raw_filled) if raw_filled is not None else 0.0
        exit_qty = math.floor(filled_float * 1e8) / 1e8 if filled_float > 0 else math.floor(qty * 0.9975 * 1e8) / 1e8

        # 2. Stop-loss (hard stop-limit order on Alpaca — risk protection first).
        # NOTE: Alpaca locks the full crypto balance when any GTC exit order is
        # pending, so only ONE exit order can be live at a time. The stop-loss is
        # placed as the hard order; the take-profit is enforced softly by the
        # executor's tick loop (executor.on_tick checks price vs pos.target and
        # submits a market close when the target level is reached).
        dp = _price_decimals(stop_price)
        stop_rounded = round(stop_price, dp)
        stop_limit = round(stop_price * (0.999 if side == "long" else 1.001), dp)
        try:
            stop_order = await _run_in_executor(
                self._trading.submit_order,
                StopLimitOrderRequest(
                    symbol=ticker,
                    qty=exit_qty,
                    side=exit_side,
                    time_in_force=TimeInForce.GTC,
                    stop_price=stop_rounded,
                    limit_price=stop_limit,
                ),
            )
            _crypto_stop_orders[ticker] = str(stop_order.id)
            log.info("Crypto stop-loss submitted: %s stop=%.*f limit=%.*f qty=%.8f order_id=%s",
                     ticker, dp, stop_price, dp, stop_limit, exit_qty, stop_order.id)
        except Exception as e:
            log.error("Crypto stop-loss order failed for %s: %s", ticker, e)

        log.info(
            "Crypto take-profit for %s is soft (tick-based): target=%.4f "
            "enforced by executor on_tick",
            ticker, take_profit_price,
        )

        return entry_order

    async def submit_market_order(self, ticker: str, qty: int, side: str) -> object:
        """Submit a plain market order (used for EOD close-all)."""
        alpaca_side = OrderSide.SELL if side == "sell" else OrderSide.BUY
        tif = TimeInForce.GTC if is_crypto(ticker) else TimeInForce.DAY
        request = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=alpaca_side,
            time_in_force=tif,
        )
        order = await _run_in_executor(self._trading.submit_order, request)
        log.info("Market order submitted: %s %s qty=%d", ticker, side, qty)
        return order

    def get_bars(self, ticker: str, timeframe: str, limit: int) -> pd.DataFrame:
        """Fetch historical bars. Returns DataFrame with lowercase OHLCV columns and UTC DatetimeIndex."""
        tf_map = {
            "1Min": TimeFrame(1, TimeFrameUnit.Minute),
            "5Min": TimeFrame(5, TimeFrameUnit.Minute),
            "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
            "1Day": TimeFrame(1, TimeFrameUnit.Day),
        }
        tf = tf_map.get(timeframe, TimeFrame(1, TimeFrameUnit.Minute))

        end = datetime.now(tz=timezone.utc)
        # Crypto trades 24/7 so 2x is enough; equities need 10x to safely cover weekends/gaps
        multiplier = 2 if is_crypto(ticker) else 10
        start = end - timedelta(minutes=limit * multiplier)

        if is_crypto(ticker):
            request = CryptoBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=tf,
                start=start,
                end=end,
                limit=limit,
            )
            bars = self._crypto_data.get_crypto_bars(request)
        else:
            request = StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=tf,
                start=start,
                end=end,
                limit=limit,
                feed=DataFeed.IEX,
            )
            bars = self._data.get_stock_bars(request)

        df = bars.df

        _empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        if df is None or df.empty:
            log.debug("No bars returned for %s (market closed or no data)", ticker)
            return _empty

        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(ticker, level="symbol")

        if df.empty:
            return _empty

        df = df[["open", "high", "low", "close", "volume"]].copy()
        df.index = pd.to_datetime(df.index, utc=True)
        df = df.tail(limit)

        log.debug("Fetched %d bars for %s", len(df), ticker)
        return df

    async def is_tradable(self, ticker: str) -> bool:
        """Return True if the asset is active and tradable. Cached per session.

        Crypto tickers (e.g. BTC/USD) are always tradable 24/7 — skip the REST
        call entirely since the slash in the symbol breaks the URL path.
        """
        if is_crypto(ticker):
            return True
        if ticker in _asset_cache:
            return _asset_cache[ticker]
        try:
            asset = await _run_in_executor(self._trading.get_asset, ticker)
            result = bool(asset.tradable) and str(asset.status).lower() in ("active", "assetstatus.active")
        except Exception as e:
            log.warning("Could not check tradability for %s: %s", ticker, e)
            result = False
        _asset_cache[ticker] = result
        return result
