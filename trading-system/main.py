"""Entry point — boots all async loops for the agentic day trading system."""

import asyncio
import logging
import os
from datetime import datetime
from pytz import timezone

from core.config import load_config
from core.logging_setup import configure_logging
from core.broker import AlpacaBroker
from core.forex_broker import OANDABroker
from core.forex_stream import ForexStream
from core.portfolio import Portfolio
from core.order_manager import OrderManager
from core.monitor import HealthMonitor
from core import stream
from signals.bar_store import BarStore
from signals.engine import SignalEngine
from regime.classifier import RegimeClassifier
from regime.news_watcher import NewsWatcher
from regime.regime_store import RegimeStore
from memory.chroma_store import ChromaStore
from execution.executor import Executor
from risk.gate import check as gate_check

log = logging.getLogger(__name__)


def _is_crypto(ticker: str) -> bool:
    return "/" in ticker or (ticker.endswith("USD") and len(ticker) > 4)


async def close_all_positions_eod(
    broker: AlpacaBroker,
    portfolio: Portfolio,
    shutdown: asyncio.Event,
    chroma: "ChromaStore | None" = None,
) -> None:
    """Sleeps until 3:55 PM ET, closes all equity positions, then sets the shutdown event."""
    et = timezone("America/New_York")
    now = datetime.now(tz=et)
    close_time = now.replace(hour=15, minute=55, second=0, microsecond=0)

    if now >= close_time:
        # Started after today's close — schedule for tomorrow
        from datetime import timedelta
        close_time = close_time + timedelta(days=1)

    wait_seconds = (close_time - now).total_seconds()
    log.info("EOD close scheduled in %.0f seconds (at %s ET)", wait_seconds, close_time.strftime("%H:%M"))
    await asyncio.sleep(wait_seconds)

    log.info("EOD: closing equity positions")
    from datetime import timezone as _tz
    date_str = datetime.now(tz=_tz.utc).date().isoformat()
    for ticker, pos in list(portfolio.positions.items()):
        if _is_crypto(ticker):
            log.info("EOD: skipping crypto position %s (24/7 market)", ticker)
            continue
        side = "sell" if pos.side == "long" else "buy"
        close_order = await broker.submit_market_order(ticker, pos.qty, side)
        pnl_pct = portfolio.record_close(close_order)
        if chroma is not None and pnl_pct is not None:
            try:
                chroma.update_outcome(ticker, date_str, pnl_pct)
            except Exception as e:
                log.error("EOD update_outcome failed for %s: %s", ticker, e)
    shutdown.set()


async def _forex_loop(config) -> None:
    """Start OANDA forex stream if credentials are configured. Runs indefinitely."""
    if not config.oanda_api_key or not config.oanda_account_id:
        log.info("OANDA credentials not set — forex trading disabled")
        return
    if config.forex is None or not config.forex.pairs:
        log.info("No forex pairs configured — forex trading disabled")
        return

    log.info("Starting forex stream for pairs: %s", config.forex.pairs)

    async def on_forex_tick(pair: str, price: float, volume: float, ts) -> None:
        log.debug("Forex tick: %s %.5f", pair, price)

    await ForexStream(config.oanda_api_key, config.oanda_account_id).start(
        config.forex.pairs,
        on_forex_tick,
    )


async def _daily_reset_loop(portfolio: Portfolio) -> None:
    """Resets daily P&L at 9:30 AM ET each trading day."""
    from zoneinfo import ZoneInfo
    from datetime import timedelta
    et = ZoneInfo("America/New_York")
    while True:
        now = datetime.now(tz=et)
        open_today = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now >= open_today:
            open_today = open_today + timedelta(days=1)
        await asyncio.sleep((open_today - now).total_seconds())
        portfolio.reset_daily()


async def main() -> None:
    configure_logging()
    config = load_config()
    log.info("System starting. NAV=%.0f tickers=%d", config.account.nav, len(config.universe.tickers))

    broker = AlpacaBroker(config)
    acct = await broker.get_account()
    log.info("Alpaca connected. Paper NAV=%.2f", acct["nav"])

    portfolio = Portfolio(nav=config.account.nav)
    daily_reset_task = asyncio.create_task(_daily_reset_loop(portfolio))
    positions = await broker.get_positions()
    portfolio.reconcile_positions(positions)
    regime_store = RegimeStore()
    chroma = ChromaStore(min_outcomes_for_summary=config.memory.min_outcomes_for_summary)
    bar_store = BarStore()

    log.info("Backfilling bars...")
    for ticker in config.universe.tickers:
        try:
            bars = broker.get_bars(ticker, "1Min", limit=100)
            bar_store.backfill(ticker, bars)
        except Exception as e:
            log.error("Backfill failed for %s: %s", ticker, e)

    classifier = RegimeClassifier(config, chroma, broker=broker)
    news_watcher = NewsWatcher(config, classifier, regime_store)
    log.info("Running morning regime sweep...")
    await news_watcher.run_morning_sweep()

    signal_engine = SignalEngine(config, bar_store, regime_store)
    order_manager = OrderManager(config)
    executor = Executor(broker, portfolio, signal_engine, order_manager, gate_check, config, chroma_store=chroma)

    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
    monitor = HealthMonitor(
        broker=broker,
        classifier=classifier,
        slack_webhook_url=slack_webhook,
    )

    async def _on_tick(ticker: str, price: float, volume: float, ts) -> None:
        monitor.record_tick()
        await executor.on_tick(ticker, price, volume, ts)

    shutdown = asyncio.Event()

    log.info("Starting stream and watchers...")
    crypto_tickers = [t for t in config.universe.tickers if _is_crypto(t)]

    asset_cache_task = asyncio.create_task(executor.refresh_asset_cache())
    monitor_task = asyncio.create_task(monitor.run())

    stream_task = asyncio.create_task(
        stream.start(config.universe.tickers, _on_tick,
                     api_key=config.alpaca_api_key, secret_key=config.alpaca_secret_key,
                     paper=config.account.paper)
    )
    watch_task = asyncio.create_task(news_watcher.watch())
    eod_task = asyncio.create_task(close_all_positions_eod(broker, portfolio, shutdown, chroma=chroma))
    score_log_task = asyncio.create_task(signal_engine.log_scores_loop(config.universe.tickers))
    duration_task = asyncio.create_task(executor.check_position_durations())
    forex_task = asyncio.create_task(_forex_loop(config))

    await shutdown.wait()
    log.info("Shutdown event set — cancelling stream and news watcher.")
    asset_cache_task.cancel()
    monitor_task.cancel()
    stream_task.cancel()
    watch_task.cancel()
    score_log_task.cancel()
    duration_task.cancel()
    daily_reset_task.cancel()
    forex_task.cancel()
    results = await asyncio.gather(
        asset_cache_task, monitor_task, stream_task, watch_task, eod_task,
        score_log_task, duration_task, daily_reset_task, forex_task,
        return_exceptions=True,
    )
    task_names = ["asset_cache", "monitor", "stream", "news_watcher", "eod_close", "score_log", "duration_check", "daily_reset", "forex"]
    for name, result in zip(task_names, results):
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            log.critical("Task %s exited with exception: %s", name, result, exc_info=result)


if __name__ == "__main__":
    asyncio.run(main())
