"""Signal engine — called on every tick.

Single responsibility: coordinate bar_store, indicators, regime_store,
and scoring to produce a SignalResult or None per tick.
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, time as dtime

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
        # Maps ticker -> (last_bar_timestamp, snapshot, confidence)
        self._indicator_cache: dict[str, tuple[datetime, IndicatorSnapshot, float]] = {}

    def _compute_indicators(self, ticker: str, df, price: float) -> tuple[IndicatorSnapshot, float] | None:
        """Compute all indicator values for a ticker. Returns (snapshot, confidence) or None if suppressed."""
        cfg = self._config.signal

        non_none_count = 0

        ema_fast_series = ema(df, cfg.ema_fast)
        ema_slow_series = ema(df, cfg.ema_slow)
        if ema_fast_series is not None and ema_slow_series is not None:
            ema_fast_val = float(ema_fast_series.iloc[-1])
            ema_slow_val = float(ema_slow_series.iloc[-1])
            non_none_count += 1
        else:
            ema_fast_val = None
            ema_slow_val = None

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

        prev_close_below_lower_band = False
        current_close_above_lower_band = False
        prev_close_above_upper_band = False
        current_close_below_upper_band = False
        if bands is not None and "-2.0" in bands and "+2.0" in bands and len(df) >= 2:
            lower_band_series = bands["-2.0"]
            upper_band_series = bands["+2.0"]
            prev_close = float(df["close"].iloc[-2])
            curr_close = float(df["close"].iloc[-1])
            lower_prev = float(lower_band_series.iloc[-2])
            lower_curr = float(lower_band_series.iloc[-1])
            upper_prev = float(upper_band_series.iloc[-2])
            upper_curr = float(upper_band_series.iloc[-1])
            prev_close_below_lower_band = prev_close < lower_prev
            current_close_above_lower_band = curr_close > lower_curr
            prev_close_above_upper_band = prev_close > upper_prev
            current_close_below_upper_band = curr_close < upper_curr

        atr_val = atr(df, cfg.atr_period)
        if atr_val is not None:
            non_none_count += 1

        # Suppress signal if current ATR is spiking far above its recent baseline.
        if atr_val is not None and len(df) >= 40:
            atr_baseline = atr(df.iloc[-40:-20], cfg.atr_period)
            try:
                atr_spike_mult = float(getattr(cfg, "atr_spike_multiplier", 3.0))
            except (TypeError, ValueError):
                atr_spike_mult = 3.0
            if atr_baseline is not None and atr_baseline > 0 and atr_val > atr_spike_mult * atr_baseline:
                log.info(
                    "%s: ATR spike detected (%.4f > %.1fx %.4f) — signal suppressed",
                    ticker, atr_val, atr_spike_mult, atr_baseline,
                )
                return None

        rsi_val = rsi(df, cfg.rsi_period)
        if rsi_val is not None:
            non_none_count += 1

        macd_line, macd_signal, _ = macd(df)
        if macd_line is not None:
            non_none_count += 1

        rvol_val = rvol(df)
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

        snap = IndicatorSnapshot(
            ema_fast=ema_fast_val if ema_fast_val is not None else price,
            ema_slow=ema_slow_val if ema_slow_val is not None else price,
            vwap=vwap_val if vwap_val is not None else price,
            current_price=price,
            rsi=rsi_val,
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
        return snap, confidence

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
            min_conv = self._config.regime.min_conviction_to_trade
            regime_state = RegimeState(regime="ranging", conviction=min_conv, direction="neutral", catalyst="no regime yet")
            log.debug("%s: no regime classification yet — using fallback ranging/conviction=%d", ticker, min_conv)

        if regime_state.regime == "avoid":
            return None
        if regime_state.conviction < self._config.regime.min_conviction_to_trade:
            log.debug("%s: conviction=%d below min=%d — signal suppressed", ticker, regime_state.conviction, self._config.regime.min_conviction_to_trade)
            return None

        last_bar_ts = df.index[-1]
        if ticker in self._indicator_cache:
            cached_ts, cached_snap, cached_confidence = self._indicator_cache[ticker]
            if cached_ts == last_bar_ts:
                snap = replace(cached_snap, current_price=price)
                confidence = cached_confidence
            else:
                result = self._compute_indicators(ticker, df, price)
                if result is None:
                    return None
                snap, confidence = result
                self._indicator_cache[ticker] = (last_bar_ts, snap, confidence)
        else:
            result = self._compute_indicators(ticker, df, price)
            if result is None:
                return None
            snap, confidence = result
            self._indicator_cache[ticker] = (last_bar_ts, snap, confidence)

        score = compute_score(snap, regime_state)
        if score is None:
            return None

        return snap, score, regime_state, confidence

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
                "%s score=%.3f confidence=%.2f rsi=%s rvol=%.2f vwap=%.2f price=%.2f vwap_std=%.4f "
                "ema_fast=%.2f ema_slow=%.2f regime=%s conviction=%d threshold=%.2f",
                ticker, score, confidence,
                "%.1f" % snapshot.rsi if snapshot.rsi is not None else "None",
                snapshot.rvol, snapshot.vwap, price, snapshot.vwap_std,
                snapshot.ema_fast, snapshot.ema_slow,
                regime_state.regime, regime_state.conviction, cfg.entry_threshold,
            )
