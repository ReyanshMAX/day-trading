"""Signal engine — called on every tick.

Single responsibility: coordinate bar_store, indicators, regime_store,
and scoring to produce a SignalResult or None per tick.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import time as dtime

import pytz

from core.config import Config
from core.utils import is_crypto
from signals.bar_store import BarStore
from signals.indicators import ema, vwap, vwap_bands, atr, rsi, macd, rvol, orb as _orb_indicator
from signals.scoring import compute_score, IndicatorSnapshot, RegimeState
from signals import session_state
from regime.regime_store import RegimeStore

log = logging.getLogger(__name__)

_ET = pytz.timezone("America/New_York")
_ORB_LOCK_TIME = dtime(9, 45)  # ORB levels are locked at 9:45 AM ET

_SCORE_LOG_INTERVAL = 30.0  # seconds between heartbeat logs per ticker


def _resolve_orb(
    ticker: str,
    df,
    atr_val: float | None,
    window_minutes: int,
) -> tuple[float | None, float | None]:
    """Return ORB levels for ticker, applying session caching and quality filter.

    - Crypto tickers always return (None, None).
    - Before 9:45 AM ET, returns (None, None).
    - At or after 9:45 AM ET: compute once, cache via session_state, and apply
      the quality filter (suppress if range > 2x ATR).
    """
    if is_crypto(ticker):
        return None, None

    from datetime import datetime as _dt
    now_et = _dt.now(_ET).time()

    if now_et < _ORB_LOCK_TIME:
        return None, None

    # Already suppressed for this session
    if session_state.is_orb_suppressed(ticker):
        return None, None

    # Already cached for this session
    cached = session_state.get_orb(ticker)
    if cached is not None:
        return cached

    # Not yet cached — compute it now
    orb_high, orb_low = _orb_indicator(df, window_minutes)
    if orb_high is None or orb_low is None:
        return None, None

    # Quality filter: if range is > 2x ATR, suppress ORB for the session
    if atr_val is not None and (orb_high - orb_low) > 2.0 * atr_val:
        log.debug("%s ORB suppressed: range=%.4f > 2x ATR=%.4f", ticker, orb_high - orb_low, atr_val)
        session_state.suppress_orb(ticker)
        return None, None

    session_state.set_orb(ticker, orb_high, orb_low)
    log.debug("%s ORB locked: high=%.4f low=%.4f", ticker, orb_high, orb_low)
    return orb_high, orb_low
# Module-level defaults; actual values are read from config at runtime.
_MIN_BARS = 30              # mirrors config.signal.min_bars
_CONFIDENCE_THRESHOLD = 0.6  # mirrors config.signal.confidence_threshold
_EXPECTED_COMPONENTS = 8    # ema, vwap, vwap_bands, atr, rsi, macd, rvol, orb


@dataclass
class SignalResult:
    ticker: str
    score: float
    direction: str
    atr: float
    regime: str
    conviction: int
    indicators: IndicatorSnapshot
    confidence: float  # fraction of expected indicator components that returned non-None


class SignalEngine:
    """Processes ticks and produces signal results."""

    def __init__(self, config: Config, bar_store: BarStore, regime_store: RegimeStore) -> None:
        self._config = config
        self._bar_store = bar_store
        self._regime_store = regime_store

    def _compute(self, ticker: str, price: float) -> tuple[IndicatorSnapshot, float, RegimeState, float] | None:
        """Compute indicators and score for a ticker. Returns None if not ready.

        Returns (snapshot, score, regime_state, confidence) or None.
        """
        df = self._bar_store.get_bars(ticker, 100)

        # minimum bar guard — must be checked before any indicator calls
        if len(df) < self._config.signal.min_bars:
            return None

        regime_state = self._regime_store.get(ticker)
        if regime_state is None:
            regime_state = RegimeState(regime="ranging", conviction=2, direction="neutral", catalyst="no regime yet")

        if regime_state.regime == "avoid":
            return None
        if regime_state.conviction < self._config.regime.min_conviction_to_trade:
            return None

        cfg = self._config.signal

        # --- Compute all indicator components, tracking which returned non-None ---
        non_none_count = 0

        ema_series = ema(df, cfg.ema_fast)
        ema_fast_val = float(ema_series.iloc[-1]) if ema_series is not None else None
        if ema_fast_val is not None:
            non_none_count += 1

        ema_slow_series = ema(df, cfg.ema_slow)
        ema_slow_val = float(ema_slow_series.iloc[-1]) if ema_slow_series is not None else None
        # ema_fast and ema_slow are counted as one component (both or neither)
        # ema is already counted above; ema_slow uses the same slot

        vwap_series = vwap(df)
        vwap_val = float(vwap_series.iloc[-1]) if vwap_series is not None else None
        if vwap_val is not None:
            non_none_count += 1

        bands = vwap_bands(df, cfg.vwap_deviation_bands)
        std_key = f"-{cfg.vwap_deviation_bands[0]}"
        if bands is not None and std_key in bands and vwap_val is not None:
            vwap_std = abs(vwap_val - float(bands[std_key].iloc[-1]))
            non_none_count += 1
        else:
            vwap_std = 0.0

        # Compute band re-entry flags for mean-reversion confirmation.
        # Requires at least 2 bars and the 2.0-deviation bands to be present.
        prev_close_below_lower_band = False
        current_close_above_lower_band = False
        prev_close_above_upper_band = False
        current_close_below_upper_band = False
        if bands is not None and "-2.0" in bands and "+2.0" in bands and len(df) >= 2:
            lower_band_series = bands["-2.0"]
            upper_band_series = bands["+2.0"]
            # Last two bars' close prices and band values
            prev_close = float(df["close"].iloc[-2])
            curr_close = float(df["close"].iloc[-1])
            lower_prev = float(lower_band_series.iloc[-2])
            lower_curr = float(lower_band_series.iloc[-1])
            upper_prev = float(upper_band_series.iloc[-2])
            upper_curr = float(upper_band_series.iloc[-1])
            # Long re-entry: prev bar closed below -2.0 band, current bar closed above it
            prev_close_below_lower_band = prev_close < lower_prev
            current_close_above_lower_band = curr_close > lower_curr
            # Short re-entry: prev bar closed above +2.0 band, current bar closed below it
            prev_close_above_upper_band = prev_close > upper_prev
            current_close_below_upper_band = curr_close < upper_curr

        atr_val = atr(df, cfg.atr_period)
        if atr_val is not None:
            non_none_count += 1

        rsi_val = rsi(df, cfg.rsi_period)
        if rsi_val is not None:
            non_none_count += 1

        macd_line, macd_signal, _ = macd(df)
        if macd_line is not None:
            non_none_count += 1

        rvol_val = rvol(df)
        # rvol always returns a float (defaults to 1.0), so always count it
        non_none_count += 1

        orb_high, orb_low = _resolve_orb(ticker, df, atr_val, cfg.orb_window_minutes)
        if orb_high is not None:
            non_none_count += 1

        confidence = non_none_count / _EXPECTED_COMPONENTS

        log.debug(
            "%s confidence=%.2f (%d/%d components non-None)",
            ticker, confidence, non_none_count, _EXPECTED_COMPONENTS,
        )

        confidence_threshold = self._config.signal.confidence_threshold
        if confidence < confidence_threshold:
            log.debug("%s: confidence %.2f below threshold %.2f — signal suppressed", ticker, confidence, confidence_threshold)
            return None

        # Substitute safe defaults for None values so IndicatorSnapshot can be constructed.
        # These don't affect scoring since compute_score handles None orb and the scorer
        # reads ema_fast/ema_slow/vwap/rsi/macd_line/macd_signal as scalars.
        snapshot = IndicatorSnapshot(
            ema_fast=ema_fast_val if ema_fast_val is not None else price,
            ema_slow=ema_slow_val if ema_slow_val is not None else price,
            vwap=vwap_val if vwap_val is not None else price,
            current_price=price,
            rsi=rsi_val if rsi_val is not None else 50.0,
            macd_line=macd_line if macd_line is not None else 0.0,
            macd_signal=macd_signal if macd_signal is not None else 0.0,
            rvol=rvol_val,
            orb_high=orb_high,
            orb_low=orb_low,
            atr=atr_val if atr_val is not None else 0.0,
            vwap_std=vwap_std,
            prev_close_below_lower_band=prev_close_below_lower_band,
            current_close_above_lower_band=current_close_above_lower_band,
            prev_close_above_upper_band=prev_close_above_upper_band,
            current_close_below_upper_band=current_close_below_upper_band,
        )

        score = compute_score(snapshot, regime_state)
        if score is None:
            return None

        return snapshot, score, regime_state, confidence

    def get_bars(self, ticker: str, n: int):
        """Public accessor for bar data. Delegates to _bar_store."""
        return self._bar_store.get_bars(ticker, n)

    def on_tick(self, ticker: str, price: float, volume: float, timestamp) -> SignalResult | None:
        """Process a single tick. Returns SignalResult or None."""
        self._bar_store.update(ticker, price, volume, timestamp)

        result = self._compute(ticker, price)
        if result is None:
            return None

        snapshot, score, regime_state, confidence = result

        if abs(score) < self._config.signal.entry_threshold:
            return None

        direction = "long" if score > 0 else "short"

        log.debug(
            "%s signal: score=%.3f direction=%s confidence=%.2f regime=%s conviction=%d",
            ticker, score, direction, confidence, regime_state.regime, regime_state.conviction,
        )

        return SignalResult(
            ticker=ticker,
            score=score,
            direction=direction,
            atr=snapshot.atr,
            regime=regime_state.regime,
            conviction=regime_state.conviction,
            indicators=snapshot,
            confidence=confidence,
        )

    async def log_scores_loop(self, tickers: list[str]) -> None:
        """Log indicator snapshots for all tickers every _SCORE_LOG_INTERVAL seconds."""
        # Stagger startup so tickers don't all log at the same instant
        interval = _SCORE_LOG_INTERVAL
        stagger = interval / len(tickers) if tickers else interval

        for i, ticker in enumerate(tickers):
            await asyncio.sleep(stagger * i)
            asyncio.create_task(self._ticker_log_loop(ticker, interval))

        # Keep the coroutine alive
        await asyncio.Event().wait()

    async def _ticker_log_loop(self, ticker: str, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            price = self._bar_store.get_current_price(ticker)
            if price is None:
                log.info("%s — no price yet", ticker)
                continue

            result = self._compute(ticker, price)
            if result is None:
                df = self._bar_store.get_bars(ticker, 100)
                log.info("%s — bars=%d (need %d to score)", ticker, len(df), self._config.signal.min_bars)
                continue

            snapshot, score, regime_state, confidence = result
            cfg = self._config.signal
            log.info(
                "%s score=%.3f confidence=%.2f rsi=%.1f rvol=%.2f vwap=%.2f price=%.2f vwap_std=%.4f "
                "ema_fast=%.2f ema_slow=%.2f regime=%s conviction=%d threshold=%.2f",
                ticker, score, confidence, snapshot.rsi, snapshot.rvol, snapshot.vwap, price, snapshot.vwap_std,
                snapshot.ema_fast, snapshot.ema_slow,
                regime_state.regime, regime_state.conviction, cfg.entry_threshold,
            )
