"""Unit tests for regime/classifier.py.

Uses AsyncMock to avoid real Groq API calls.
"""

import hashlib
import json
from datetime import datetime, timezone
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from regime.classifier import RegimeClassifier, fallback_regime
from signals.scoring import RegimeState


def make_mock_response(content: str):
    """Build a mock Groq response object."""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
    return resp


def make_classifier():
    config = MagicMock()
    config.groq_api_key = "test"
    config.llm.cache_ttl_minutes = 10
    chroma = MagicMock()
    chroma.get_similar_contexts.return_value = []
    chroma.store_classification.return_value = None
    clf = RegimeClassifier(config, chroma)
    return clf


VALID_JSON = json.dumps({
    "regime": "trending",
    "direction": "bullish",
    "conviction": 4,
    "catalyst": "Strong earnings beat",
    "avoid_reason": None,
})


@pytest.mark.asyncio
async def test_valid_response_returns_correct_state():
    clf = make_classifier()
    clf._client = AsyncMock()
    clf._client.chat = AsyncMock()
    clf._client.chat.completions = AsyncMock()
    clf._client.chat.completions.create = AsyncMock(return_value=make_mock_response(VALID_JSON))

    state = await clf.classify("NVDA", ["NVDA beats earnings"], "ranging")
    assert state.regime == "trending"
    assert state.direction == "bullish"
    assert state.conviction == 4
    assert state.catalyst == "Strong earnings beat"


@pytest.mark.asyncio
async def test_malformed_json_returns_fallback():
    clf = make_classifier()
    clf._client = AsyncMock()
    clf._client.chat = AsyncMock()
    clf._client.chat.completions = AsyncMock()
    clf._client.chat.completions.create = AsyncMock(
        return_value=make_mock_response("not valid json at all")
    )
    state = await clf.classify("NVDA", [], None)
    assert state.regime == "ranging"
    assert state.conviction == 2


@pytest.mark.asyncio
async def test_out_of_range_conviction_returns_fallback():
    clf = make_classifier()
    bad_json = json.dumps({
        "regime": "trending", "direction": "bullish", "conviction": 7,
        "catalyst": "big move", "avoid_reason": None,
    })
    clf._client = AsyncMock()
    clf._client.chat = AsyncMock()
    clf._client.chat.completions = AsyncMock()
    clf._client.chat.completions.create = AsyncMock(return_value=make_mock_response(bad_json))
    state = await clf.classify("NVDA", [], None)
    assert state == fallback_regime()


@pytest.mark.asyncio
async def test_wrong_case_regime_returns_fallback():
    clf = make_classifier()
    bad_json = json.dumps({
        "regime": "TRENDING", "direction": "bullish", "conviction": 3,
        "catalyst": "momentum", "avoid_reason": None,
    })
    clf._client = AsyncMock()
    clf._client.chat = AsyncMock()
    clf._client.chat.completions = AsyncMock()
    clf._client.chat.completions.create = AsyncMock(return_value=make_mock_response(bad_json))
    state = await clf.classify("NVDA", [], None)
    assert state == fallback_regime()


@pytest.mark.asyncio
async def test_api_exception_returns_fallback():
    clf = make_classifier()
    clf._client = AsyncMock()
    clf._client.chat = AsyncMock()
    clf._client.chat.completions = AsyncMock()
    clf._client.chat.completions.create = AsyncMock(side_effect=TimeoutError("Groq timeout"))
    state = await clf.classify("NVDA", [], None)
    assert state == fallback_regime()
    # Must not raise


@pytest.mark.asyncio
async def test_cache_hit_calls_groq_exactly_once():
    """Two classify() calls with identical headlines within 10 min → Groq called once."""
    headlines = ["NVDA beats earnings", "Revenue up 20%"]

    # Build a regime store mock whose get() returns None first call,
    # then returns the cached state on the second call.
    regime_store = MagicMock()
    # First call: no cached state yet
    # Second call: return a state that was classified just now with matching hash
    joined = "; ".join(headlines[:5])
    current_hash = hashlib.md5(joined.encode()).hexdigest()
    cached_state = RegimeState(
        regime="trending",
        conviction=4,
        direction="bullish",
        catalyst="Strong earnings beat",
        last_classified_at=datetime.now(timezone.utc),
        last_headlines_hash=current_hash,
    )
    # First call returns None (cache miss), second returns the cached state
    regime_store.get.side_effect = [None, cached_state]
    regime_store.set = MagicMock()

    clf = make_classifier()
    clf._regime_store = regime_store
    groq_mock = AsyncMock(return_value=make_mock_response(VALID_JSON))
    clf._client = AsyncMock()
    clf._client.chat = AsyncMock()
    clf._client.chat.completions = AsyncMock()
    clf._client.chat.completions.create = groq_mock

    # First call — cache miss, Groq fires
    await clf.classify("NVDA", headlines, "ranging")
    # Simulate the regime store being updated after first classification
    # (in production news_watcher calls regime_store.set; here we already
    # set side_effect so second get() returns cached_state automatically)

    # Second call — same headlines, cache should hit
    state2 = await clf.classify("NVDA", headlines, "ranging")

    assert groq_mock.call_count == 1, (
        f"Expected Groq to be called exactly once, got {groq_mock.call_count}"
    )
    assert state2.regime == "trending"
    assert state2.last_headlines_hash == current_hash
