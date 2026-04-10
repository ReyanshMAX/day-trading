"""Integration tests for Alpaca broker connection.

Requires live Alpaca paper API credentials in .env.
Run with: pytest tests/test_broker_connection.py -m integration -v
"""

import pytest
from core.config import load_config
from core.broker import AlpacaBroker


@pytest.fixture(scope="module")
def broker():
    config = load_config()
    return AlpacaBroker(config)


@pytest.mark.integration
def test_get_account_returns_positive_nav(broker):
    account = broker.get_account()
    assert isinstance(account["nav"], float)
    assert account["nav"] > 0


@pytest.mark.integration
def test_get_bars_returns_correct_shape(broker):
    df = broker.get_bars("AAPL", "1Min", 5)
    assert len(df) == 5
    for col in ("open", "high", "low", "close", "volume"):
        assert col in df.columns
