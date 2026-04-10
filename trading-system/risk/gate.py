"""Risk gate — hard-coded pre-trade checks.

Single responsibility: approve or reject a proposed trade based on portfolio
state and config limits. Pure function — no state, no I/O, no side effects.
"""

import logging
from dataclasses import dataclass

from core.portfolio import Portfolio, SECTOR_MAP
from core.config import Config

log = logging.getLogger(__name__)


@dataclass
class GateResult:
    approved: bool
    reason: str | None


def check(
    ticker: str,
    direction: str,
    qty: int,
    stop_distance: float,
    portfolio: Portfolio,
    config: Config,
) -> GateResult:
    """Run all five risk checks in order. Returns GateResult."""
    import datetime

    now = datetime.datetime.now().isoformat()

    # 1. Daily loss limit flag already set
    if portfolio.daily_loss_limit_hit:
        log.warning("%s | %s | REJECT: daily loss limit flag set", now, ticker)
        return GateResult(approved=False, reason="daily loss limit")

    # 2. Daily P&L crosses -3% threshold
    if portfolio.daily_pnl_pct() < -config.risk.daily_loss_limit_pct:
        portfolio.daily_loss_limit_hit = True
        log.warning(
            "%s | %s | REJECT: daily pnl %.2f%% crossed limit -%.0f%%",
            now, ticker, portfolio.daily_pnl_pct() * 100, config.risk.daily_loss_limit_pct * 100,
        )
        return GateResult(approved=False, reason="daily loss limit")

    # 3. Portfolio heat
    if portfolio.open_risk_pct() >= config.risk.max_portfolio_heat_pct:
        log.warning(
            "%s | %s | REJECT: portfolio heat %.2f%% >= %.0f%%",
            now, ticker, portfolio.open_risk_pct() * 100, config.risk.max_portfolio_heat_pct * 100,
        )
        return GateResult(approved=False, reason="portfolio heat")

    # 4. Trade risk
    trade_risk = qty * stop_distance
    if portfolio.nav > 0 and (trade_risk / portfolio.nav) > config.risk.max_trade_risk_pct:
        log.warning(
            "%s | %s | REJECT: trade risk %.2f%% > %.0f%%",
            now, ticker, (trade_risk / portfolio.nav) * 100, config.risk.max_trade_risk_pct * 100,
        )
        return GateResult(approved=False, reason="trade risk")

    # 5. Sector concentration
    sector = SECTOR_MAP.get(ticker, "other")
    if portfolio.sector_count(sector) >= config.risk.max_sector_positions:
        log.warning(
            "%s | %s | REJECT: sector '%s' at max %d positions",
            now, ticker, sector, config.risk.max_sector_positions,
        )
        return GateResult(approved=False, reason="sector concentration")

    log.info("%s | %s | APPROVE: direction=%s qty=%d stop_dist=%.2f", now, ticker, direction, qty, stop_distance)
    return GateResult(approved=True, reason=None)
