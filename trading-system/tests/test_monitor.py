"""Tests for core/monitor.py — HealthMonitor."""

import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from core.monitor import HealthMonitor

_ET = ZoneInfo("America/New_York")


def _make_monitor(broker=None, classifier=None, tick_timeout=300.0):
    if broker is None:
        broker = AsyncMock()
        broker.get_account = AsyncMock(return_value={"nav": 100000.0})
    if classifier is None:
        classifier = MagicMock()
        classifier._groq_down = False
    return HealthMonitor(
        broker=broker,
        classifier=classifier,
        slack_webhook_url="",
        tick_timeout_seconds=tick_timeout,
        check_interval_seconds=60.0,
    )


def test_record_tick_resets_timer():
    monitor = _make_monitor()
    old_time = monitor._last_tick_time
    time.sleep(0.01)
    monitor.record_tick()
    assert monitor._last_tick_time > old_time


def test_is_market_hours_false_on_saturday():
    monitor = _make_monitor()
    # 2024-01-06 is a Saturday
    saturday = datetime(2024, 1, 6, 11, 0, 0, tzinfo=_ET)
    with patch("core.monitor.datetime") as mock_dt:
        mock_dt.now.return_value = saturday
        assert monitor._is_market_hours() is False


def test_is_market_hours_true_on_tuesday():
    monitor = _make_monitor()
    # 2024-01-09 is a Tuesday, 11am ET
    tuesday = datetime(2024, 1, 9, 11, 0, 0, tzinfo=_ET)
    with patch("core.monitor.datetime") as mock_dt:
        mock_dt.now.return_value = tuesday
        assert monitor._is_market_hours() is True


@pytest.mark.asyncio
async def test_check_stream_activity_no_alert_if_recent():
    monitor = _make_monitor(tick_timeout=300.0)
    monitor._last_tick_time = time.time()  # just now
    # Force market hours
    with patch.object(monitor, "_is_market_hours", return_value=True):
        with patch.object(monitor, "_alert", new_callable=AsyncMock) as mock_alert:
            await monitor._check_stream_activity()
            mock_alert.assert_not_called()


@pytest.mark.asyncio
async def test_check_stream_activity_alerts_if_stale_during_market_hours():
    monitor = _make_monitor(tick_timeout=5.0)
    monitor._last_tick_time = time.time() - 400.0  # 400s ago > 5s timeout
    with patch.object(monitor, "_is_market_hours", return_value=True):
        with patch.object(monitor, "_alert", new_callable=AsyncMock) as mock_alert:
            await monitor._check_stream_activity()
            mock_alert.assert_called_once()
            assert "stream" in mock_alert.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_check_stream_activity_no_alert_outside_market_hours():
    monitor = _make_monitor(tick_timeout=5.0)
    monitor._last_tick_time = time.time() - 400.0  # would trigger alert during hours
    with patch.object(monitor, "_is_market_hours", return_value=False):
        with patch.object(monitor, "_alert", new_callable=AsyncMock) as mock_alert:
            await monitor._check_stream_activity()
            mock_alert.assert_not_called()


@pytest.mark.asyncio
async def test_check_groq_circuit_alerts_when_down():
    classifier = MagicMock()
    classifier._groq_down = True
    monitor = _make_monitor(classifier=classifier)
    with patch.object(monitor, "_alert", new_callable=AsyncMock) as mock_alert:
        await monitor._check_groq_circuit()
        mock_alert.assert_called_once()
        assert "groq" in mock_alert.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_check_groq_circuit_no_alert_when_up():
    classifier = MagicMock()
    classifier._groq_down = False
    monitor = _make_monitor(classifier=classifier)
    with patch.object(monitor, "_alert", new_callable=AsyncMock) as mock_alert:
        await monitor._check_groq_circuit()
        mock_alert.assert_not_called()


@pytest.mark.asyncio
async def test_check_broker_connection_alerts_on_exception():
    broker = AsyncMock()
    broker.get_account = AsyncMock(side_effect=ConnectionError("broker unreachable"))
    monitor = _make_monitor(broker=broker)
    with patch.object(monitor, "_alert", new_callable=AsyncMock) as mock_alert:
        await monitor._check_broker_connection()
        mock_alert.assert_called_once()
        assert "broker" in mock_alert.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_alert_cooldown_suppresses_repeated_alerts():
    monitor = _make_monitor()
    # First alert: should fire
    monitor._alert_cooldowns = {}  # clear any pre-existing state
    with patch.object(monitor, "_is_market_hours", return_value=True):
        monitor._last_tick_time = time.time() - 400.0
        monitor._tick_timeout = 5.0

        fired = []

        async def fake_post_slack(msg):
            fired.append(msg)

        monitor._slack_webhook = "http://fake-webhook"
        monitor._post_slack = fake_post_slack

        # Force _alert_cooldown_seconds to 0 for the first call, then back to high
        # Easier: call _alert directly with same key twice
        monitor._alert_cooldowns = {}
        monitor._alert_cooldown_seconds = 600.0

        await monitor._alert("test message", "test_key")
        await monitor._alert("test message", "test_key")

        # Only one log/slack call because the second is rate-limited
        assert len(fired) == 1
