"""News headline poller with hash-based change detection.

Single responsibility: poll Alpaca News API for each ticker, detect new
headlines via MD5 hashing, and trigger regime classification only on changes.
"""

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, time as time_type
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

from core.config import Config
from core.portfolio import SECTOR_MAP
from regime.classifier import RegimeClassifier
from regime.finnhub_news import FinnhubNewsClient
from regime.regime_store import RegimeStore

log = logging.getLogger(__name__)

_HASH_FILE = Path("logs/news_hashes.json")
_HASH_TTL_SECONDS = 86400  # 24 hours


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
        finnhub_key = getattr(config, "finnhub_api_key", "") or ""
        self._finnhub = FinnhubNewsClient(api_key=finnhub_key)
        # Load persisted timestamps (hash -> unix_timestamp), TTL=24h
        self._hash_timestamps: dict[str, dict[str, float]] = self._load_hashes()
        # Build set view for O(1) lookup
        self._hashes: dict[str, set[str]] = {
            t: set(self._hash_timestamps.get(t, {}).keys())
            for t in config.universe.tickers
        }
        # Limit concurrent Alpaca API calls to stay within the connection pool size (10).
        # Each ticker can trigger a news fetch + VIXY bar fetch simultaneously.
        self._sem = asyncio.Semaphore(5)

    def _load_hashes(self) -> dict[str, dict[str, float]]:
        """Load persisted hash->timestamp map, filtering out entries older than 24h."""
        if not _HASH_FILE.exists():
            return {}
        try:
            raw = json.loads(_HASH_FILE.read_text())
            now = time.time()
            return {
                ticker: {h: ts for h, ts in entries.items() if now - ts < _HASH_TTL_SECONDS}
                for ticker, entries in raw.items()
            }
        except Exception as e:
            log.warning("Failed to load news hashes from disk: %s", e)
            return {}

    def _save_hashes(self) -> None:
        """Persist current hash->timestamp map to disk."""
        try:
            _HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
            raw = {
                ticker: {h: ts for h, ts in entries.items()}
                for ticker, entries in self._hash_timestamps.items()
            }
            _HASH_FILE.write_text(json.dumps(raw))
        except Exception as e:
            log.warning("Failed to save news hashes to disk: %s", e)

    async def _fetch_headlines(self, ticker: str) -> list[str]:
        """Fetch headlines: Finnhub primary, Alpaca fallback. Each source has a 10s timeout."""
        loop = asyncio.get_event_loop()

        # Finnhub (synchronous client — run in executor)
        try:
            articles = await asyncio.wait_for(
                loop.run_in_executor(None, self._finnhub.fetch_news, ticker),
                timeout=10.0,
            )
            if articles:
                headlines = [a["headline"] for a in articles if a.get("headline")]
                if headlines:
                    try:
                        sentiment = await asyncio.wait_for(
                            loop.run_in_executor(None, self._finnhub.get_aggregate_sentiment, ticker),
                            timeout=10.0,
                        )
                        if sentiment is not None:
                            log.debug("Finnhub sentiment for %s: %.2f", ticker, sentiment)
                    except Exception:
                        pass
                    return headlines
        except Exception as e:
            log.debug("Finnhub primary failed for %s: %s", ticker, e)

        # Alpaca fallback
        try:
            request = NewsRequest(symbols=ticker, limit=10)
            news = await asyncio.wait_for(
                loop.run_in_executor(None, self._news_client.get_news, request),
                timeout=10.0,
            )
            articles = news.news if hasattr(news, "news") else []
            return [a.headline for a in articles if a.headline]
        except Exception as e:
            log.warning("Failed to fetch news for %s (both sources): %s", ticker, e)
            return []

    def _md5(self, text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()

    @staticmethod
    def _is_regular_trading_hours() -> bool:
        """Return True if current ET time is within 9:30-16:00 Mon-Fri."""
        now = datetime.now(tz=ZoneInfo("America/New_York"))
        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        return time_type(9, 30) <= now.time() < time_type(16, 0)

    async def _is_market_open_today(self) -> bool:
        """Check Alpaca calendar -- returns True if today is a trading day."""
        try:
            loop = asyncio.get_event_loop()
            broker = getattr(self._classifier, '_broker', None)
            if broker is None:
                return self._is_regular_trading_hours()  # fallback to weekday check
            calendar = await loop.run_in_executor(None, broker.get_market_calendar)
            return len(calendar) > 0
        except Exception as e:
            log.warning("Market calendar check failed: %s -- falling back to weekday check", e)
            return self._is_regular_trading_hours()

    def _active_tickers(self) -> list[str]:
        """Return all tickers during market hours; crypto-only otherwise."""
        if self._is_regular_trading_hours():
            return self._config.universe.tickers
        crypto = [t for t in self._config.universe.tickers if SECTOR_MAP.get(t) == "crypto"]
        log.debug("Outside trading hours -- polling crypto tickers only: %s", crypto)
        return crypto

    async def _process_ticker(self, ticker: str) -> None:
        """Fetch headlines, check for new ones, trigger classify if needed."""
        async with self._sem:
            headlines = await self._fetch_headlines(ticker)
            new_found = False
            for h in headlines:
                h_hash = self._md5(h)
                if h_hash not in self._hashes[ticker]:
                    self._hashes[ticker].add(h_hash)
                    if ticker not in self._hash_timestamps:
                        self._hash_timestamps[ticker] = {}
                    self._hash_timestamps[ticker][h_hash] = time.time()
                    new_found = True

            if new_found:
                log.info("New headlines detected for %s, triggering classifier", ticker)
                prior = self._regime_store.get(ticker)
                prior_regime_str = prior.regime if prior else None
                state = await self._classifier.classify(ticker, headlines, prior_regime_str)
                self._regime_store.set(ticker, state)
                self._save_hashes()

    async def run_morning_sweep(self) -> None:
        """Classify all tickers concurrently at market open."""
        log.info("Running morning regime sweep for %d tickers", len(self._config.universe.tickers))

        async def _safe_morning(ticker: str) -> None:
            try:
                await asyncio.wait_for(self._classify_for_morning(ticker), timeout=45.0)
            except asyncio.TimeoutError:
                log.warning("Morning sweep timed out for %s — skipping", ticker)
            except Exception as e:
                log.error("Morning sweep error for %s: %s", ticker, e)

        tasks = [_safe_morning(t) for t in self._config.universe.tickers]
        await asyncio.gather(*tasks)
        self._save_hashes()
        log.info("Morning sweep complete")

    async def _classify_for_morning(self, ticker: str) -> None:
        async with self._sem:
            headlines = await self._fetch_headlines(ticker)
            for h in headlines:
                h_hash = self._md5(h)
                self._hashes[ticker].add(h_hash)
                if ticker not in self._hash_timestamps:
                    self._hash_timestamps[ticker] = {}
                self._hash_timestamps[ticker][h_hash] = time.time()
            prior = self._regime_store.get(ticker)
            prior_regime_str = prior.regime if prior else None
            state = await self._classifier.classify(ticker, headlines, prior_regime_str)
            self._regime_store.set(ticker, state)

    async def watch(self) -> None:
        """Continuous news polling loop. All tickers processed in parallel each pass."""
        interval = self._config.regime.news_poll_interval_seconds
        while True:
            is_open = await self._is_market_open_today()
            active = (
                self._config.universe.tickers
                if is_open and self._is_regular_trading_hours()
                else [t for t in self._config.universe.tickers if SECTOR_MAP.get(t) == "crypto"]
            )
            async def _safe_process(ticker: str):
                try:
                    await asyncio.wait_for(self._process_ticker(ticker), timeout=45.0)
                except asyncio.TimeoutError:
                    log.warning("watch() timed out for %s — skipping this pass", ticker)
                except Exception as e:
                    log.error("watch() error for %s: %s", ticker, e)

            await asyncio.gather(*[_safe_process(t) for t in active])
            await asyncio.sleep(interval)
