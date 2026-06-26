"""LLM regime classifier using Groq.

Single responsibility: classify a stock's intraday regime from headlines
using Groq's API. All JSON validation happens here — never passes raw LLM
output downstream.
"""

import asyncio
import hashlib
import httpx
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

Regime definitions (judge from headlines and context only — do not apply numeric price thresholds):

TRENDING: Strong directional consensus across multiple headlines. Clear catalyst present \
(earnings beat, FDA approval, analyst upgrade, macro event, sector rotation language). \
Multiple independent sources confirming same direction. Institutional language ("buyout", \
"raised guidance", "record revenue", "massive inflows").

RANGING: Contradictory signals. Mixed analyst opinions. "Wait and see" or "uncertainty" \
language. No dominant catalyst. Price oscillating per recent reports. "Could go either way."

AVOID: Earnings release TODAY for this ticker. Fed announcement TODAY. Ticker explicitly \
mentioned as halted, suspended, or under SEC review. Extreme panic language ("flash crash", \
"circuit breaker", "emergency halt"). Analyst issuing "sell" or "avoid" rating TODAY. \
Extreme VIX/volatility event affecting the whole market."""

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
{{"regime":"trending|ranging|avoid","direction":"bullish|bearish|neutral","conviction":1-5,"catalyst":"brief phrase","avoid_reason":null,"reasoning":"one sentence explaining the classification"}}"""


def fallback_regime(prior_regime: "RegimeState | None" = None) -> RegimeState:
    if prior_regime is not None:
        return RegimeState(
            regime=prior_regime.regime,
            conviction=max(1, prior_regime.conviction - 1),
            direction=prior_regime.direction,
            catalyst="classifier error",
        )
    # No prior regime available (e.g. morning sweep with Groq down).
    # Default to conviction=3 so the signal engine can still evaluate tickers
    # rather than hard-blocking everything at the conviction gate.
    return RegimeState(
        regime="ranging",
        conviction=3,
        direction="neutral",
        catalyst="classifier error",
    )


class RegimeClassifier:
    """Classifies ticker regime via Groq LLM with few-shot ChromaDB context."""

    _PROBE_INTERVAL = 60  # seconds between liveness checks when circuit is open

    def __init__(self, config: Config, chroma: ChromaStore, broker: object | None = None, regime_store: object | None = None) -> None:
        self._client = AsyncGroq(api_key=config.groq_api_key)
        self._chroma = chroma
        self._groq_model = config.llm.groq_model
        self._cache_ttl_minutes = config.llm.cache_ttl_minutes
        self._broker = broker
        self._regime_store = regime_store
        # Circuit breaker: open when Groq is unreachable, probed every 60s.
        self._groq_down: bool = False
        self._last_probe_at: datetime | None = None
        self._probe_lock = asyncio.Lock()

    async def _probe_groq(self) -> bool:
        """Send a minimal request to check if Groq is reachable. Returns True on success."""
        try:
            await self._client.chat.completions.create(
                model=self._groq_model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            return True
        except Exception as e:
            log.debug("Groq probe failed: %s", e)
            return False

    async def _check_circuit(self) -> bool:
        """Return True if Groq is available (circuit closed). May trigger a probe."""
        if not self._groq_down:
            return True
        # Circuit is open — check if enough time has passed to probe again.
        async with self._probe_lock:
            if not self._groq_down:
                return True  # another coroutine already closed it
            now = datetime.now(timezone.utc)
            if self._last_probe_at is not None:
                elapsed = (now - self._last_probe_at).total_seconds()
                if elapsed < self._PROBE_INTERVAL:
                    return False
            self._last_probe_at = now
            log.info("Groq circuit open — probing liveness...")
            up = await self._probe_groq()
            if up:
                self._groq_down = False
                log.info("Groq is back online — circuit closed.")
            else:
                log.info("Groq still unreachable — next probe in %ds.", self._PROBE_INTERVAL)
            return up

    async def _classify_local(
        self,
        ticker: str,
        messages: list[dict],
        prior_regime: "RegimeState | None",
    ) -> "RegimeState":
        """Classify using local Ollama instance. Never raises."""
        try:
            import ollama
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: ollama.chat(
                    model="llama3.2:3b",
                    messages=messages,
                    format="json",
                ),
            )
            raw = response["message"]["content"].strip()
            data = json.loads(raw)
            assert data["regime"] in ("trending", "ranging", "avoid")
            assert 1 <= data["conviction"] <= 5
            assert data["direction"] in ("bullish", "bearish", "neutral")
            state = RegimeState(
                regime=data["regime"],
                conviction=int(data["conviction"]),
                direction=data["direction"],
                catalyst=str(data.get("catalyst", "")),
                avoid_reason=data.get("avoid_reason"),
                last_classified_at=datetime.now(timezone.utc),
            )
            log.info(
                "Ollama fallback classified %s: %s conviction=%d",
                ticker, state.regime, state.conviction,
            )
            return state
        except ImportError:
            log.debug("ollama package not installed — skipping local fallback")
            return fallback_regime(prior_regime)
        except Exception as e:
            log.error("Ollama fallback failed for %s: %s", ticker, e)
            return fallback_regime(prior_regime)

    async def classify(
        self,
        ticker: str,
        headlines: list[str],
        prior_regime: str | None = None,
    ) -> RegimeState:
        """Classify a ticker's regime. Never raises — falls back on any error.

        When Groq is unreachable the circuit breaker opens: all classify calls
        return the existing stored regime (or the default fallback) immediately
        without hitting the API. A lightweight probe fires every 60s; on success
        the circuit closes and normal classification resumes.
        """
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

        # Circuit breaker: if Groq is known-down, skip the API call entirely.
        if not await self._check_circuit():
            stored = self._regime_store.get(ticker) if self._regime_store is not None else None
            if stored is not None:
                log.debug("Groq circuit open — attempting Ollama fallback for %s", ticker)
            # Build minimal messages for local fallback (no few-shot, no VIXY).
            now_et_local = datetime.now(ZoneInfo("America/New_York"))
            asset_class_local = "crypto" if is_crypto(ticker) else "equity"
            headlines_str_local = "; ".join(headlines[:5]) if headlines else "none"
            prior_label = prior_regime if isinstance(prior_regime, str) else (stored.regime if stored is not None else "unknown")
            user_msg_local = _USER_TEMPLATE.format(
                ticker=ticker,
                asset_class=asset_class_local,
                time_et=now_et_local.strftime("%H:%M"),
                day_of_week=now_et_local.strftime("%A"),
                headlines=headlines_str_local,
                prior_regime=prior_label,
                vixy_note="",
                few_shot="",
            )
            local_messages = [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg_local},
            ]
            return await self._classify_local(ticker, local_messages, stored)

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
            response = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self._groq_model,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0.1,
                    max_tokens=200,
                ),
                timeout=30.0,
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

            reasoning = data.get("reasoning", "")
            if reasoning:
                log.debug("Classifier reasoning for %s: %s", ticker, reasoning)

            state = RegimeState(
                regime=data["regime"],
                conviction=int(data["conviction"]),
                direction=data["direction"],
                catalyst=str(data.get("catalyst", "")),
                avoid_reason=data.get("avoid_reason"),
                last_classified_at=datetime.now(timezone.utc),
                last_headlines_hash=current_hash,
            )

        except asyncio.TimeoutError:
            log.error("Groq API timeout for %s after 30s", ticker)
            return fallback_regime(prior_regime if isinstance(prior_regime, RegimeState) else None)
        except (json.JSONDecodeError, KeyError, AssertionError, ValueError) as e:
            log.error("Classifier JSON invalid for %s: %s | raw: %s", ticker, e, raw)
            return fallback_regime()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            # Network-level failures trip the circuit breaker.
            log.error("Classifier network error for %s: %s — opening circuit breaker", ticker, e)
            self._groq_down = True
            self._last_probe_at = datetime.now(timezone.utc)
            stored = self._regime_store.get(ticker) if self._regime_store is not None else None
            return stored if stored is not None else fallback_regime()
        except Exception as e:
            # Unexpected errors: log and return fallback but do NOT open circuit breaker.
            log.error("Classifier unexpected error for %s: %s", ticker, e)
            return fallback_regime()

        try:
            self._chroma.store_classification(ticker, state, headlines)
        except Exception as e:
            log.error("ChromaDB store failed for %s: %s", ticker, e)

        return state
