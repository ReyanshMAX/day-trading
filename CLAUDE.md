# Build Plan — Agentic Day Trading System

This document is the authoritative build plan. Follow it sequentially.
Do not skip phases. Do not build ahead. Each phase ends with a checkpoint
that must pass before proceeding.

Reference TRADING_SYSTEM_SPEC.md for all interface contracts, data schemas,
config values, and module-level detail. This document governs order and method.

---

## Build Status (as of 2026-04-10)

**IMPORTANT: `signal/` was renamed to `signals/` to avoid collision with Python stdlib `signal` module.**
All imports use `signals.*` not `signal.*`.

| Phase | Status | Notes |
|-------|--------|-------|
| 0 — Scaffold | ✅ DONE | All dirs, __init__.py, config.yaml, .gitignore, requirements.txt |
| 0.2 — Config loader | ✅ DONE | `core/config.py` — needs `.env` with real keys for checkpoint |
| 1 — Broker | ✅ DONE | `core/broker.py` — checkpoint requires live Alpaca paper keys |
| 2 — Bar store + indicators | ✅ DONE | `signals/bar_store.py`, `signals/indicators.py`, 9/9 tests pass |
| 3 — Signal scoring + engine | ✅ DONE | `signals/scoring.py`, `signals/engine.py`, 5/5 tests pass |
| 4 — Risk gate | ✅ DONE | `core/portfolio.py`, `risk/gate.py`, 7/7 tests pass |
| 5 — Regime classifier + ChromaDB | ✅ DONE | `memory/chroma_store.py`, `regime/classifier.py`, 5/5 tests pass |
| 6 — News watcher | ✅ DONE | `regime/news_watcher.py`, `regime/regime_store.py`, 4/4 tests pass |
| 7 — Order manager | ✅ DONE | `core/order_manager.py`, 7/7 tests pass |
| 8 — Executor | ✅ DONE | `execution/executor.py`, 6/6 tests pass |
| 9 — WebSocket stream | ✅ DONE | `core/stream.py` — manual smoke test required with live keys |
| 10 — Full boot + main.py | ✅ DONE | `main.py`, `core/logging_setup.py` — requires live keys to run |
| 11 — Hardening | ⏳ PENDING | Logging audit, edge cases, asset tradability already in executor |

**Total offline tests: 43/43 passing.** Run with: `pytest tests/ -m "not integration" -v`

**To run integration tests (requires .env with real keys):**
```
pytest tests/test_broker_connection.py -m integration -v
```

**To boot the system:**
```
cd trading-system && python main.py
```

---

---

## Ground Rules

- No `print()` anywhere. Use the `logging` module exclusively.
- All async functions must have full type hints on parameters and return types.
- Every module gets a docstring describing its single responsibility.
- Never hardcode values that exist in `config.yaml`. Always read from config.
- Never let a raw LLM output reach order logic. Validate and cast first.
- Write the test before wiring the module into the larger system.
- After each file is written, run its tests before moving on.
- Never commit `.env`. It is in `.gitignore` from step one.

---

## Phase 0 — Project Scaffold

### 0.1 Directory and environment setup

Create the full directory tree from the spec:

```
trading-system/
├── .env.example
├── .gitignore
├── config.yaml
├── main.py
├── requirements.txt
├── core/
├── signal/
├── regime/
├── risk/
├── memory/
├── execution/
├── tests/
└── logs/
```

Create all `__init__.py` files.

Create `.gitignore` containing at minimum:
```
.env
logs/
__pycache__/
*.pyc
.pytest_cache/
chroma_db/
```

Create `.env.example` with all keys blank so the structure is documented.

Create `config.yaml` with the full contents from the spec.

Install all dependencies from `requirements.txt`:
```bash
pip install -r requirements.txt
```

### 0.2 Config loader

Create `core/config.py`:
- Load `config.yaml` using PyYAML
- Load `.env` using `python-dotenv`
- Return a single `Config` dataclass that is passed by dependency injection
  to every module — no module reads env vars directly except this one
- Validate on load: assert all required env vars are present, assert NAV > 0,
  assert len(tickers) > 0

### Checkpoint 0
```bash
python -c "from core.config import load_config; c = load_config(); print(c.account.nav)"
```
Must print `100000` without errors.

---

## Phase 1 — Broker Connection

### 1.1 core/broker.py

Implement all functions from the spec using `alpaca-py`.

Use `alpaca.trading.client.TradingClient` for REST order operations.
Use `alpaca.data.historical.StockHistoricalDataClient` for bar fetching.

Do not implement the WebSocket stream here — that is Phase 5.

For `get_bars()`: return a `pd.DataFrame` with lowercase column names
`open, high, low, close, volume`. Index must be a `DatetimeIndex` in UTC.

For `submit_bracket_order()`: use `alpaca.trading.requests.MarketOrderRequest`
with `order_class="bracket"`, `stop_loss` and `take_profit` legs.
Return the raw `Order` object from alpaca-py.

### 1.2 Smoke test broker connection

Create `tests/test_broker_connection.py`:
- Call `broker.get_account()` and assert NAV is a positive float
- Call `broker.get_bars("AAPL", "1Min", 5)` and assert DataFrame has 5 rows
  and the required columns

This test hits the real Alpaca paper API. Mark it with `@pytest.mark.integration`
so it can be skipped in offline runs: `pytest -m "not integration"`.

### Checkpoint 1
```bash
pytest tests/test_broker_connection.py -m integration -v
```
Both tests must pass.

---

## Phase 2 — Signal Foundation

This phase has no network calls. All tests use synthetic data.

### 2.1 signal/bar_store.py

Implement the rolling bar buffer.

`BarStore` holds a `deque(maxlen=200)` of 1-min bars per ticker.

`update(ticker, price, volume, timestamp)`:
- Aggregate ticks into the current in-progress bar (update high/low/close, add to volume)
- When `timestamp.minute` changes, close the current bar and append to deque
- A bar is a dict: `{open, high, low, close, volume, timestamp}`

`get_bars(ticker, n) -> pd.DataFrame`:
- Return last `n` closed bars as DataFrame with DatetimeIndex
- If fewer than `n` bars exist, return what is available without error

`backfill(ticker, df: pd.DataFrame)`:
- Accepts a DataFrame from `broker.get_bars()` and loads it into the deque

`get_current_price(ticker) -> float | None`:
- Returns the last seen price from the current in-progress bar

### 2.2 signal/indicators.py

Implement every function from the spec as a stateless function.

Implementation notes:

`vwap(df)`:
- Only use bars where `df.index.date == df.index[-1].date()` — session bars only
- Formula: `cumsum(typical_price * volume) / cumsum(volume)`
- `typical_price = (high + low + close) / 3`

`vwap_bands(df, deviations)`:
- Standard deviation: rolling std of `(typical_price - vwap)` weighted by volume
- Return dict keyed by string: `{"+1.0": Series, "-1.0": Series, "+2.0": Series, ...}`

`atr(df, period) -> float`:
- Use pandas-ta: `pandas_ta.atr(df.high, df.low, df.close, length=period)`
- Return the last value as a scalar float

`rsi(df, period) -> float`:
- Use pandas-ta. Return last value as scalar float.

`orb(df, window_minutes) -> tuple[float | None, float | None]`:
- Filter bars where `bar.timestamp.time() >= 09:30` and `bar.timestamp.time() < 09:30 + window_minutes`
- If fewer than `window_minutes` bars in that window, return `(None, None)`
- Otherwise return `(max(highs), min(lows))` of those bars

`fibonacci_levels(swing_high, swing_low) -> dict`:
- Range = swing_high - swing_low
- Retracements: `swing_high - ratio * range` for each ratio
- Extensions: `swing_high + (ratio - 1.0) * range` for each ratio

`detect_swing_high(df, lookback) -> float`:
- Return the highest high in the last `lookback` bars

`detect_swing_low(df, lookback) -> float`:
- Return the lowest low in the last `lookback` bars

### 2.3 tests/test_indicators.py

Write tests before finalizing `indicators.py`. Use a synthetic DataFrame:
```python
import pandas as pd, numpy as np

def make_bars(n=100, seed=42):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    high = close + rng.uniform(0.1, 0.5, n)
    low = close - rng.uniform(0.1, 0.5, n)
    open_ = close - rng.uniform(-0.3, 0.3, n)
    volume = rng.integers(10000, 100000, n).astype(float)
    idx = pd.date_range("2024-01-15 09:30", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": volume}, index=idx)
```

Required test cases:
- `ema(df, 9)` returns a Series of length n, no NaN in last 50 rows
- `vwap(df)` returns a Series, all values positive, resets correctly at session boundary
- `atr(df, 14)` returns a positive float
- `rsi(df, 14)` returns a float between 0 and 100
- `orb(df, 15)` returns two floats when 15+ bars exist, returns `(None, None)` when fewer
- `fibonacci_levels(110, 100)` — assert 0.618 retracement is approximately 93.82
- `detect_swing_high(df, 20)` returns a float >= all close values in last 20 bars

### Checkpoint 2
```bash
pytest tests/test_indicators.py -v
```
All tests must pass.

---

## Phase 3 — Signal Scoring

### 3.1 signal/scoring.py

Implement `compute_score()` exactly as specified.

Define `IndicatorSnapshot` as a `dataclass` in this file.

Scoring must be deterministic: same inputs always produce same output.
No randomness, no LLM, no I/O.

The function must handle `orb_high=None` and `orb_low=None` by
simply omitting the ORB component from the score (do not treat as 0).
Renormalize weights accordingly when ORB is unavailable.

### 3.2 tests/test_signal_scoring.py

Test cases:
- Perfect trending long setup (all bullish signals) → score > 0.8
- Perfect ranging oversold setup → score > 0.5 in ranging mode
- All neutral indicators → score near 0.0
- `regime="avoid"` passed to engine → score returns None (test via engine, not scorer)
- ORB components omitted when `orb_high=None` → still returns valid score, no exception

### Checkpoint 3
```bash
pytest tests/test_signal_scoring.py -v
```
All tests must pass.

---

## Phase 4 — Risk Gate

### 4.1 core/portfolio.py

Implement `Portfolio` class. For now, use mock state (no broker calls).
The portfolio will be connected to live order fills in Phase 7.

`open_risk()` must sum `abs(avg_entry - stop) * qty` for all open positions.

Sector mapping: hardcode a dict in `portfolio.py`:
```python
SECTOR_MAP = {
    "NVDA": "tech", "AMD": "tech", "AAPL": "tech", "MSFT": "tech",
    "META": "tech", "GOOGL": "tech", "AMZN": "tech",
    "TSLA": "consumer", "COIN": "crypto", "MSTR": "crypto",
    "PLTR": "tech", "SOFI": "finance", "ARKK": "etf",
    "SPY": "etf", "QQQ": "etf"
}
```

### 4.2 risk/gate.py

Implement all five checks in order from the spec.

The gate must be a pure function — no state, no side effects, no I/O.
Pass everything it needs as arguments.

### 4.3 tests/test_risk_gate.py

Test every rejection scenario explicitly:
- Daily loss limit flag already set → REJECT
- daily_pnl_pct crosses -3% threshold → REJECT and flag is set
- portfolio heat at 6.1% → REJECT
- trade risk > 1% NAV → REJECT
- sector count at max → REJECT
- all clear → APPROVE

Also test that gate does not mutate portfolio state — it only reads it.

### Checkpoint 4
```bash
pytest tests/test_risk_gate.py -v
```
All tests must pass with zero mutations to portfolio in rejection cases.

---

## Phase 5 — Regime Classifier

### 5.1 memory/chroma_store.py

Implement ChromaDB store first because the classifier depends on it for
few-shot context retrieval.

Use `chromadb.PersistentClient(path="chroma_db/")`.

Collection name: `"regime_outcomes"`.

`store_classification()` — upsert with document ID `"{ticker}_{date}"`.

`get_similar_contexts(ticker, headlines, n=2)`:
- Query string: join headlines into a single string
- Return top `n` results formatted as:
  ```
  Past example: {ticker} on {date} — regime={regime}, conviction={conviction}
  Catalyst: {catalyst}
  Outcome: {outcome} ({pnl_pct:+.2f}%)
  ```
- If collection is empty or has fewer than `n` entries, return whatever exists

### 5.2 regime/classifier.py

Implement `classify()` as an async function.

Groq client: `from groq import AsyncGroq`. Initialize once, reuse.

JSON validation:
```python
import json

try:
    raw = response.choices[0].message.content.strip()
    data = json.loads(raw)
    assert data["regime"] in ("trending", "ranging", "avoid")
    assert 1 <= data["conviction"] <= 5
    assert data["direction"] in ("bullish", "bearish", "neutral")
except (json.JSONDecodeError, KeyError, AssertionError) as e:
    log.error(f"Classifier JSON invalid for {ticker}: {e} | raw: {raw}")
    return fallback_regime(prior_regime)
```

`fallback_regime()` returns `RegimeState(regime="ranging", conviction=2, direction="neutral", catalyst="classifier error")`.

### 5.3 tests/test_regime_classifier.py

Mock the Groq client using `unittest.mock.AsyncMock`.

Test cases:
- Valid JSON response → returns correct `RegimeState`
- Malformed JSON → returns fallback state, logs error
- Valid JSON but `conviction=7` (out of range) → returns fallback
- `regime="TRENDING"` (wrong case) → returns fallback
- Groq API timeout → returns fallback, does not raise

### Checkpoint 5
```bash
pytest tests/test_regime_classifier.py -v
```
All tests must pass. Classifier must never raise an unhandled exception.

---

## Phase 6 — News Watcher

### 6.1 regime/news_watcher.py

Implement `NewsWatcher`.

Alpaca news endpoint: `GET /v1beta1/news?symbols={ticker}&limit=10`
Use `alpaca-py`'s `alpaca.data.historical.news.NewsClient`.

`run_morning_sweep()`:
- Calls `classify()` for all tickers concurrently using `asyncio.gather()`
- Do not call them sequentially — all 15 should fire in parallel
- Store results in `regime_store`

`watch()` async loop:
- Sleep `news_poll_interval_seconds` between full passes
- Per ticker: fetch headlines, hash each headline's `headline` field with `hashlib.md5`
- Only trigger `classify()` if at least one new hash is found
- Update hash set after triggering

### 6.2 tests/test_news_watcher.py

Mock `NewsClient` and `RegimeClassifier`.

Test cases:
- First poll with 3 headlines → classifier called once, all 3 hashes stored
- Second poll with same 3 headlines → classifier NOT called
- Second poll with 1 new headline added → classifier called once
- `run_morning_sweep()` → classifier called for all 15 tickers

### Checkpoint 6
```bash
pytest tests/test_news_watcher.py -v
```
Hash deduplication must be airtight.

---

## Phase 7 — Order Manager

### 7.1 core/order_manager.py

Implement `build_bracket()` and `compute_base_size()`.

Fibonacci snapping logic:
```python
def snap_to_fib(price: float, fib_levels: list[float], tolerance_pct: float = 0.003) -> float:
    for level in fib_levels:
        if abs(price - level) / price < tolerance_pct:
            return level
    return price
```

Call `snap_to_fib` on both the computed stop and target after ATR calculation.
Pass the appropriate fib level list (retracements for stop, extensions for target).

`build_bracket()` must never return a `stop` that is on the wrong side of entry.
Assert: for long, `stop < entry < target`. For short, `target < entry < stop`.
Raise `ValueError` if this invariant is violated — it indicates a config error.

`compute_base_size()` must return at least 1. If ATR-derived size is 0 due to
a very wide stop, return 1 and log a warning.

### 7.2 tests/test_order_manager.py

Test cases:
- Long signal, trending regime, conviction 4: assert stop < entry < target
- Short signal: assert target < entry < stop
- Fibonacci snap: stop within 0.3% of a fib level → snaps to it
- Fibonacci no-snap: stop far from all levels → unchanged
- Very wide stop → qty = 1, warning logged
- `conviction=0` not possible (gate catches earlier), but conviction=1 →
  size_multiplier from config applied correctly

### Checkpoint 7
```bash
pytest tests/test_order_manager.py -v
```
Bracket invariant test must pass for all signal directions.

---

## Phase 8 — Executor Integration Test

### 8.1 execution/executor.py

Implement `Executor` class with `on_tick()` method.

Dependency inject all components via `__init__`. No module-level globals.

### 8.2 tests/test_executor.py

This is the most important integration test. Mock every dependency.

```python
@pytest.mark.asyncio
async def test_full_tick_to_order_path():
    # Arrange: mock broker, portfolio (no position), regime (trending, conviction 4),
    #          signal engine (returns score=0.7, long), risk gate (approve)
    # Act: call executor.on_tick("NVDA", 910.0, 50000, now)
    # Assert: broker.submit_bracket_order called exactly once with correct direction
```

Additional cases:
- Signal engine returns `None` → broker never called
- Ticker already has open position → broker never called
- Risk gate rejects → broker never called
- Broker raises exception → exception is caught, logged, does not crash executor

### Checkpoint 8
```bash
pytest tests/test_executor.py -v
```
Broker must not be called in any rejection path.

---

## Phase 9 — WebSocket Stream

### 9.1 core/stream.py

Implement using `alpaca-py`'s `alpaca.data.live.StockDataStream`.

```python
async def start(tickers: list[str], on_tick_callback):
    stream = StockDataStream(api_key, secret_key)

    async def handler(data):
        await on_tick_callback(
            data.symbol, float(data.price),
            float(data.size), data.timestamp
        )

    stream.subscribe_trades(handler, *tickers)
    await stream.run()
```

Reconnect logic — wrap `stream.run()` in a retry loop:
```python
retries = 0
max_retries = 5
while retries < max_retries:
    try:
        await stream.run()
    except Exception as e:
        retries += 1
        wait = 2 ** retries
        log.error(f"Stream disconnected: {e}. Retrying in {wait}s ({retries}/{max_retries})")
        await asyncio.sleep(wait)
log.critical("Stream failed after max retries. Manual restart required.")
```

### 9.2 Manual smoke test

No automated test for the stream — it requires a live connection.
Run this manually and observe logs for 2 minutes:
```bash
python -c "
import asyncio
from core.config import load_config
from core.stream import start

async def dummy(ticker, price, volume, ts):
    print(f'{ticker} {price}')

asyncio.run(start(load_config().universe.tickers[:3], dummy))
"
```
Confirm ticks are arriving for at least 2 tickers.

### Checkpoint 9
Ticks flowing in logs for at least 2 tickers. No unhandled exceptions.

---

## Phase 10 — Full System Boot

### 10.1 main.py

Implement the full startup sequence from the spec.

Add the end-of-day close-all coroutine:
```python
async def close_all_positions_eod(broker, portfolio, config):
    while True:
        now = datetime.now(tz=timezone("America/New_York"))
        close_time = now.replace(hour=15, minute=55, second=0, microsecond=0)
        if now >= close_time:
            log.info("EOD: closing all positions")
            for ticker, pos in portfolio.positions.items():
                side = "sell" if pos.side == "long" else "buy"
                await broker.submit_market_order(ticker, pos.qty, side)
            break
        await asyncio.sleep(30)
```

Add `broker.submit_market_order()` to `core/broker.py` for this purpose.

### 10.2 First paper trading run

Run the system for a full trading session (or at minimum 30 minutes).

Monitor `logs/trading.log` for:
- At least one regime classification per ticker at open
- Tick processing confirms in debug log
- At least one gate evaluation logged
- No unhandled exceptions

If orders fire, verify them in the Alpaca paper trading dashboard.

### Checkpoint 10
System runs for 30 minutes without crashing.
At least one order attempt logged (approved or rejected — either is fine).
EOD close fires correctly at 3:55 PM if run through end of session.

---

## Phase 11 — Hardening

Only begin this phase after Phase 10 checkpoint passes.

### 11.1 Logging audit
- Confirm every gate rejection has a log line with ticker, reason, and timestamp
- Confirm every order fire has score, regime, conviction, stop, target
- Confirm every LLM call logs token usage (from Groq response metadata)

### 11.2 Edge cases to manually verify
- What happens if Alpaca paper API is down at startup? System should exit cleanly with a log message, not hang.
- What happens if a ticker is not tradable today (halted)? Gate should catch it via `broker.get_asset()` check — add this to the gate or executor.
- What happens if `config.yaml` has a ticker with no news coverage? News watcher returns empty list, no LLM call, regime stays at morning classification.

### 11.3 Add asset tradability check

In `executor.on_tick()`, before calling the signal engine, add:
```python
if not await broker.is_tradable(ticker):
    return
```

Add `is_tradable(ticker) -> bool` to `core/broker.py`:
- Calls `GET /v2/assets/{ticker}`
- Returns `asset.tradable and asset.status == "active"`
- Cache result per ticker for the session (don't call on every tick)

---

## What Not to Build Yet

These are out of scope for the initial build. Do not add them proactively:

- Short selling (long-only first, validate the system works)
- Multiple timeframe analysis (1-min only first)
- Options or futures
- Live account (paper only until system has 30+ days of logged performance)
- A UI or dashboard
- Any attempt to fine-tune or train a model

---

## Definition of Done

The system is considered built when:

- All phase checkpoints pass
- `pytest` runs clean with zero failures: `pytest tests/ -m "not integration" -v`
- System runs a full paper trading session without crashing
- Every order in the Alpaca dashboard has a corresponding log line with full context
- `logs/trading.log` is human-readable enough to diagnose any trade after the fact