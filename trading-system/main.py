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

log = logging.getLogger(__name__)


async def close_all_positions_eod(broker, portfolio, config):
    """Scheduled coroutine that closes all positions at 3:55 PM ET."""
    while True:
        now = datetime.now(tz=timezone("America/New_York"))
        close_time = now.replace(hour=15, minute=55, second=0, microsecond=0)
        if now >= close_time:
            log.info("EOD: closing all positions")
            for ticker, pos in list(portfolio.positions.items()):
                side = "sell" if pos.side == "long" else "buy"
                await broker.submit_market_order(ticker, pos.qty, side)
            break
        await asyncio.sleep(30)


async def main():
    configure_logging()
    config = load_config()
    log.info("System starting. NAV=%.0f tickers=%d", config.account.nav, len(config.universe.tickers))

    broker = AlpacaBroker(config)
    acct = broker.get_account()
    log.info("Alpaca connected. Paper NAV=%.2f", acct["nav"])

    portfolio = Portfolio(nav=config.account.nav)
    regime_store = RegimeStore()
    chroma = ChromaStore()
    bar_store = BarStore()

    log.info("Backfilling bars...")
    for ticker in config.universe.tickers:
        try:
            bars = broker.get_bars(ticker, "1Min", limit=100)
            bar_store.backfill(ticker, bars)
        except Exception as e:
            log.error("Backfill failed for %s: %s", ticker, e)

    classifier = RegimeClassifier(config, chroma)
    news_watcher = NewsWatcher(config, classifier, regime_store)
    log.info("Running morning regime sweep...")
    await news_watcher.run_morning_sweep()

    signal_engine = SignalEngine(config, bar_store, regime_store)
    order_manager = OrderManager(config)
    executor = Executor(broker, portfolio, signal_engine, order_manager, None, config)

    log.info("Starting stream and watchers...")
    await asyncio.gather(
        stream.start(config.universe.tickers, executor.on_tick,
                     api_key=config.alpaca_api_key, secret_key=config.alpaca_secret_key),
        news_watcher.watch(),
        close_all_positions_eod(broker, portfolio, config),
    )


if __name__ == "__main__":
    asyncio.run(main())
