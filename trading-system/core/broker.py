"""Alpaca REST client wrapper.

Single responsibility: all communication with the Alpaca paper trading API.
No signal logic, no risk logic — pure I/O.
"""

import logging
import math
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Order
from alpaca.trading.requests import (
    MarketOrderRequest, StopLimitOrderRequest,
    OrderClass, StopLossRequest, TakeProfitRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data import DataFeed

from core.config import Config

log = logging.getLogger(__name__)

_asset_cache: dict[str, bool] = {}


def _is_crypto(ticker: str) -> bool:
    """Return True if ticker is a crypto pair (e.g. BTC/USD, ETHUSD)."""
    return "/" in ticker or ticker.endswith("USD") and len(ticker) > 4


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

    def get_account(self) -> dict:
        """Return NAV, buying power, and cash from the paper account."""
        acct = self._trading.get_account()
        return {
            "nav": float(acct.portfolio_value),
            "buying_power": float(acct.buying_power),
            "cash": float(acct.cash),
        }

    def get_positions(self) -> list:
        """Return all open positions."""
        return self._trading.get_all_positions()

    def get_open_orders(self) -> list:
        """Return all open orders."""
        return self._trading.get_orders()

    def cancel_order(self, order_id: str) -> None:
        """Cancel an order by ID."""
        self._trading.cancel_order_by_id(order_id)
        log.info("Cancelled order %s", order_id)

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
        if _is_crypto(ticker):
            return self._submit_crypto_orders(ticker, qty, side, stop_price, take_profit_price)

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
        order = self._trading.submit_order(request)
        log.info(
            "Bracket order submitted: %s %s qty=%d stop=%.2f target=%.2f order_id=%s",
            ticker, side, qty, stop_price, take_profit_price, order.id,
        )
        return order

    def _submit_crypto_orders(
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
        entry_order = self._trading.submit_order(MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=entry_side,
            time_in_force=TimeInForce.GTC,
        ))
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

        # 2. Stop-loss (stop-limit with 0.1% limit buffer below stop)
        # NOTE: Alpaca locks the full crypto balance when any GTC exit order is
        # pending. Submitting both stop-loss AND take-profit simultaneously causes
        # the second order to fail with "insufficient balance". Only the stop-loss
        # is submitted here (risk protection first). The take-profit is handled by
        # the EOD close-all sweep or future fill-event monitoring.
        dp = _price_decimals(stop_price)
        stop_rounded = round(stop_price, dp)
        stop_limit = round(stop_price * (0.999 if side == "long" else 1.001), dp)
        try:
            self._trading.submit_order(StopLimitOrderRequest(
                symbol=ticker,
                qty=exit_qty,
                side=exit_side,
                time_in_force=TimeInForce.GTC,
                stop_price=stop_rounded,
                limit_price=stop_limit,
            ))
            log.info("Crypto stop-loss submitted: %s stop=%.*f limit=%.*f qty=%.8f",
                     ticker, dp, stop_price, dp, stop_limit, exit_qty)
        except Exception as e:
            log.error("Crypto stop-loss order failed for %s: %s", ticker, e)

        log.info(
            "Crypto take-profit skipped for %s (target=%.4f): Alpaca locks balance on "
            "pending stop-loss; EOD sweep will close at market",
            ticker, take_profit_price,
        )

        return entry_order

    async def submit_market_order(self, ticker: str, qty: int, side: str) -> None:
        """Submit a plain market order (used for EOD close-all)."""
        alpaca_side = OrderSide.SELL if side == "sell" else OrderSide.BUY
        tif = TimeInForce.GTC if _is_crypto(ticker) else TimeInForce.DAY
        request = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=alpaca_side,
            time_in_force=tif,
        )
        order = self._trading.submit_order(request)
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
        # Crypto trades 24/7 so 1.5x is enough; equities need 3x to skip non-trading gaps
        multiplier = 2 if _is_crypto(ticker) else 3
        start = end - timedelta(minutes=limit * multiplier)

        if _is_crypto(ticker):
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
        if _is_crypto(ticker):
            return True
        if ticker in _asset_cache:
            return _asset_cache[ticker]
        try:
            asset = self._trading.get_asset(ticker)
            result = bool(asset.tradable) and str(asset.status).lower() in ("active", "assetstatus.active")
        except Exception as e:
            log.warning("Could not check tradability for %s: %s", ticker, e)
            result = False
        _asset_cache[ticker] = result
        return result
