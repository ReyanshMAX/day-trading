"""Signal engine — called on every tick.

Single responsibility: coordinate bar_store, indicators, regime_store,
and scoring to produce a SignalResult or None per tick.
"""

import asyncio
import logging
from dataclasses import dataclass

from core.config import Config
from signals.bar_store import BarStore
from signals.indicators import ema, vwap, vwap_bands, atr, rsi, macd, rvol, orb
from signals.scoring import compute_score, IndicatorSnapshot, RegimeState
from regime.regime_store import RegimeStore

log = logging.getLogger(__name__)

_SCORE_LOG_INTERVAL = 30.0  # seconds between heartbeat logs per ticker


@dataclass
class SignalResult:
    ticker: str
    score: float
    direction: str
    atr: float
    regime: str
    conviction: int
    indicators: IndicatorSnapshot


class SignalEngine:
    """Processes ticks and produces signal results."""

    def __init__(self, config: Config, bar_store: BarStore, regime_store: RegimeStore) -> None:
        self._config = config
        self._bar_store = bar_store
        self._regime_store = regime_store

    def _compute(self, ticker: str, price: float) -> tuple[IndicatorSnapshot, float, RegimeState] | None:
        """Compute indicators and score for a ticker. Returns None if not ready."""
        df = self._bar_store.get_bars(ticker, 100)
        if len(df) < 20:
            return None

        regime_state = self._regime_store.get(ticker)
        if regime_state is None:
            regime_state = RegimeState(regime="ranging", conviction=2, direction="neutral", catalyst="no regime yet")

        if regime_state.regime == "avoid":
            return None
        if regime_state.conviction < self._config.regime.min_conviction_to_trade:
            return None

        cfg = self._config.signal
        ema_fast_val = float(ema(df, cfg.ema_fast).iloc[-1])
        ema_slow_val = float(ema(df, cfg.ema_slow).iloc[-1])
        vwap_val = float(vwap(df).iloc[-1])
        bands = vwap_bands(df, cfg.vwap_deviation_bands)
        std_key = f"-{cfg.vwap_deviation_bands[0]}"
        vwap_std = abs(vwap_val - float(bands[std_key].iloc[-1])) if std_key in bands else 0.0
        atr_val = atr(df, cfg.atr_period)
        rsi_val = rsi(df, cfg.rsi_period)
        macd_line, macd_signal, _ = macd(df)
        rvol_val = rvol(df)
        orb_high, orb_low = orb(df, cfg.orb_window_minutes)

        snapshot = IndicatorSnapshot(
            ema_fast=ema_fast_val,
            ema_slow=ema_slow_val,
            vwap=vwap_val,
            current_price=price,
            rsi=rsi_val,
            macd_line=macd_line,
            macd_signal=macd_signal,
            rvol=rvol_val,
            orb_high=orb_high,
            orb_low=orb_low,
            atr=atr_val,
            vwap_std=vwap_std,
        )

        score = compute_score(snapshot, regime_state)
        if score is None:
            return None

        return snapshot, score, regime_state

    def on_tick(self, ticker: str, price: float, volume: float, timestamp) -> SignalResult | None:
        """Process a single tick. Returns SignalResult or None."""
        self._bar_store.update(ticker, price, volume, timestamp)

        result = self._compute(ticker, price)
        if result is None:
            return None

        snapshot, score, regime_state = result

        if abs(score) < self._config.signal.entry_threshold:
            return None

        direction = "long" if score > 0 else "short"

        return SignalResult(
            ticker=ticker,
            score=score,
            direction=direction,
            atr=snapshot.atr,
            regime=regime_state.regime,
            conviction=regime_state.conviction,
            indicators=snapshot,
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
                log.info("%s — bars=%d (need 20 to score)", ticker, len(df))
                continue

            snapshot, score, regime_state = result
            cfg = self._config.signal
            log.info(
                "%s score=%.3f rsi=%.1f rvol=%.2f vwap=%.2f price=%.2f vwap_std=%.4f "
                "ema_fast=%.2f ema_slow=%.2f regime=%s conviction=%d threshold=%.2f",
                ticker, score, snapshot.rsi, snapshot.rvol, snapshot.vwap, price, snapshot.vwap_std,
                snapshot.ema_fast, snapshot.ema_slow,
                regime_state.regime, regime_state.conviction, cfg.entry_threshold,
            )
