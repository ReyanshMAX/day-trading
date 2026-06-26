"""Tests for core/trade_journal.py — async SQLite trade journal."""

import sys
from unittest.mock import patch

import pytest
import pytest_asyncio

from core.trade_journal import TradeJournal


@pytest.mark.asyncio
async def test_open_creates_table():
    journal = TradeJournal(db_path=":memory:")
    await journal.open()
    assert journal._db is not None
    cursor = await journal._db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    )
    row = await cursor.fetchone()
    assert row is not None
    await journal.close()


@pytest.mark.asyncio
async def test_record_entry_returns_id():
    journal = TradeJournal(db_path=":memory:")
    await journal.open()
    trade_id = await journal.record_entry(
        ticker="NVDA",
        side="long",
        qty=10.0,
        entry_price=900.0,
        stop=880.0,
        target=940.0,
        regime="trending",
        conviction=4,
        signal_score=0.75,
    )
    assert trade_id is not None
    assert isinstance(trade_id, int)
    assert trade_id > 0
    await journal.close()


@pytest.mark.asyncio
async def test_record_exit_updates_row():
    journal = TradeJournal(db_path=":memory:")
    await journal.open()
    trade_id = await journal.record_entry(
        ticker="AAPL", side="long", qty=5.0, entry_price=180.0,
        stop=175.0, target=190.0, regime="ranging", conviction=3, signal_score=0.6,
    )
    await journal.record_exit(trade_id, exit_price=188.0, exit_reason="target_hit", pnl=40.0, pnl_pct=0.044)
    cursor = await journal._db.execute(
        "SELECT exit_price, exit_reason, pnl, pnl_pct FROM trades WHERE id=?", (trade_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == pytest.approx(188.0)
    assert row[1] == "target_hit"
    assert row[2] == pytest.approx(40.0)
    assert row[3] == pytest.approx(0.044)
    await journal.close()


@pytest.mark.asyncio
async def test_entry_exit_roundtrip():
    journal = TradeJournal(db_path=":memory:")
    await journal.open()
    trade_id = await journal.record_entry(
        ticker="TSLA", side="short", qty=3.0, entry_price=250.0,
        stop=260.0, target=230.0, regime="trending", conviction=5, signal_score=0.85,
    )
    await journal.record_exit(trade_id, exit_price=232.0, exit_reason="stop_hit", pnl=-54.0, pnl_pct=-0.072)
    cursor = await journal._db.execute(
        "SELECT ticker, side, qty, entry_price, exit_price, regime, conviction, signal_score, exit_reason, pnl, pnl_pct FROM trades WHERE id=?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == "TSLA"
    assert row[1] == "short"
    assert row[2] == pytest.approx(3.0)
    assert row[3] == pytest.approx(250.0)
    assert row[4] == pytest.approx(232.0)
    assert row[5] == "trending"
    assert row[6] == 5
    assert row[7] == pytest.approx(0.85)
    assert row[8] == "stop_hit"
    assert row[9] == pytest.approx(-54.0)
    assert row[10] == pytest.approx(-0.072)
    await journal.close()


@pytest.mark.asyncio
async def test_daily_summary_counts():
    journal = TradeJournal(db_path=":memory:")
    await journal.open()
    t1 = await journal.record_entry(
        ticker="NVDA", side="long", qty=10.0, entry_price=900.0,
        stop=880.0, target=940.0, regime="trending", conviction=4, signal_score=0.7,
    )
    t2 = await journal.record_entry(
        ticker="AMD", side="long", qty=20.0, entry_price=150.0,
        stop=145.0, target=160.0, regime="trending", conviction=3, signal_score=0.6,
    )
    t3 = await journal.record_entry(
        ticker="AAPL", side="short", qty=5.0, entry_price=180.0,
        stop=185.0, target=170.0, regime="ranging", conviction=3, signal_score=0.55,
    )
    await journal.record_exit(t1, exit_price=938.0, exit_reason="target_hit", pnl=380.0, pnl_pct=0.042)
    await journal.record_exit(t2, exit_price=148.0, exit_reason="stop_hit", pnl=-40.0, pnl_pct=-0.013)
    await journal.record_exit(t3, exit_price=172.0, exit_reason="eod", pnl=40.0, pnl_pct=0.044)

    summary = await journal.daily_summary()
    assert summary["trades"] == 3
    assert summary["wins"] == 2
    assert summary["losses"] == 1
    assert summary["total_pnl"] == pytest.approx(380.0 - 40.0 + 40.0)
    await journal.close()


@pytest.mark.asyncio
async def test_daily_summary_empty():
    journal = TradeJournal(db_path=":memory:")
    await journal.open()
    summary = await journal.daily_summary()
    assert summary["trades"] == 0
    assert summary["wins"] == 0
    assert summary["losses"] == 0
    assert summary["total_pnl"] == pytest.approx(0.0)
    await journal.close()


@pytest.mark.asyncio
async def test_record_exit_none_trade_id_is_noop():
    journal = TradeJournal(db_path=":memory:")
    await journal.open()
    # Should not raise
    await journal.record_exit(None, exit_price=100.0, exit_reason="eod")
    await journal.close()


@pytest.mark.asyncio
async def test_graceful_disable_when_aiosqlite_missing():
    # Simulate aiosqlite not being installed
    with patch.dict(sys.modules, {"aiosqlite": None}):
        journal = TradeJournal(db_path=":memory:")
        await journal.open()
        assert journal._db is None
        # All operations should be no-ops when db is None
        result = await journal.record_entry(
            ticker="X", side="long", qty=1.0, entry_price=10.0,
            stop=9.0, target=12.0, regime="ranging", conviction=2, signal_score=0.5,
        )
        assert result is None
        await journal.record_exit(1, exit_price=11.0, exit_reason="eod")  # no-op
        summary = await journal.daily_summary()
        assert summary == {}
