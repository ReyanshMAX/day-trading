"""Shared utilities for the trading system.

Single responsibility: small stateless helpers used across multiple modules.
"""


def is_crypto(ticker: str) -> bool:
    """Return True if ticker is a crypto pair (e.g. BTC/USD, ETHUSD)."""
    return "/" in ticker or (ticker.endswith("USD") and len(ticker) > 4)
