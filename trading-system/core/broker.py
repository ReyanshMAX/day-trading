"""Alpaca REST client wrapper.

Single responsibility: all communication with the Alpaca paper trading API.
No signal logic, no risk logic — pure I/O.
"""

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, OrderClass, StopLossRequest, TakeProfitRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from core.config import Config

log = logging.getLogger(__name__)

_asset_cache: dict[str, bool] = {}


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

    def submit_bracket_order(
        self,
        ticker: str,
        qty: int,
        side: str,
        stop_price: float,
        take_profit_price: float,
    ):
        """Submit a bracket order (market entry with stop-loss and take-profit legs)."""
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

    async def submit_market_order(self, ticker: str, qty: int, side: str):
        """Submit a plain market order (used for EOD close-all)."""
        alpaca_side = OrderSide.SELL if side == "sell" else OrderSide.BUY
        request = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=alpaca_side,
            time_in_force=TimeInForce.DAY,
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
        # Fetch extra to account for non-trading periods
        start = end - timedelta(minutes=limit * 3)

        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=tf,
            start=start,
            end=end,
            limit=limit,
        )
        bars = self._data.get_stock_bars(request)
        df = bars.df

        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(ticker, level="symbol")

        df = df.rename(columns={
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "vwap": "vwap",
        })
        df = df[["open", "high", "low", "close", "volume"]].copy()
        df.index = pd.to_datetime(df.index, utc=True)
        df = df.tail(limit)

        log.debug("Fetched %d bars for %s", len(df), ticker)
        return df

    async def is_tradable(self, ticker: str) -> bool:
        """Return True if the asset is active and tradable. Cached per session."""
        if ticker in _asset_cache:
            return _asset_cache[ticker]
        try:
            asset = self._trading.get_asset(ticker)
            result = bool(asset.tradable) and asset.status.value == "active"
        except Exception as e:
            log.warning("Could not check tradability for %s: %s", ticker, e)
            result = False
        _asset_cache[ticker] = result
        return result
