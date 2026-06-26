"""Offline tests for core/forex_broker.py.

All tests mock aiohttp — no network calls.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_broker():
    from core.forex_broker import OANDABroker
    return OANDABroker(api_key="test_key", account_id="test_account")


def _mock_resp(payload: dict, status: int = 200):
    """Return a context-manager mock that yields an aiohttp response-like object."""
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=payload)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _mock_session(get_payload=None, post_payload=None):
    """Return a patched aiohttp.ClientSession context manager."""
    session = AsyncMock()
    if get_payload is not None:
        session.get = MagicMock(return_value=_mock_resp(get_payload))
    if post_payload is not None:
        session.post = MagicMock(return_value=_mock_resp(post_payload))
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, session


# ── get_account ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_account_returns_expected_keys():
    broker = _make_broker()
    payload = {
        "account": {
            "NAV": "125000.50",
            "unrealizedPL": "500.00",
            "marginUsed": "2500.00",
        }
    }
    ctx, _ = _mock_session(get_payload=payload)
    with patch("aiohttp.ClientSession", return_value=ctx):
        result = await broker.get_account()

    assert set(result.keys()) == {"nav", "unrealized_pnl", "margin_used"}
    assert result["nav"] == pytest.approx(125000.50)
    assert result["unrealized_pnl"] == pytest.approx(500.0)
    assert result["margin_used"] == pytest.approx(2500.0)


# ── get_bars ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_bars_returns_dataframe_with_correct_shape():
    broker = _make_broker()
    candle = {
        "time": "2024-01-15T10:00:00.000000000Z",
        "mid": {"o": "1.09500", "h": "1.09600", "l": "1.09400", "c": "1.09550"},
        "volume": 1234,
    }
    payload = {"candles": [candle] * 5}
    ctx, _ = _mock_session(get_payload=payload)
    with patch("aiohttp.ClientSession", return_value=ctx):
        df = await broker.get_bars("EUR/USD", "M1", 5)

    assert len(df) == 5
    for col in ("open", "high", "low", "close", "volume"):
        assert col in df.columns


@pytest.mark.asyncio
async def test_get_bars_empty_candles_returns_empty_df():
    broker = _make_broker()
    payload = {"candles": []}
    ctx, _ = _mock_session(get_payload=payload)
    with patch("aiohttp.ClientSession", return_value=ctx):
        df = await broker.get_bars("EUR/USD", "M1", 5)

    assert df.empty
    for col in ("open", "high", "low", "close", "volume"):
        assert col in df.columns


# ── submit_order ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_order_long_posts_positive_units():
    broker = _make_broker()
    payload = {"orderFillTransaction": {"id": "12345"}}
    ctx, session = _mock_session(post_payload=payload)
    with patch("aiohttp.ClientSession", return_value=ctx):
        result = await broker.submit_order("EUR/USD", 1000, "long", 1.09, 1.11)

    assert result == payload
    posted_data = session.post.call_args
    # Check URL contains /orders
    url_arg = posted_data[0][0]
    assert "/orders" in url_arg
    # Check body has positive units
    body = json.loads(posted_data[1]["data"])
    assert body["order"]["units"] == "1000"
    assert body["order"]["instrument"] == "EUR_USD"


@pytest.mark.asyncio
async def test_submit_order_short_posts_negative_units():
    broker = _make_broker()
    payload = {"orderFillTransaction": {"id": "12346"}}
    ctx, session = _mock_session(post_payload=payload)
    with patch("aiohttp.ClientSession", return_value=ctx):
        await broker.submit_order("GBP/USD", 2000, "short", 1.27, 1.25)

    body = json.loads(session.post.call_args[1]["data"])
    assert body["order"]["units"] == "-2000"


# ── get_open_positions ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_open_positions_returns_list_with_long():
    broker = _make_broker()
    payload = {
        "positions": [
            {
                "instrument": "EUR_USD",
                "long": {"units": "1000", "averagePrice": "1.09500", "unrealizedPL": "50.00"},
                "short": {"units": "0"},
            }
        ]
    }
    ctx, _ = _mock_session(get_payload=payload)
    with patch("aiohttp.ClientSession", return_value=ctx):
        positions = await broker.get_open_positions()

    assert len(positions) == 1
    pos = positions[0]
    assert pos["pair"] == "EUR/USD"
    assert pos["side"] == "long"
    assert pos["units"] == pytest.approx(1000.0)


@pytest.mark.asyncio
async def test_get_open_positions_empty():
    broker = _make_broker()
    payload = {"positions": []}
    ctx, _ = _mock_session(get_payload=payload)
    with patch("aiohttp.ClientSession", return_value=ctx):
        positions = await broker.get_open_positions()

    assert positions == []


# ── close_position ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_close_position_posts_to_correct_path():
    broker = _make_broker()
    payload = {"relatedTransactionIDs": ["1", "2"]}
    ctx, session = _mock_session(post_payload=payload)
    with patch("aiohttp.ClientSession", return_value=ctx):
        result = await broker.close_position("EUR/USD")

    url_arg = session.post.call_args[0][0]
    assert "EUR_USD" in url_arg
    assert "/close" in url_arg
    assert result == payload


@pytest.mark.asyncio
async def test_close_position_returns_empty_dict_on_error():
    broker = _make_broker()
    ctx = AsyncMock()
    session = AsyncMock()
    session.post = MagicMock(side_effect=Exception("network error"))
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    with patch("aiohttp.ClientSession", return_value=ctx):
        result = await broker.close_position("EUR/USD")

    assert result == {}


# ── compute_units ─────────────────────────────────────────────────────────────

def test_compute_units_returns_int_at_least_min_lot():
    from core.forex_broker import OANDABroker
    broker = OANDABroker("key", "acct")
    units = broker.compute_units(nav=100_000, risk_pct=0.005, stop_distance_pips=10.0)
    assert isinstance(units, int)
    assert units >= 1000


def test_compute_units_correct_calculation():
    from core.forex_broker import OANDABroker
    broker = OANDABroker("key", "acct")
    # risk = 100000 * 0.005 = $500
    # units = 500 / (10 * 10 / 100000) = 500 / 0.001 = 500000
    units = broker.compute_units(nav=100_000, risk_pct=0.005, stop_distance_pips=10.0, pip_value=10.0)
    assert units == 500_000


def test_compute_units_zero_stop_returns_minimum():
    from core.forex_broker import OANDABroker
    broker = OANDABroker("key", "acct")
    units = broker.compute_units(nav=100_000, risk_pct=0.005, stop_distance_pips=0)
    assert units == 1000
