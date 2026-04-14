"""Entry point — boots all async loops for the agentic day trading system."""

import asyncio
import logging
from datetime import datetime
from pytz import timezone

from core.config import load_config
from core.logging_setup import configure_logging
from core.broker import AlpacaBroker
from core.portfolio import Portfolio
from core.order_manager import OrderManager
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


async def main() -> None:
    configure_logging()
    config = load_config()
    log.info("System starting. NAV=%.0f tickers=%d", config.account.nav, len(config.universe.tickers))

    broker = AlpacaBroker(config)
    acct = await broker.get_account()
    log.info("Alpaca connected. Paper NAV=%.2f", acct["nav"])

    portfolio = Portfolio(nav=config.account.nav)
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

    shutdown = asyncio.Event()

    log.info("Starting stream and watchers...")
    crypto_tickers = [t for t in config.universe.tickers if _is_crypto(t)]

    asset_cache_task = asyncio.create_task(executor.refresh_asset_cache())

    stream_task = asyncio.create_task(
        stream.start(config.universe.tickers, executor.on_tick,
                     api_key=config.alpaca_api_key, secret_key=config.alpaca_secret_key)
    )
    watch_task = asyncio.create_task(news_watcher.watch())
    eod_task = asyncio.create_task(close_all_positions_eod(broker, portfolio, shutdown, chroma=chroma))
    score_log_task = asyncio.create_task(signal_engine.log_scores_loop(crypto_tickers))
    duration_task = asyncio.create_task(executor.check_position_durations())

    await shutdown.wait()
    log.info("Shutdown event set — cancelling stream and news watcher.")
    asset_cache_task.cancel()
    stream_task.cancel()
    watch_task.cancel()
    score_log_task.cancel()
    duration_task.cancel()
    results = await asyncio.gather(
        asset_cache_task, stream_task, watch_task, eod_task,
        score_log_task, duration_task,
        return_exceptions=True,
    )
    task_names = ["asset_cache", "stream", "news_watcher", "eod_close", "score_log", "duration_check"]
    for name, result in zip(task_names, results):
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            log.critical("Task %s exited with exception: %s", name, result, exc_info=result)


if __name__ == "__main__":
    asyncio.run(main())
