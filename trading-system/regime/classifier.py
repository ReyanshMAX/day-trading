"""LLM regime classifier using Groq.

Single responsibility: classify a stock's intraday regime from headlines
using Groq's API. All JSON validation happens here — never passes raw LLM
output downstream.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from groq import AsyncGroq

from core.config import Config
from core.utils import is_crypto
from memory.chroma_store import ChromaStore
from signals.scoring import RegimeState

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
Intraday regime classifier. Reply ONLY with valid JSON.

Regime definitions (use these exact thresholds when classifying):
- trending: clear directional price movement exceeding 0.5% in the last 2 hours \
with volume above the 20-period average, suggesting institutional participation
- ranging: price oscillating within a 0.3% band around VWAP with no sustained \
directional move and volume at or below average
- avoid: earnings announcement today or tomorrow, halt risk, ATR spiking more \
than 3x its 20-period average, major conflicting macro event, or insufficient liquidity"""

_USER_TEMPLATE = """\
Ticker: {ticker}
Asset class: {asset_class}
Time (ET): {time_et}
Day of week: {day_of_week}
Prior regime: {prior_regime}
Headlines: {headlines}
{vixy_note}\
{few_shot}
Respond with only this JSON object, no nesting:
{{"regime":"trending|ranging|avoid","direction":"bullish|bearish|neutral","conviction":1-5,"catalyst":"brief phrase","avoid_reason":null}}"""


def fallback_regime(prior_regime: "RegimeState | None" = None) -> RegimeState:
    if prior_regime is not None:
        return RegimeState(
            regime=prior_regime.regime,
            conviction=max(1, prior_regime.conviction - 1),
            direction=prior_regime.direction,
            catalyst="classifier error",
        )
    return RegimeState(
        regime="ranging",
        conviction=2,
        direction="neutral",
        catalyst="classifier error",
    )


class RegimeClassifier:
    """Classifies ticker regime via Groq LLM with few-shot ChromaDB context."""

    def __init__(self, config: Config, chroma: ChromaStore, broker: object | None = None, regime_store: object | None = None) -> None:
        self._client = AsyncGroq(api_key=config.groq_api_key)
        self._chroma = chroma
        self._groq_model = config.llm.groq_model
        self._cache_ttl_minutes = config.llm.cache_ttl_minutes
        self._broker = broker
        self._regime_store = regime_store

    async def classify(
        self,
        ticker: str,
        headlines: list[str],
        prior_regime: str | None = None,
    ) -> RegimeState:
        """Classify a ticker's regime. Never raises — falls back on any error."""
        # Cache check: skip Groq if same headlines classified < 10 min ago.
        joined = "; ".join(headlines[:5]) if headlines else ""
        current_hash = hashlib.md5(joined.encode()).hexdigest()
        if self._regime_store is not None:
            cached = self._regime_store.get(ticker)
            if (
                cached is not None
                and cached.last_classified_at is not None
                and cached.last_headlines_hash == current_hash
                and (datetime.now(timezone.utc) - cached.last_classified_at) < timedelta(minutes=self._cache_ttl_minutes)
            ):
                log.debug("classifier cache hit for %s", ticker)
                return cached

        few_shot_examples = self._chroma.get_similar_contexts(ticker, headlines, n=2)
        few_shot_block = ""
        if few_shot_examples:
            few_shot_block = "Prior: " + " | ".join(few_shot_examples) + "\n"

        # Current time in ET for market-aware context
        now_et = datetime.now(ZoneInfo("America/New_York"))
        time_et = now_et.strftime("%H:%M")
        day_of_week = now_et.strftime("%A")

        # Asset class for the model
        asset_class = "crypto" if is_crypto(ticker) else "equity"

        top_headlines = headlines[:5]
        headlines_str = "; ".join(top_headlines) if top_headlines else "none"

        # VIXY check — elevated VIX proxy biases toward avoid for equities
        vixy_note = ""
        if self._broker is not None and not is_crypto(ticker):
            try:
                vixy_df = self._broker.get_bars("VIXY", "1Min", 1)
                if vixy_df is not None and not vixy_df.empty:
                    vixy_price = float(vixy_df["close"].iloc[-1])
                    if vixy_price > 25:
                        vixy_note = (
                            f"Note: market volatility is currently elevated "
                            f"(VIXY={vixy_price:.2f}). Bias toward avoid for individual equities.\n"
                        )
            except Exception:
                pass  # fail silently per spec

        user_msg = _USER_TEMPLATE.format(
            ticker=ticker,
            asset_class=asset_class,
            time_et=time_et,
            day_of_week=day_of_week,
            headlines=headlines_str,
            prior_regime=prior_regime or "unknown",
            vixy_note=vixy_note,
            few_shot=few_shot_block,
        )

        raw = ""
        state: RegimeState | None = None
        try:
            response = await self._client.chat.completions.create(
                model=self._groq_model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.1,
                max_tokens=200,
            )
            usage = response.usage
            log.debug(
                "Groq tokens for %s: prompt=%d completion=%d",
                ticker, usage.prompt_tokens, usage.completion_tokens,
            )
            raw = response.choices[0].message.content.strip()
            data = json.loads(raw)
            assert data["regime"] in ("trending", "ranging", "avoid"), f"bad regime: {data['regime']}"
            assert 1 <= data["conviction"] <= 5, f"bad conviction: {data['conviction']}"
            assert data["direction"] in ("bullish", "bearish", "neutral"), f"bad direction: {data['direction']}"

            state = RegimeState(
                regime=data["regime"],
                conviction=int(data["conviction"]),
                direction=data["direction"],
                catalyst=str(data.get("catalyst", "")),
                avoid_reason=data.get("avoid_reason"),
                last_classified_at=datetime.now(timezone.utc),
                last_headlines_hash=current_hash,
            )

        except (json.JSONDecodeError, KeyError, AssertionError) as e:
            log.error("Classifier JSON invalid for %s: %s | raw: %s", ticker, e, raw)
            return fallback_regime()
        except Exception as e:
            log.error("Classifier error for %s: %s", ticker, e)
            return fallback_regime()

        try:
            self._chroma.store_classification(ticker, state, headlines)
        except Exception as e:
            log.error("ChromaDB store failed for %s: %s", ticker, e)

        return state
