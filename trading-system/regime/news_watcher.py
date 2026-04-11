"""News headline poller with hash-based change detection.

Single responsibility: poll Alpaca News API for each ticker, detect new
headlines via MD5 hashing, and trigger regime classification only on changes.
"""

import asyncio
import hashlib
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

from core.config import Config
from core.portfolio import SECTOR_MAP
from regime.classifier import RegimeClassifier
from regime.regime_store import RegimeStore

log = logging.getLogger(__name__)


class NewsWatcher:
    """Polls Alpaca News and triggers regime classification on new headlines."""

    def __init__(self, config: Config, classifier: RegimeClassifier, regime_store: RegimeStore) -> None:
        self._config = config
        self._classifier = classifier
        self._regime_store = regime_store
        self._news_client = NewsClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
        )
        self._hashes: dict[str, set[str]] = {t: set() for t in config.universe.tickers}

    def _fetch_headlines(self, ticker: str) -> list[str]:
        """Fetch recent headlines for a ticker from Alpaca News."""
        try:
            request = NewsRequest(symbols=ticker, limit=10)
            news = self._news_client.get_news(request)
            articles = news.news if hasattr(news, "news") else []
            return [a.headline for a in articles if a.headline]
        except Exception as e:
            log.warning("Failed to fetch news for %s: %s", ticker, e)
            return []

    def _md5(self, text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()

    @staticmethod
    def _is_regular_trading_hours() -> bool:
        """Return True if current ET time is within 9:30–16:00 Mon–Fri."""
        now = datetime.now(tz=ZoneInfo("America/New_York"))
        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        return time(9, 30) <= now.time() < time(16, 0)

    def _active_tickers(self) -> list[str]:
        """Return all tickers during market hours; crypto-only otherwise."""
        if self._is_regular_trading_hours():
            return self._config.universe.tickers
        crypto = [t for t in self._config.universe.tickers if SECTOR_MAP.get(t) == "crypto"]
        log.debug("Outside trading hours — polling crypto tickers only: %s", crypto)
        return crypto

    async def _process_ticker(self, ticker: str) -> None:
        """Fetch headlines, check for new ones, trigger classify if needed."""
        headlines = self._fetch_headlines(ticker)
        new_found = False
        for h in headlines:
            h_hash = self._md5(h)
            if h_hash not in self._hashes[ticker]:
                self._hashes[ticker].add(h_hash)
                new_found = True

        if new_found:
            log.info("New headlines detected for %s, triggering classifier", ticker)
            prior = self._regime_store.get(ticker)
            prior_regime_str = prior.regime if prior else None
            state = await self._classifier.classify(ticker, headlines, prior_regime_str)
            self._regime_store.set(ticker, state)

    async def run_morning_sweep(self) -> None:
        """Classify all tickers concurrently at market open."""
        log.info("Running morning regime sweep for %d tickers", len(self._config.universe.tickers))
        tasks = [self._classify_for_morning(t) for t in self._config.universe.tickers]
        await asyncio.gather(*tasks)
        log.info("Morning sweep complete")

    async def _classify_for_morning(self, ticker: str) -> None:
        headlines = self._fetch_headlines(ticker)
        for h in headlines:
            self._hashes[ticker].add(self._md5(h))
        prior = self._regime_store.get(ticker)
        prior_regime_str = prior.regime if prior else None
        state = await self._classifier.classify(ticker, headlines, prior_regime_str)
        self._regime_store.set(ticker, state)

    async def watch(self) -> None:
        """Continuous news polling loop."""
        interval = self._config.regime.news_poll_interval_seconds
        while True:
            for ticker in self._active_tickers():
                await self._process_ticker(ticker)
            await asyncio.sleep(interval)
