"""Bracket order construction and position sizing.

Single responsibility: compute stop, target, and quantity from signal/ATR/config.
Pure functions — no I/O, no broker calls.
"""

import logging
import math
from dataclasses import dataclass

from core.config import Config

log = logging.getLogger(__name__)


@dataclass
class BracketParams:
    qty: int
    stop: float
    target: float
    stop_distance: float


def snap_to_fib(price: float, fib_levels: list[float], tolerance_pct: float = 0.003) -> float:
    """Snap price to nearest Fibonacci level if within tolerance_pct."""
    for level in fib_levels:
        if abs(price - level) / price < tolerance_pct:
            return level
    return price


def compute_base_size(nav: float, stop_distance: float, max_risk_pct: float) -> int:
    """Risk-based position size. Always returns at least 1."""
    if stop_distance <= 0:
        log.warning("stop_distance <= 0, defaulting qty to 1")
        return 1
    risk_dollars = nav * max_risk_pct
    qty = math.floor(risk_dollars / stop_distance)
    if qty < 1:
        log.warning("ATR-derived qty=0 (stop_dist=%.4f), defaulting to 1", stop_distance)
        return 1
    return qty


class OrderManager:
    """Constructs bracket order parameters from signal data and config."""

    def __init__(self, config: Config) -> None:
        self._config = config

    def build_bracket(
        self,
        ticker: str,
        signal_score: float,
        regime: str,
        conviction: int,
        atr: float,
        current_price: float,
        fib_levels: dict | None = None,
    ) -> BracketParams:
        """Compute stop, target, and qty for a bracket order.

        For long: stop < entry < target.
        For short: target < entry < stop.
        Raises ValueError if invariant is violated.
        """
        profile = self._config.rr_profiles.get(regime)
        if profile is None:
            log.warning("No RR profile for regime '%s', using ranging", regime)
            profile = self._config.rr_profiles["ranging"]

        size_mult = profile.size_multiplier_by_conviction.get(conviction, 0.5)
        stop_dist = profile.stop_atr_mult * atr
        target_dist = profile.target_atr_mult * atr

        direction = "long" if signal_score > 0 else "short"
        entry = current_price

        if direction == "long":
            raw_stop = entry - stop_dist
            raw_target = entry + target_dist
        else:
            raw_stop = entry + stop_dist
            raw_target = entry - target_dist

        # Fibonacci snapping — only apply if the snapped value preserves the direction invariant
        if fib_levels:
            retracements = list(fib_levels.get("retracements", {}).values())
            extensions = list(fib_levels.get("extensions", {}).values())
            if direction == "long":
                snapped_stop = snap_to_fib(raw_stop, retracements)
                snapped_target = snap_to_fib(raw_target, extensions)
                raw_stop = snapped_stop if snapped_stop < entry else raw_stop
                raw_target = snapped_target if snapped_target > entry else raw_target
            else:
                snapped_stop = snap_to_fib(raw_stop, extensions)
                snapped_target = snap_to_fib(raw_target, retracements)
                raw_stop = snapped_stop if snapped_stop > entry else raw_stop
                raw_target = snapped_target if snapped_target < entry else raw_target

        # Recompute actual stop distance after potential snap
        actual_stop_dist = abs(entry - raw_stop)
        base_qty = compute_base_size(
            self._config.account.nav,
            actual_stop_dist,
            self._config.risk.max_trade_risk_pct,
        )
        qty = max(1, int(base_qty * size_mult))

        # Hard cap: notional value must not exceed max_position_pct of NAV.
        # Without this, a tiny ATR-derived stop produces a massive share count.
        max_notional = self._config.account.nav * self._config.risk.max_position_pct
        max_qty_by_notional = max(1, math.floor(max_notional / entry))
        if qty > max_qty_by_notional:
            log.warning(
                "%s qty capped by notional: %d -> %d (entry=%.2f max_notional=%.0f)",
                ticker, qty, max_qty_by_notional, entry, max_notional,
            )
            qty = max_qty_by_notional

        # Validate bracket invariant
        if direction == "long":
            if not (raw_stop < entry < raw_target):
                raise ValueError(
                    f"Bracket invariant violated (long): stop={raw_stop:.2f} entry={entry:.2f} target={raw_target:.2f}"
                )
        else:
            if not (raw_target < entry < raw_stop):
                raise ValueError(
                    f"Bracket invariant violated (short): target={raw_target:.2f} entry={entry:.2f} stop={raw_stop:.2f}"
                )

        log.info(
            "%s %s qty=%d entry=%.2f stop=%.2f target=%.2f atr=%.4f mult=%.2f",
            ticker, direction, qty, entry, raw_stop, raw_target, atr, size_mult,
        )

        return BracketParams(
            qty=qty,
            stop=raw_stop,
            target=raw_target,
            stop_distance=actual_stop_dist,
        )
