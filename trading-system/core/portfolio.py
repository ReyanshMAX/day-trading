"""Portfolio state tracker.

Single responsibility: track open positions, daily P&L, and risk exposure.
Does not call the broker directly — updated via record_fill/record_close callbacks.
"""

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

SECTOR_MAP: dict[str, str] = {
    "NVDA": "tech", "AMD": "tech", "AAPL": "tech", "MSFT": "tech",
    "META": "tech", "GOOGL": "tech", "AMZN": "tech",
    "TSLA": "consumer", "COIN": "crypto", "MSTR": "crypto",
    "PLTR": "tech", "SOFI": "finance", "ARKK": "etf",
    "SPY": "etf", "QQQ": "etf",
    "BTC/USD": "crypto", "ETH/USD": "crypto",
    "SOL/USD": "crypto"
}


@dataclass
class Position:
    ticker: str
    qty: float   # float to support fractional crypto units
    avg_entry: float
    stop: float
    target: float
    side: str  # "long" | "short"


class Portfolio:
    """Tracks open positions and daily P&L state."""

    def __init__(self, broker=None, nav: float = 100_000.0) -> None:
        self._broker = broker
        self.nav: float = nav
        self.positions: dict[str, Position] = {}
        self.daily_pnl: float = 0.0
        self.daily_loss_limit_hit: bool = False

    def has_position(self, ticker: str) -> bool:
        return ticker in self.positions

    def open_risk(self) -> float:
        """Sum of abs(avg_entry - stop) * qty across all open positions."""
        total = 0.0
        for pos in self.positions.values():
            total += abs(pos.avg_entry - pos.stop) * pos.qty
        return total

    def open_risk_pct(self) -> float:
        return self.open_risk() / self.nav if self.nav > 0 else 0.0

    def daily_pnl_pct(self) -> float:
        return self.daily_pnl / self.nav if self.nav > 0 else 0.0

    def sector_count(self, sector: str) -> int:
        count = 0
        for ticker in self.positions:
            if SECTOR_MAP.get(ticker, "other") == sector:
                count += 1
        return count

    def record_fill(self, order, stop: float = 0.0, target: float = 0.0, entry_price: float = 0.0) -> None:
        """Record a new position from an order fill.

        entry_price is used as a fallback when filled_avg_price is not yet
        populated (paper market orders return before fill confirmation).
        Using 0 as fallback would inflate open_risk by ~entry * qty and
        silently block all subsequent orders via the heat pre-check.
        """
        try:
            ticker = str(order.symbol)
            qty = float(order.qty)
            side = "long" if str(order.side).lower() in ("buy", "orderside.buy") else "short"
            entry = float(order.filled_avg_price or order.limit_price or entry_price or 0.0)
            self.positions[ticker] = Position(
                ticker=ticker,
                qty=qty,
                avg_entry=entry,
                stop=stop,
                target=target,
                side=side,
            )
            log.info("Position opened: %s %s qty=%d entry=%.2f", ticker, side, qty, entry)
        except Exception as e:
            log.error("record_fill failed: %s", e)

    def record_close(self, order) -> None:
        """Remove a position and update daily P&L."""
        try:
            ticker = str(order.symbol)
            if ticker not in self.positions:
                return
            pos = self.positions.pop(ticker)
            exit_price = float(order.filled_avg_price or 0.0)
            if pos.side == "long":
                pnl = (exit_price - pos.avg_entry) * pos.qty
            else:
                pnl = (pos.avg_entry - exit_price) * pos.qty
            self.daily_pnl += pnl
            log.info(
                "Position closed: %s pnl=%.2f daily_pnl=%.2f",
                ticker, pnl, self.daily_pnl,
            )
        except Exception as e:
            log.error("record_close failed: %s", e)
