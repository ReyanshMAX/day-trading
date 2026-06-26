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
    set_loss_limit: bool = False


def check(
    ticker: str,
    direction: str,
    qty: int,
    stop_distance: float,
    portfolio: Portfolio,
    config: Config,
    atr: float = 0.0,
) -> GateResult:
    """Run all six risk checks in order. Returns GateResult.

    Pure function — no side effects. If the daily loss limit is newly breached,
    set_loss_limit=True is returned so the caller can apply the mutation.
    """
    import datetime

    now = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()

    # 1. Daily loss limit flag already set
    if portfolio.daily_loss_limit_hit:
        log.warning("%s | %s | REJECT: daily loss limit flag set", now, ticker)
        return GateResult(approved=False, reason="daily loss limit")

    # 2. Daily P&L crosses -3% threshold
    if portfolio.daily_pnl_pct() < -config.risk.daily_loss_limit_pct:
        log.warning(
            "%s | %s | REJECT: daily pnl %.2f%% crossed limit -%.0f%%",
            now, ticker, portfolio.daily_pnl_pct() * 100, config.risk.daily_loss_limit_pct * 100,
        )
        return GateResult(approved=False, reason="daily loss limit", set_loss_limit=True)

    # 3. Portfolio heat
    if portfolio.open_risk_pct() >= config.risk.max_portfolio_heat_pct:
        log.info(
            "%s | %s | REJECT: portfolio heat %.2f%% >= %.0f%%",
            now, ticker, portfolio.open_risk_pct() * 100, config.risk.max_portfolio_heat_pct * 100,
        )
        return GateResult(approved=False, reason="portfolio heat")

    # 4. Trade risk
    trade_risk = qty * stop_distance
    # Guard: non-positive NAV means we cannot safely size any trade
    if portfolio.nav <= 0:
        log.warning(
            "%s | %s | REJECT: NAV is non-positive (%.2f) — trading halted",
            now, ticker, portfolio.nav,
        )
        return GateResult(approved=False, reason="NAV is non-positive — trading halted")
    if (trade_risk / portfolio.nav) > config.risk.max_trade_risk_pct:
        log.info(
            "%s | %s | REJECT: trade risk %.2f%% > %.0f%%",
            now, ticker, (trade_risk / portfolio.nav) * 100, config.risk.max_trade_risk_pct * 100,
        )
        return GateResult(approved=False, reason="trade risk")

    # 5. Sector concentration
    sector = SECTOR_MAP.get(ticker, "other")
    if portfolio.sector_count(sector) >= config.risk.max_sector_positions:
        log.info(
            "%s | %s | REJECT: sector '%s' at max %d positions",
            now, ticker, sector, config.risk.max_sector_positions,
        )
        return GateResult(approved=False, reason="sector concentration")

    # 6. Short stop width guard
    if direction == "short" and atr > 0 and stop_distance > 2.0 * atr:
        log.info(
            "%s | %s | REJECT: short stop too wide — stop_dist=%.4f > 2x ATR=%.4f",
            now, ticker, stop_distance, atr,
        )
        return GateResult(approved=False, reason="short stop too wide")

    log.info("%s | %s | APPROVE: direction=%s qty=%d stop_dist=%.2f", now, ticker, direction, qty, stop_distance)
    return GateResult(approved=True, reason=None)
