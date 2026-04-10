"""Unit tests for regime/classifier.py.

Uses AsyncMock to avoid real Groq API calls.
"""

import json
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
