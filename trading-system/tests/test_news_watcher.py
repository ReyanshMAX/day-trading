"""Unit tests for regime/news_watcher.py.

Mocks _fetch_headlines and RegimeClassifier. Tests hash deduplication.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from regime.news_watcher import NewsWatcher
from regime.regime_store import RegimeStore
from signals.scoring import RegimeState


def make_config(tickers=None):
    config = MagicMock()
    config.universe.tickers = tickers or ["NVDA", "AAPL"]
    config.regime.news_poll_interval_seconds = 120
    config.alpaca_api_key = "test"
    config.alpaca_secret_key = "test"
    return config


def make_watcher(config, classifier, regime_store, fetch_side_effect=None):
    """Build a NewsWatcher with _fetch_headlines mocked."""
    watcher = NewsWatcher.__new__(NewsWatcher)
    watcher._config = config
    watcher._classifier = classifier
    watcher._regime_store = regime_store
    watcher._news_client = MagicMock()
    watcher._hashes = {t: set() for t in config.universe.tickers}
    if fetch_side_effect is not None:
        watcher._fetch_headlines = MagicMock(side_effect=fetch_side_effect)
    return watcher


@pytest.mark.asyncio
async def test_first_poll_triggers_classifier_and_stores_hashes():
    config = make_config(["NVDA"])
    classifier = MagicMock()
    dummy_state = RegimeState(regime="trending", conviction=4, direction="bullish")
    classifier.classify = AsyncMock(return_value=dummy_state)
    regime_store = RegimeStore()

    watcher = make_watcher(config, classifier, regime_store)
    watcher._fetch_headlines = MagicMock(return_value=["Headline A", "Headline B", "Headline C"])

    await watcher._process_ticker("NVDA")

    classifier.classify.assert_called_once()
    assert len(watcher._hashes["NVDA"]) == 3


@pytest.mark.asyncio
async def test_second_poll_same_headlines_does_not_trigger_classifier():
    config = make_config(["NVDA"])
    classifier = MagicMock()
    dummy_state = RegimeState(regime="trending", conviction=4, direction="bullish")
    classifier.classify = AsyncMock(return_value=dummy_state)
    regime_store = RegimeStore()

    watcher = make_watcher(config, classifier, regime_store)
    watcher._fetch_headlines = MagicMock(return_value=["Headline A", "Headline B", "Headline C"])

    await watcher._process_ticker("NVDA")  # first poll
    await watcher._process_ticker("NVDA")  # second poll — same headlines

    assert classifier.classify.call_count == 1


@pytest.mark.asyncio
async def test_new_headline_in_second_poll_triggers_classifier():
    config = make_config(["NVDA"])
    classifier = MagicMock()
    dummy_state = RegimeState(regime="trending", conviction=4, direction="bullish")
    classifier.classify = AsyncMock(return_value=dummy_state)
    regime_store = RegimeStore()

    watcher = make_watcher(config, classifier, regime_store)
    responses = [
        ["Headline A", "Headline B"],
        ["Headline A", "Headline B", "New Headline D"],
    ]
    watcher._fetch_headlines = MagicMock(side_effect=responses)

    await watcher._process_ticker("NVDA")
    await watcher._process_ticker("NVDA")

    assert classifier.classify.call_count == 2


@pytest.mark.asyncio
async def test_morning_sweep_calls_classifier_for_all_tickers():
    tickers = ["NVDA", "AAPL", "MSFT", "TSLA", "META",
               "GOOGL", "AMZN", "AMD", "SPY", "QQQ",
               "SOFI", "PLTR", "COIN", "MSTR", "ARKK"]
    config = make_config(tickers)
    classifier = MagicMock()
    dummy_state = RegimeState(regime="ranging", conviction=3, direction="neutral")
    classifier.classify = AsyncMock(return_value=dummy_state)
    regime_store = RegimeStore()

    watcher = make_watcher(config, classifier, regime_store)
    watcher._fetch_headlines = MagicMock(return_value=["Some headline"])

    await watcher.run_morning_sweep()

    assert classifier.classify.call_count == len(tickers)
