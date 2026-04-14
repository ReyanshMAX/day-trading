"""Session-scoped state for ORB caching.

Single responsibility: store per-session ORB levels so they are computed
exactly once at 9:45 AM ET and never recomputed mid-session.
"""

_orb_cache: dict[str, tuple[float, float]] = {}
_orb_suppressed: set[str] = set()


def get_orb(ticker: str) -> tuple[float, float] | None:
    """Return cached (orb_high, orb_low) for ticker, or None if not yet locked."""
    return _orb_cache.get(ticker)


def set_orb(ticker: str, high: float, low: float) -> None:
    """Lock in ORB levels for ticker for this session."""
    _orb_cache[ticker] = (high, low)


def is_orb_suppressed(ticker: str) -> bool:
    """Return True if ORB was suppressed due to quality filter (range > 2x ATR)."""
    return ticker in _orb_suppressed


def suppress_orb(ticker: str) -> None:
    """Mark ticker's ORB as suppressed for this session."""
    _orb_suppressed.add(ticker)


def reset_session() -> None:
    """Clear all session state. Call at system startup."""
    _orb_cache.clear()
    _orb_suppressed.clear()
