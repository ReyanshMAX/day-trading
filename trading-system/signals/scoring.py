"""Composite signal score computation.

Single responsibility: map an IndicatorSnapshot + RegimeState to a
deterministic float score in [-1.0, 1.0].
"""

import logging
from dataclasses import dataclass

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


@dataclass
class RegimeState:
    regime: str       # "trending" | "ranging" | "avoid"
    conviction: int   # 1–5
    direction: str    # "bullish" | "bearish" | "neutral"
    catalyst: str = ""
    avoid_reason: str | None = None


def compute_score(indicators: IndicatorSnapshot, regime: RegimeState) -> float | None:
    """Compute composite signal score. Returns None for avoid regime."""
    if regime.regime == "avoid":
        return None

    if regime.regime == "trending":
        return _score_trending(indicators, regime)
    elif regime.regime == "ranging":
        return _score_ranging(indicators, regime)
    else:
        log.warning("Unknown regime '%s', returning 0.0", regime.regime)
        return 0.0


def _score_trending(indicators: IndicatorSnapshot, regime: RegimeState) -> float:
    """Trending regime scoring. Positive = long bias, negative = short bias."""
    weights: list[tuple[float, bool]] = []

    # EMA crossover
    weights.append((0.25, indicators.ema_fast > indicators.ema_slow))
    # Price vs VWAP
    weights.append((0.20, indicators.current_price > indicators.vwap))
    # ORB breakout — only include when available
    if indicators.orb_high is not None:
        weights.append((0.15, indicators.current_price > indicators.orb_high))
        total_weight = 1.0
    else:
        total_weight = 0.85  # redistribute ORB weight proportionally
    # Volume confirmation
    weights.append((0.15, indicators.rvol > 1.5))
    # RSI momentum
    weights.append((0.10, 40 < indicators.rsi < 70))
    # MACD confirmation
    weights.append((0.15, indicators.macd_line > indicators.macd_signal))

    raw_score = sum(w for w, cond in weights if cond)

    # Renormalize if ORB was excluded
    if indicators.orb_high is None:
        raw_score = raw_score / total_weight

    # Apply direction: regime says bearish → negate
    if regime.direction == "bearish":
        raw_score = -raw_score

    return max(-1.0, min(1.0, raw_score))


def _score_ranging(indicators: IndicatorSnapshot, regime: RegimeState) -> float:
    """Ranging regime scoring — mean reversion bias."""
    score = 0.0
    total_w = 0.0

    # Price below VWAP - 1 std (mean reversion long)
    if indicators.vwap_std > 0:
        lower_band = indicators.vwap - 1.0 * indicators.vwap_std
        if indicators.current_price < lower_band:
            score += 0.35
    total_w += 0.35

    # RSI oversold
    if indicators.rsi < 35:
        score += 0.25
    total_w += 0.25

    # Volume confirmation
    if indicators.rvol > 1.2:
        score += 0.20
    total_w += 0.20

    # Near ORB support
    orb_weight = 0.20
    if indicators.orb_low is not None:
        near_support = abs(indicators.current_price - indicators.orb_low) / indicators.current_price < 0.005
        if near_support:
            score += orb_weight
        total_w += orb_weight

    if total_w > 0:
        score = score / total_w
    else:
        score = 0.0

    if regime.direction == "bearish":
        score = -score

    return max(-1.0, min(1.0, score))
