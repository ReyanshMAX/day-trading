"""Portfolio state tracker.

Single responsibility: track open positions, daily P&L, and risk exposure.
Does not call the broker directly — updated via record_fill/record_close callbacks.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SECTOR_MAP: dict[str, str] = {
    "NVDA": "tech", "AMD": "tech", "AAPL": "tech", "MSFT": "tech",
    "META": "tech", "GOOGL": "tech", "AMZN": "tech",
    "TSLA": "consumer", "COIN": "crypto", "MSTR": "crypto",
    "PLTR": "tech", "SOFI": "finance", "ARKK": "etf",
    "SPY": "broad_market", "QQQ": "tech_etf",
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
    stop_order_id: str | None = None  # GTC stop order ID for crypto positions
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    atr: float = 0.0  # ATR at entry time, used for trailing soft target increments
    current_soft_target: float = 0.0  # Trailing soft take-profit level; never moves against the position


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

    def reconcile_positions(self, broker_positions: list) -> None:
        """Compare local position state against broker-reported positions.

        Logs CRITICAL for any discrepancy — no state mutation.
        """
        broker_symbols: set[str] = {str(p.symbol) for p in broker_positions}
        local_symbols: set[str] = set(self.positions.keys())

        for ticker in local_symbols - broker_symbols:
            pos = self.positions[ticker]
            log.critical(
                "RECONCILE: ticker=%s is in local portfolio but NOT in broker positions "
                "(side=%s qty=%.4f entry=%.4f stop=%.4f target=%.4f) — "
                "possible missed fill or ghost position",
                ticker, pos.side, pos.qty, pos.avg_entry, pos.stop, pos.target,
            )

        for symbol in broker_symbols - local_symbols:
            log.critical(
                "RECONCILE: ticker=%s is in broker positions but NOT in local portfolio — "
                "possible untracked position or manual intervention",
                symbol,
            )

    def record_fill(self, order, stop: float = 0.0, target: float = 0.0, entry_price: float = 0.0, stop_order_id: str | None = None, entry_time: datetime | None = None, atr: float = 0.0) -> None:
        """Record a new position from an order fill.

        entry_price is used as a fallback when filled_avg_price is not yet
        populated (paper market orders return before fill confirmation).
        Using 0 as fallback would inflate open_risk by ~entry * qty and
        silently block all subsequent orders via the heat pre-check.

        atr is stored on the position so the executor can apply minimum trail
        increments (0.1 * atr) when updating current_soft_target on each tick.
        """
        try:
            ticker = str(order.symbol)
            qty = float(order.filled_qty or order.qty)
            side = "long" if str(order.side).lower() in ("buy", "orderside.buy") else "short"
            entry = float(order.filled_avg_price or order.limit_price or entry_price or 0.0)
            self.positions[ticker] = Position(
                ticker=ticker,
                qty=qty,
                avg_entry=entry,
                stop=stop,
                target=target,
                side=side,
                stop_order_id=stop_order_id,
                entry_time=entry_time if entry_time is not None else datetime.now(timezone.utc),
                atr=atr,
                current_soft_target=target,  # initialise to the original bracket target
            )
            log.info("Position opened: %s %s qty=%.4f entry=%.2f", ticker, side, qty, entry)
        except Exception as e:
            log.error("record_fill failed: %s", e)

    def record_close(self, order) -> float | None:
        """Remove a position and update daily P&L.

        Returns pnl_pct (pnl / nav at close time) so callers can update
        ChromaDB outcome records. Returns None if the exit price is missing
        or invalid (P&L is not updated in that case either).
        """
        try:
            ticker = str(order.symbol)
            if ticker not in self.positions:
                return None
            pos = self.positions.pop(ticker)
            raw_exit = order.filled_avg_price
            if not raw_exit:
                log.error(
                    "record_close: filled_avg_price is None/0 for %s order=%s — skipping P&L update",
                    ticker, getattr(order, "id", "unknown"),
                )
                return None
            exit_price = float(raw_exit)
            if exit_price == 0.0:
                log.error(
                    "record_close: filled_avg_price is 0 for %s order=%s — skipping P&L update",
                    ticker, getattr(order, "id", "unknown"),
                )
                return None
            if pos.side == "long":
                pnl = (exit_price - pos.avg_entry) * pos.qty
            else:
                pnl = (pos.avg_entry - exit_price) * pos.qty
            self.daily_pnl += pnl
            self.nav += pnl
            pnl_pct = pnl / self.nav if self.nav > 0 else 0.0
            log.info(
                "Position closed: %s pnl=%.2f pnl_pct=%.4f daily_pnl=%.2f nav=%.2f",
                ticker, pnl, pnl_pct, self.daily_pnl, self.nav,
            )
            return pnl_pct
        except Exception as e:
            log.error("record_close failed: %s", e)
            return None
