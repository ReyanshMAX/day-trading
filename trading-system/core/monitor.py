"""Health monitor for the trading system.

Single responsibility: detect silent failures and alert via logging +
optional Slack webhook. Runs as a background asyncio task.
"""

import asyncio
import logging
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_MARKET_OPEN = dtime(9, 30)
_MARKET_CLOSE = dtime(16, 0)


class HealthMonitor:
    """Monitors stream activity, circuit-breaker state, and broker connectivity."""

    def __init__(
        self,
        broker,
        classifier,
        slack_webhook_url: str = "",
        tick_timeout_seconds: float = 300.0,
        check_interval_seconds: float = 60.0,
    ) -> None:
        self._broker = broker
        self._classifier = classifier
        self._slack_webhook = slack_webhook_url
        self._tick_timeout = tick_timeout_seconds
        self._check_interval = check_interval_seconds
        self._last_tick_time: float = time.time()
        self._alert_cooldowns: dict[str, float] = {}  # alert_key -> last_alert_time
        self._alert_cooldown_seconds = 600.0  # 10 min between repeated alerts

    def record_tick(self) -> None:
        """Call on every incoming market tick to reset the activity timer."""
        self._last_tick_time = time.time()

    def _is_market_hours(self) -> bool:
        now = datetime.now(tz=_ET)
        if now.weekday() >= 5:
            return False
        return _MARKET_OPEN <= now.time() < _MARKET_CLOSE

    def _should_alert(self, key: str) -> bool:
        """Rate-limit alerts: return True if enough time has passed since last alert."""
        now = time.time()
        last = self._alert_cooldowns.get(key, 0.0)
        if now - last >= self._alert_cooldown_seconds:
            self._alert_cooldowns[key] = now
            return True
        return False

    async def _alert(self, message: str, key: str) -> None:
        if not self._should_alert(key):
            return
        log.critical("HEALTH ALERT: %s", message)
        if self._slack_webhook:
            await self._post_slack(message)

    async def _post_slack(self, message: str) -> None:
        try:
            import aiohttp
            import json
            async with aiohttp.ClientSession() as session:
                await session.post(
                    self._slack_webhook,
                    data=json.dumps({"text": f"[TradingSystem] {message}"}),
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=5),
                )
        except Exception as e:
            log.debug("Slack alert failed: %s", e)

    async def _check_stream_activity(self) -> None:
        if not self._is_market_hours():
            return
        elapsed = time.time() - self._last_tick_time
        if elapsed > self._tick_timeout:
            await self._alert(
                f"No market ticks received for {elapsed:.0f}s — stream may be dead",
                "stream_dead",
            )

    async def _check_groq_circuit(self) -> None:
        if getattr(self._classifier, "_groq_down", False):
            await self._alert(
                "Groq circuit breaker is OPEN — regime classification using Ollama fallback or stale data",
                "groq_circuit_open",
            )

    async def _check_broker_connection(self) -> None:
        try:
            acct = await self._broker.get_account()
            nav = acct.get("nav", 0)
            if nav <= 0:
                await self._alert(
                    f"Broker NAV is non-positive: {nav} — possible account issue",
                    "nav_nonpositive",
                )
        except Exception as e:
            await self._alert(
                f"Broker connectivity check failed: {e}",
                "broker_unreachable",
            )

    async def run(self) -> None:
        """Main monitoring loop. Runs indefinitely until cancelled."""
        log.info("Health monitor started (interval=%ds, tick_timeout=%ds)",
                 int(self._check_interval), int(self._tick_timeout))
        while True:
            await asyncio.sleep(self._check_interval)
            try:
                await self._check_stream_activity()
                await self._check_groq_circuit()
                await self._check_broker_connection()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("Health monitor check failed: %s", e)
