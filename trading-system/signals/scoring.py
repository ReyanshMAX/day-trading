"""Composite signal score computation.

Single responsibility: map an IndicatorSnapshot + RegimeState to a
deterministic float score in [-1.0, 1.0].
"""

import logging
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger(__name__)


@dataclass
class IndicatorSnapshot:
    ema_fast: float
    ema_slow: float
    vwap: float
    current_price: float
    rsi: float
    macd_line: float
    macd_signal: float
    rvol: float
    orb_high: float | None
    orb_low: float | None
    atr: float
    vwap_std: float = 0.0  # standard deviation of (typical_price - vwap)
    # Mean-reversion band re-entry flags (long side)
    prev_close_below_lower_band: bool = False   # prev bar closed below -2.0 VWAP band
    current_close_above_lower_band: bool = False  # current bar close is above -2.0 VWAP band
    # Mean-reversion band re-entry flags (short side)
    prev_close_above_upper_band: bool = False   # prev bar closed above +2.0 VWAP band
    current_close_below_upper_band: bool = False  # current bar close is back below +2.0 VWAP band


@dataclass
class RegimeState:
    regime: str       # "trending" | "ranging" | "avoid"
    conviction: int   # 1–5
    direction: str    # "bullish" | "bearish" | "neutral"
    catalyst: str = ""
    avoid_reason: str | None = None
    last_classified_at: datetime | None = None
    last_headlines_hash: str = ""


def compute_score(
    indicators: IndicatorSnapshot,
    regime: RegimeState,
    confidence: float = 1.0,
) -> float | None:
    """Compute composite signal score. Returns None for avoid regime."""
    if regime.regime == "avoid":
        log.debug("compute_score: avoid regime, returning None (confidence=%.2f)", confidence)
        return None

    if regime.regime == "trending":
        score = _score_trending(indicators, regime)
    elif regime.regime == "ranging":
        score = _score_ranging(indicators, regime)
    else:
        log.warning("Unknown regime '%s', returning 0.0", regime.regime)
        score = 0.0

    log.debug(
        "compute_score: regime=%s direction=%s score=%.4f confidence=%.2f",
        regime.regime,
        regime.direction,
        score,
        confidence,
    )
    return score


def _weighted_sum(pairs: list[tuple[float, float | None]]) -> float:
    """Compute a normalized weighted sum, excluding None-valued components.

    Args:
        pairs: list of (weight, value) where value is 0.0/1.0 or None.
               None means the component is unavailable and its weight is
               redistributed proportionally across remaining components.

    Returns:
        Weighted average over non-None components, in [0.0, 1.0].
        Returns 0.0 if all components are None.
    """
    active = [(w, v) for w, v in pairs if v is not None]
    if not active:
        return 0.0
    total_weight = sum(w for w, _ in active)
    if total_weight <= 0.0:
        return 0.0
    return sum(w * v for w, v in active) / total_weight


def _score_trending(indicators: IndicatorSnapshot, regime: RegimeState) -> float:
    """Trending regime scoring. Positive = long bias, negative = short bias.

    Weights (sum to 1.0 when all present):
        ema_trend=0.25, vwap_pos=0.20, orb=0.15, macd=0.15, rsi=0.10, rvol=0.15
    """
    # ORB is None when pre-market or window not elapsed — excluded from sum.
    orb_value: float | None = None
    if indicators.orb_high is not None:
        orb_value = 1.0 if indicators.current_price > indicators.orb_high else 0.0

    pairs: list[tuple[float, float | None]] = [
        (0.25, 1.0 if indicators.ema_fast > indicators.ema_slow else 0.0),
        (0.20, 1.0 if indicators.current_price > indicators.vwap else 0.0),
        (0.15, orb_value),
        (0.15, 1.0 if indicators.macd_line > indicators.macd_signal else 0.0),
        (0.10, 1.0 if 40 < indicators.rsi < 70 else 0.0),
        (0.15, 1.0 if indicators.rvol > 1.5 else 0.0),
    ]

    raw_score = _weighted_sum(pairs)

    # Apply direction: regime says bearish → negate
    if regime.direction == "bearish":
        raw_score = -raw_score

    return max(-1.0, min(1.0, raw_score))


def _score_ranging(indicators: IndicatorSnapshot, regime: RegimeState) -> float:
    """Ranging regime scoring — mean reversion bias.

    Bullish/neutral: score long-bias conditions (oversold).
    Bearish: score short-bias conditions (overbought) and return as negative.

    Weights (sum to 1.0 when all present):
        vwap_band=0.35, rsi=0.25, orb_proximity=0.20, rvol=0.20
    """
    if regime.direction == "bearish":
        raw_score = _score_ranging_short(indicators)
        return max(-1.0, min(1.0, -raw_score))
    else:
        raw_score = _score_ranging_long(indicators)
        return max(-1.0, min(1.0, raw_score))


def _score_ranging_long(indicators: IndicatorSnapshot) -> float:
    """Long-bias ranging score (mean reversion from oversold).

    VWAP band component fires only when price has re-entered the -2.0 band:
    previous bar was below the -2.0 band AND current bar is above it.
    """
    # vwap_band: None when vwap_std is unavailable (zero → no std computed yet)
    # Use the -2.0 standard deviation band (not -1.0) as the trigger threshold.
    # Additionally require band re-entry confirmation: prev bar below, current bar above.
    vwap_band_value: float | None = None
    if indicators.vwap_std > 0:
        if indicators.prev_close_below_lower_band and indicators.current_close_above_lower_band:
            vwap_band_value = 1.0
        else:
            vwap_band_value = None  # no contribution when re-entry not confirmed

    # orb_proximity: None when ORB not yet established
    orb_prox_value: float | None = None
    if indicators.orb_low is not None:
        near_support = (
            abs(indicators.current_price - indicators.orb_low) / indicators.current_price < 0.005
        )
        orb_prox_value = 1.0 if near_support else 0.0

    pairs: list[tuple[float, float | None]] = [
        (0.35, vwap_band_value),
        (0.25, 1.0 if indicators.rsi < 35 else 0.0),
        (0.20, orb_prox_value),
        (0.20, 1.0 if indicators.rvol > 1.2 else 0.0),
    ]

    return _weighted_sum(pairs)


def _score_ranging_short(indicators: IndicatorSnapshot) -> float:
    """Short-bias ranging score (mean reversion from overbought).

    VWAP band component fires only when price has re-entered the +2.0 band:
    previous bar was above the +2.0 band AND current bar is back below it.
    """
    # vwap_band: None when vwap_std is unavailable
    # Use the +2.0 standard deviation band (not +1.0) as the trigger threshold.
    # Additionally require band re-entry confirmation: prev bar above, current bar below.
    vwap_band_value: float | None = None
    if indicators.vwap_std > 0:
        if indicators.prev_close_above_upper_band and indicators.current_close_below_upper_band:
            vwap_band_value = 1.0
        else:
            vwap_band_value = None  # no contribution when re-entry not confirmed

    # orb_proximity: None when ORB not yet established
    orb_prox_value: float | None = None
    if indicators.orb_high is not None:
        near_resistance = (
            abs(indicators.current_price - indicators.orb_high) / indicators.current_price < 0.005
        )
        orb_prox_value = 1.0 if near_resistance else 0.0

    pairs: list[tuple[float, float | None]] = [
        (0.35, vwap_band_value),
        (0.25, 1.0 if indicators.rsi > 65 else 0.0),
        (0.20, orb_prox_value),
        (0.20, 1.0 if indicators.rvol > 1.2 else 0.0),
    ]

    return _weighted_sum(pairs)
