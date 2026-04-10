"""In-memory regime state store.

Single responsibility: thread-safe read/write of RegimeState per ticker.
"""

import logging
from signals.scoring import RegimeState

log = logging.getLogger(__name__)


class RegimeStore:
    """Holds latest RegimeState per ticker."""

    def __init__(self) -> None:
        self._store: dict[str, RegimeState] = {}

    def get(self, ticker: str) -> RegimeState | None:
        return self._store.get(ticker)

    def set(self, ticker: str, state: RegimeState) -> None:
        self._store[ticker] = state
        log.info(
            "Regime updated: %s regime=%s direction=%s conviction=%d catalyst=%s",
            ticker, state.regime, state.direction, state.conviction, state.catalyst,
        )
