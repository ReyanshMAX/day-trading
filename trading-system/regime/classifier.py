"""LLM regime classifier using Groq.

Single responsibility: classify a stock's intraday regime from headlines
using Groq's API. All JSON validation happens here — never passes raw LLM
output downstream.
"""

import json
import logging
from datetime import datetime, timezone

from groq import AsyncGroq

from core.config import Config
from memory.chroma_store import ChromaStore
from signals.scoring import RegimeState

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = "Intraday regime classifier. Reply ONLY with valid JSON."

_USER_TEMPLATE = """\
Ticker: {ticker}
Time: {time}
Prior regime: {prior_regime}
Headlines: {headlines}
{few_shot}
Respond with only this JSON object, no nesting:
{{"regime":"trending|ranging|avoid","direction":"bullish|bearish|neutral","conviction":1-5,"catalyst":"brief phrase","avoid_reason":null}}"""


def fallback_regime() -> RegimeState:
    return RegimeState(
        regime="ranging",
        conviction=2,
        direction="neutral",
        catalyst="classifier error",
    )


class RegimeClassifier:
    """Classifies ticker regime via Groq LLM with few-shot ChromaDB context."""

    def __init__(self, config: Config, chroma: ChromaStore) -> None:
        self._client = AsyncGroq(api_key=config.groq_api_key)
        self._chroma = chroma

    async def classify(
        self,
        ticker: str,
        headlines: list[str],
        prior_regime: str | None = None,
    ) -> RegimeState:
        """Classify a ticker's regime. Never raises — falls back on any error."""
        few_shot_examples = self._chroma.get_similar_contexts(ticker, headlines, n=2)
        few_shot_block = ""
        if few_shot_examples:
            few_shot_block = "Prior: " + " | ".join(few_shot_examples) + "\n"

        now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        top_headlines = headlines[:5]
        headlines_str = "; ".join(top_headlines) if top_headlines else "none"

        user_msg = _USER_TEMPLATE.format(
            ticker=ticker,
            time=now,
            headlines=headlines_str,
            prior_regime=prior_regime or "unknown",
            few_shot=few_shot_block,
        )

        raw = ""
        try:
            response = await self._client.chat.completions.create(
                model="llama-3.1-8b-instant",
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
            )
            self._chroma.store_classification(ticker, state, headlines)
            return state

        except (json.JSONDecodeError, KeyError, AssertionError) as e:
            log.error("Classifier JSON invalid for %s: %s | raw: %s", ticker, e, raw)
            return fallback_regime()
        except Exception as e:
            log.error("Classifier error for %s: %s", ticker, e)
            return fallback_regime()
