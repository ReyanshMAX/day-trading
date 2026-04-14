"""Tests for memory/chroma_store.py.

Uses an in-memory ChromaDB client so no disk I/O occurs.
"""

from unittest.mock import MagicMock

from memory.chroma_store import ChromaStore


_store_counter = 0


def _make_store() -> ChromaStore:
    """Return a ChromaStore backed by an in-memory ChromaDB client.

    Uses a unique collection name per call so tests never share state,
    even within the same in-process EphemeralClient.
    """
    global _store_counter
    _store_counter += 1
    import chromadb
    client = chromadb.EphemeralClient()
    store = ChromaStore.__new__(ChromaStore)
    store._client = client
    store._collection = client.get_or_create_collection(f"regime_outcomes_{_store_counter}")
    store._min_outcomes_for_summary = 5
    return store


def _make_regime(regime="trending", conviction=4, direction="bullish", catalyst="test"):
    state = MagicMock()
    state.regime = regime
    state.conviction = conviction
    state.direction = direction
    state.catalyst = catalyst
    return state


def _insert_outcome(store: ChromaStore, ticker: str, date_str: str, pnl_pct: float, regime="trending") -> None:
    """Insert a completed outcome record directly into the collection."""
    doc_id = f"{ticker}_{date_str}"
    store._collection.upsert(
        ids=[doc_id],
        documents=[f"{ticker} | test | headline"],
        metadatas=[{
            "ticker": ticker,
            "regime": regime,
            "direction": "bullish",
            "conviction": 4,
            "outcome": "profitable" if pnl_pct > 0 else "unprofitable",
            "pnl_pct": pnl_pct,
            "date": date_str,
            "catalyst": "test",
            "signal_score": 0.0,
            "confidence": 0.0,
        }],
    )


# ---------------------------------------------------------------------------
# store_classification
# ---------------------------------------------------------------------------

def test_store_classification_stores_metadata():
    store = _make_store()
    state = _make_regime()
    store.store_classification("NVDA", state, ["headline A", "headline B"])

    result = store._collection.get(ids=["NVDA_" + __import__("datetime").date.today().isoformat()])
    assert len(result["metadatas"]) == 1
    meta = result["metadatas"][0]
    assert meta["ticker"] == "NVDA"
    assert meta["regime"] == "trending"
    assert meta["signal_score"] == 0.0
    assert meta["confidence"] == 0.0
    assert meta["outcome"] == "pending"


def test_store_classification_accepts_signal_score_and_confidence():
    store = _make_store()
    state = _make_regime()
    store.store_classification("AAPL", state, [], signal_score=0.85, confidence=0.9)

    result = store._collection.get(ids=["AAPL_" + __import__("datetime").date.today().isoformat()])
    meta = result["metadatas"][0]
    assert abs(meta["signal_score"] - 0.85) < 1e-6
    assert abs(meta["confidence"] - 0.9) < 1e-6


# ---------------------------------------------------------------------------
# update_outcome
# ---------------------------------------------------------------------------

def test_update_outcome_sets_pnl_and_status():
    store = _make_store()
    state = _make_regime()
    store.store_classification("NVDA", state, [])
    date_str = __import__("datetime").date.today().isoformat()
    store.update_outcome("NVDA", date_str, 0.015)

    result = store._collection.get(ids=[f"NVDA_{date_str}"])
    meta = result["metadatas"][0]
    assert abs(meta["pnl_pct"] - 0.015) < 1e-9
    assert meta["outcome"] == "profitable"


def test_update_outcome_marks_unprofitable():
    store = _make_store()
    state = _make_regime()
    store.store_classification("AMD", state, [])
    date_str = __import__("datetime").date.today().isoformat()
    store.update_outcome("AMD", date_str, -0.02)

    result = store._collection.get(ids=[f"AMD_{date_str}"])
    meta = result["metadatas"][0]
    assert meta["outcome"] == "unprofitable"


# ---------------------------------------------------------------------------
# get_similar_contexts — where filter always applied
# ---------------------------------------------------------------------------

def test_get_similar_contexts_filters_by_ticker():
    """Only results for the requested ticker should be returned."""
    store = _make_store()
    state = _make_regime()
    store.store_classification("NVDA", state, ["nvidia news"])
    store.store_classification("AAPL", _make_regime(catalyst="apple news"), ["apple news"])

    results = store.get_similar_contexts("NVDA", ["nvidia news"], n=5)
    # All returned results must be for NVDA
    for r in results:
        assert "NVDA" in r, f"Non-NVDA result returned: {r}"


def test_get_similar_contexts_returns_empty_on_empty_collection():
    store = _make_store()
    results = store.get_similar_contexts("NVDA", ["some headline"], n=2)
    assert results == []


# ---------------------------------------------------------------------------
# get_similar_contexts — performance summary (requirement 3 & 4)
# ---------------------------------------------------------------------------

def test_get_similar_contexts_performance_summary_present_with_6_outcomes():
    """Insert 6 completed outcomes for a single ticker; assert summary is in result."""
    store = _make_store()

    # Insert 6 outcomes: 4 profitable (+2%), 2 unprofitable (-1%)
    dates = [f"2024-01-{10 + i:02d}" for i in range(6)]
    pnl_values = [0.02, 0.02, 0.02, 0.02, -0.01, -0.01]

    for i, (date_str, pnl) in enumerate(zip(dates, pnl_values)):
        _insert_outcome(store, "NVDA", date_str, pnl)

    results = store.get_similar_contexts("NVDA", ["nvidia GPU earnings"], n=2)

    assert len(results) > 0, "Expected at least one result"
    # At least one result must contain the performance summary
    summary_present = any("Historical performance for NVDA" in r for r in results)
    assert summary_present, f"Performance summary not found in results: {results}"

    # Verify the summary content is correct
    summary_line = next(r for r in results if "Historical performance for NVDA" in r)
    assert "6 trades" in summary_line
    assert "win rate 67%" in summary_line  # 4/6 = 66.7% → rounds to 67%
    # avg_pnl = (4*0.02 + 2*(-0.01)) / 6 = (0.08 - 0.02) / 6 = 0.01
    assert "+0.01%" in summary_line


def test_get_similar_contexts_no_summary_with_fewer_than_5_outcomes():
    """With only 4 outcomes, no performance summary should be appended."""
    store = _make_store()

    for i in range(4):
        _insert_outcome(store, "MSFT", f"2024-01-{i + 10:02d}", 0.01)

    results = store.get_similar_contexts("MSFT", ["microsoft news"], n=2)
    for r in results:
        assert "Historical performance" not in r


def test_get_similar_contexts_no_summary_when_all_outcomes_pending():
    """Records with outcome='pending' must not count toward the 5+ threshold."""
    store = _make_store()
    # Insert 6 pending records (store_classification creates pending records)
    for i in range(6):
        doc_id = f"TSLA_2024-01-{i + 10:02d}"
        store._collection.upsert(
            ids=[doc_id],
            documents=["TSLA | test | headline"],
            metadatas=[{
                "ticker": "TSLA",
                "regime": "trending",
                "direction": "bullish",
                "conviction": 3,
                "outcome": "pending",
                "pnl_pct": 0.0,
                "date": f"2024-01-{i + 10:02d}",
                "catalyst": "test",
                "signal_score": 0.0,
                "confidence": 0.0,
            }],
        )

    results = store.get_similar_contexts("TSLA", ["tesla news"], n=2)
    for r in results:
        assert "Historical performance" not in r
