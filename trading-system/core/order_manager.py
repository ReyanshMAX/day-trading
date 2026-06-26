"""Bracket order construction and position sizing.

Single responsibility: compute stop, target, and quantity from signal/ATR/config.
Pure functions — no I/O, no broker calls.
"""

import logging
import math
from dataclasses import dataclass

from core.config import Config
from core.utils import is_crypto

log = logging.getLogger(__name__)


@dataclass
class BracketParams:
    qty: float   # int for equities, fractional float for crypto
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
    """Risk-based position size. Always returns at least 1.

    stop_distance should already include estimated slippage when passed from build_bracket.
    """
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
        nav: float | None = None,
        asset_class: str | None = None,
    ) -> BracketParams:
        """Compute stop, target, and qty for a bracket order.

        Args:
            nav: Live portfolio NAV in dollars. Falls back to config.account.nav if not provided.
            asset_class: "equity" or "crypto". If None, derived from ticker via is_crypto().
                         Used to compute slippage estimate added to stop distance for sizing:
                         0.05% of entry for equities, 0.1% for crypto.

        For long: stop < entry < target.
        For short: target < entry < stop.
        Raises ValueError if bracket invariant is violated or size_multiplier is 0.0.
        """
        # Resolve NAV — live portfolio value takes precedence over static config value.
        effective_nav = nav if nav is not None else self._config.account.nav

        profile = self._config.rr_profiles.get(regime)
        if profile is None:
            log.warning("No RR profile for regime '%s', using ranging", regime)
            profile = self._config.rr_profiles["ranging"]

        size_mult = profile.size_multiplier_by_conviction.get(conviction, 0.5)

        # A multiplier of 0.0 means the config explicitly forbids trading this
        # conviction level in this regime (e.g. ranging + conviction=1).
        # Raise so the executor's try/except catches it and skips the order.
        if size_mult == 0.0:
            raise ValueError(
                f"{ticker}: size_multiplier is 0.0 for regime='{regime}' conviction={conviction} — skip trade"
            )

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

        # Fibonacci snapping — only apply if the snapped value preserves the direction invariant.
        # Log a WARNING when the snap widens the stop distance.
        if fib_levels:
            retracements = list(fib_levels.get("retracements", {}).values())
            extensions = list(fib_levels.get("extensions", {}).values())
            if direction == "long":
                snapped_stop = snap_to_fib(raw_stop, retracements)
                if snapped_stop < entry:
                    atr_stop_dist = abs(entry - raw_stop)
                    snapped_stop_dist = abs(entry - snapped_stop)
                    if snapped_stop_dist > atr_stop_dist:
                        log.warning(
                            "%s fib snap widened stop: ATR-based=%.4f snapped=%.4f "
                            "(raw_stop=%.4f snapped_stop=%.4f)",
                            ticker, atr_stop_dist, snapped_stop_dist, raw_stop, snapped_stop,
                        )
                    raw_stop = snapped_stop
                snapped_target = snap_to_fib(raw_target, extensions)
                raw_target = snapped_target if snapped_target > entry else raw_target
            else:
                snapped_stop = snap_to_fib(raw_stop, extensions)
                if snapped_stop > entry:
                    atr_stop_dist = abs(entry - raw_stop)
                    snapped_stop_dist = abs(entry - snapped_stop)
                    if snapped_stop_dist > atr_stop_dist:
                        log.warning(
                            "%s fib snap widened stop: ATR-based=%.4f snapped=%.4f "
                            "(raw_stop=%.4f snapped_stop=%.4f)",
                            ticker, atr_stop_dist, snapped_stop_dist, raw_stop, snapped_stop,
                        )
                    raw_stop = snapped_stop
                snapped_target = snap_to_fib(raw_target, retracements)
                raw_target = snapped_target if snapped_target < entry else raw_target

        # Recompute actual stop distance after potential snap
        actual_stop_dist = abs(entry - raw_stop)

        # Slippage model: widen stop distance used for sizing to account for
        # expected fill slippage. 0.1% for crypto, 0.05% for equities.
        # This does NOT move the actual stop price — only affects position sizing
        # so we don't over-size when slippage eats into the risk budget.
        resolved_asset_class = asset_class if asset_class is not None else ("crypto" if is_crypto(ticker) else "equity")
        slippage_pct = 0.001 if resolved_asset_class == "crypto" else 0.0005
        slippage_amount = entry * slippage_pct
        adjusted_stop_dist = actual_stop_dist + slippage_amount

        max_notional = effective_nav * self._config.risk.max_position_pct

        if is_crypto(ticker):
            # Crypto supports fractional units — size in 4 decimal places (e.g. 0.0137 BTC)
            risk_dollars = effective_nav * self._config.risk.max_trade_risk_pct
            raw_qty = (risk_dollars / adjusted_stop_dist) * size_mult if adjusted_stop_dist > 0 else 0
            qty_cap = max_notional / entry
            # Crypto positions are sized at 50% due to wider ATR relative to equities
            qty_float = round(min(raw_qty, qty_cap) * 0.5, 4)
            if qty_float <= 0:
                raise ValueError(
                    f"{ticker} fractional size resolved to 0 "
                    f"(stop_dist={adjusted_stop_dist:.4f}, max_notional=${max_notional:.0f})"
                )
            qty = qty_float
        else:
            base_qty = compute_base_size(
                effective_nav,
                adjusted_stop_dist,
                self._config.risk.max_trade_risk_pct,
            )
            qty = max(1, int(base_qty * size_mult))
            max_qty_by_notional = math.floor(max_notional / entry)
            if max_qty_by_notional < 1:
                raise ValueError(
                    f"{ticker} unit price ${entry:.2f} exceeds max notional "
                    f"${max_notional:.0f} ({self._config.risk.max_position_pct:.0%} of NAV) — skipping"
                )
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
            "%s %s qty=%s entry=%s stop=%s target=%s atr=%.5f mult=%.2f nav=%.0f",
            ticker, direction, qty,
            f"{entry:.5f}".rstrip("0").rstrip("."),
            f"{raw_stop:.5f}".rstrip("0").rstrip("."),
            f"{raw_target:.5f}".rstrip("0").rstrip("."),
            atr, size_mult, effective_nav,
        )

        return BracketParams(
            qty=qty,
            stop=raw_stop,
            target=raw_target,
            stop_distance=actual_stop_dist,
        )
