"""Finnhub news client for intraday stock/crypto news.

Provides categorized headlines with sentiment scores.
Falls back to empty list on any error.
"""

import logging
from datetime import date

log = logging.getLogger(__name__)


class FinnhubNewsClient:
    """Wraps Finnhub's company_news and news_sentiment endpoints."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client = None
        if api_key:
            try:
                import finnhub
                self._client = finnhub.Client(api_key=api_key)
            except ImportError:
                log.warning("finnhub-python not installed — Finnhub news unavailable")

    def fetch_news(self, ticker: str) -> list[dict]:
        """Return today's headlines for ticker with sentiment scores.

        Returns list of dicts with keys: headline, summary, sentiment, category.
        Returns empty list on any error or if finnhub not installed.
        """
        if self._client is None:
            return []
        # Finnhub uses plain symbol names (no slash) — convert BTC/USD -> BTCUSD
        symbol = ticker.replace("/", "")
        try:
            today = date.today().isoformat()
            articles = self._client.company_news(symbol, _from=today, to=today)
            return [
                {
                    "headline": a.get("headline", ""),
                    "summary": a.get("summary", ""),
                    "sentiment": a.get("sentiment", 0.0),
                    "category": a.get("category", ""),
                }
                for a in articles[:10]
                if a.get("headline")
            ]
        except Exception as e:
            log.debug("Finnhub fetch_news failed for %s: %s", ticker, e)
            return []

    def get_aggregate_sentiment(self, ticker: str) -> float | None:
        """Return aggregate sentiment score (-1 to 1) for ticker, or None on failure."""
        if self._client is None:
            return None
        symbol = ticker.replace("/", "")
        try:
            sentiment_data = self._client.news_sentiment(symbol)
            buzz = sentiment_data.get("buzz", {})
            return float(buzz.get("sentiment", 0.0))
        except Exception as e:
            log.debug("Finnhub sentiment failed for %s: %s", ticker, e)
            return None
