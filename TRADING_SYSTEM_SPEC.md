# Agentic Day Trading System — Claude Code Spec

## Project Overview

A paper-trading algorithmic day trading system for 15 stocks on a $100k Alpaca paper account.
The system combines a real-time quant signal engine with an event-driven LLM sentiment layer.
The LLM never touches price numbers. All entry/exit levels, sizing, and order logic are pure Python.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| Broker / Data | Alpaca Markets (paper account) — REST + WebSocket |
| LLM Inference | Groq API (llama-3.1-8b-instant or 70b) |
| Vector Memory | ChromaDB (local persistent) |
| News Feed | Alpaca News API + Benzinga (via Alpaca) |
| Indicators | pandas-ta |
| Scheduling | asyncio (no APScheduler) |
| Config | python-dotenv + YAML |
| Logging | Python logging + rotating file handler |
| Testing | pytest |

---

## Environment Variables (.env)

```
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_DATA_URL=https://data.alpaca.markets
GROQ_API_KEY=
```

---

## Project Structure

```
trading-system/
├── .env
├── config.yaml
├── main.py                         # Entry point — boots all async loops
├── requirements.txt
│
├── core/
│   ├── __init__.py
│   ├── broker.py                   # Alpaca REST client wrapper
│   ├── stream.py                   # Alpaca WebSocket tick stream
│   ├── portfolio.py                # Position tracking, NAV, open risk calc
│   └── order_manager.py            # Bracket order construction and lifecycle
│
├── signal/
│   ├── __init__.py
│   ├── engine.py                   # Per-tick signal score computation
│   ├── indicators.py               # EMA, VWAP, ATR, RSI, MACD, OBV, ORB, Fibonacci
│   ├── bar_store.py                # Rolling 1-min bar buffer per ticker
│   └── scoring.py                  # Composite signal score (-1.0 to 1.0)
│
├── regime/
│   ├── __init__.py
│   ├── classifier.py               # LLM regime classification (Groq)
│   ├── news_watcher.py             # News polling + hash-based change detection
│   └── regime_store.py             # In-memory regime state per ticker
│
├── risk/
│   ├── __init__.py
│   └── gate.py                     # All risk checks — per-trade and portfolio
│
├── memory/
│   ├── __init__.py
│   └── chroma_store.py             # ChromaDB — regime fingerprints + news patterns
│
├── execution/
│   ├── __init__.py
│   └── executor.py                 # Tick handler — checks signal, gates, fires orders
│
└── tests/
    ├── test_indicators.py
    ├── test_signal_engine.py
    ├── test_risk_gate.py
    └── test_regime_classifier.py
```

---

## config.yaml

```yaml
universe:
  tickers:
    - NVDA
    - AAPL
    - MSFT
    - TSLA
    - META
    - GOOGL
    - AMZN
    - AMD
    - SPY
    - QQQ
    - SOFI
    - PLTR
    - COIN
    - MSTR
    - ARKK

account:
  paper: true
  nav: 100000

risk:
  max_trade_risk_pct: 0.01        # 1% of NAV per trade
  max_portfolio_heat_pct: 0.06    # 6% total open risk across all positions
  max_sector_positions: 4         # max concurrent positions in same sector
  daily_loss_limit_pct: 0.03      # -3% NAV = full stop for the day

signal:
  entry_threshold: 0.55           # signal score must exceed this to trigger
  atr_period: 14
  ema_fast: 9
  ema_slow: 21
  rsi_period: 14
  vwap_deviation_bands: [1.0, 2.0, 2.5]
  orb_window_minutes: 15          # opening range = first 15 min candles

regime:
  news_poll_interval_seconds: 120
  min_conviction_to_trade: 3      # LLM conviction score must be >= this

# Risk/reward ratios by regime
rr_profiles:
  trending:
    stop_atr_mult: 1.5
    target_atr_mult: 3.0
    size_multiplier_by_conviction:
      1: 0.25
      2: 0.5
      3: 0.75
      4: 1.0
      5: 1.25
  ranging:
    stop_atr_mult: 1.0
    target_atr_mult: 1.5
    size_multiplier_by_conviction:
      1: 0.0
      2: 0.25
      3: 0.5
      4: 0.75
      5: 1.0
```

---

## Module Specifications

---

### core/broker.py

Thin wrapper around `alpaca-trade-api` or `alpaca-py`.

**Functions:**
- `get_account() -> dict` — returns NAV, buying power, cash
- `get_positions() -> list[Position]` — all current open positions
- `get_open_orders() -> list[Order]`
- `cancel_order(order_id: str)`
- `submit_bracket_order(ticker, qty, side, stop_price, take_profit_price) -> Order`
- `get_bars(ticker, timeframe, limit) -> pd.DataFrame` — columns: open, high, low, close, volume, vwap

---

### core/stream.py

Manages the Alpaca WebSocket data stream.

**Responsibilities:**
- Subscribe to trade updates for all tickers in universe
- On each tick, call `executor.on_tick(ticker, price, volume, timestamp)`
- Auto-reconnect with exponential backoff on disconnect
- Log dropped ticks

**Key detail:** Use `alpaca-py`'s async data stream. Run inside `asyncio` event loop started in `main.py`.

---

### core/portfolio.py

Tracks live position and risk state. Updated on every order fill event.

**State:**
```python
positions: dict[str, Position]   # ticker -> Position(qty, avg_entry, stop, target, side)
daily_pnl: float
daily_loss_limit_hit: bool
```

**Functions:**
- `open_risk() -> float` — sum of (entry - stop) * qty across all open longs (and inverse for shorts)
- `open_risk_pct() -> float` — open_risk / NAV
- `sector_count(sector: str) -> int`
- `record_fill(order: Order)`
- `record_close(order: Order)`
- `daily_pnl_pct() -> float`

---

### core/order_manager.py

Constructs and tracks bracket orders.

**Functions:**
- `build_bracket(ticker, signal_score, regime, atr, current_price, nav) -> BracketParams`
  - Looks up `rr_profiles[regime]` from config
  - Applies conviction multiplier to base position size
  - Computes `stop = entry - stop_atr_mult * atr` (long) or inverse (short)
  - Computes `target = entry + target_atr_mult * atr` (long) or inverse (short)
  - Checks nearest Fibonacci level — if a fib retracement is within 0.3% of computed stop, snap stop to it. Same for target and fib extension.
  - Returns `BracketParams(qty, stop, target)`
- `compute_base_size(nav, stop_distance) -> int`
  - `risk_dollars = nav * max_trade_risk_pct`
  - `qty = floor(risk_dollars / stop_distance)`

---

### signal/bar_store.py

Rolling buffer of 1-minute OHLCV bars per ticker.

**State:**
```python
bars: dict[str, deque[Bar]]   # ticker -> deque maxlen=200
```

**Functions:**
- `update(ticker, tick)` — aggregates ticks into current incomplete bar, closes bar on minute boundary
- `get_bars(ticker, n) -> pd.DataFrame` — last n closed bars
- `get_current_price(ticker) -> float`

On startup, backfill each ticker with the last 100 1-min bars from Alpaca REST before the stream starts.

---

### signal/indicators.py

Stateless functions. All take a `pd.DataFrame` with columns `open, high, low, close, volume`.

**Functions:**
- `ema(df, period) -> pd.Series`
- `vwap(df) -> pd.Series` — resets daily, computed from intraday bars only
- `vwap_bands(df, deviations: list[float]) -> dict[str, pd.Series]` — e.g. `{"+1": ..., "-1": ...}`
- `atr(df, period) -> float` — returns scalar (current ATR)
- `rsi(df, period) -> float` — returns scalar
- `macd(df) -> tuple[float, float, float]` — returns (macd_line, signal_line, histogram)
- `obv(df) -> pd.Series`
- `rvol(df, lookback=20) -> float` — current bar volume / avg volume at this time of day
- `orb(df, window_minutes=15) -> tuple[float, float]` — returns (orb_high, orb_low) using first N bars after 9:30 AM. Returns (None, None) before ORB window closes.
- `fibonacci_levels(swing_high, swing_low) -> dict` — returns retracement and extension levels
  ```python
  {
    "retracements": {0.236: price, 0.382: price, 0.5: price, 0.618: price, 0.786: price},
    "extensions":   {1.272: price, 1.618: price, 2.618: price}
  }
  ```
- `detect_swing_high(df, lookback=20) -> float`
- `detect_swing_low(df, lookback=20) -> float`

---

### signal/scoring.py

Computes composite signal score from indicator values and regime.

**Function:**
```python
def compute_score(indicators: IndicatorSnapshot, regime: RegimeState) -> float
```

**IndicatorSnapshot fields:**
```python
ema_fast: float
ema_slow: float
vwap: float
current_price: float
rsi: float
macd_line: float
macd_signal: float
rvol: float
orb_high: float | None
orb_low: float | None
atr: float
```

**Scoring logic by regime:**

Trending regime — weight set:
```
+0.25  if ema_fast > ema_slow                    (EMA crossover bullish)
+0.20  if price > vwap                           (above VWAP)
+0.15  if orb_high and price > orb_high          (ORB breakout)
+0.15  if rvol > 1.5                             (volume confirmation)
+0.10  if 40 < rsi < 70                          (momentum not exhausted)
+0.15  if macd_line > macd_signal                (MACD confirmation)
```
Negate all for short signals. Sum = raw score, clamp to [-1.0, 1.0].

Ranging regime — weight set:
```
+0.35  if price < vwap - 1.0*std_dev             (mean reversion long)
+0.25  if rsi < 35                               (oversold)
+0.20  if rvol > 1.2
+0.20  if price near orb support
```

Score is always directional: positive = long bias, negative = short bias.
Only fire entry when `abs(score) > entry_threshold` from config.

---

### signal/engine.py

Called on every tick from the executor.

```python
def on_tick(ticker: str, price: float, volume: float) -> SignalResult | None
```

1. Update `bar_store` with tick
2. Fetch last 100 bars from `bar_store`
3. Compute all indicators via `indicators.py`
4. Fetch regime from `regime_store`
5. If `regime == "avoid"` or `conviction < min_conviction_to_trade`: return None
6. Call `scoring.compute_score()` → score
7. If `abs(score) < entry_threshold`: return None
8. Return `SignalResult(ticker, score, direction, atr, indicators)`

---

### regime/classifier.py

Calls Groq to classify a stock's regime.

**Function:**
```python
async def classify(ticker: str, headlines: list[str], prior_regime: str | None) -> RegimeState
```

**Prompt (system):**
```
You are a market regime classifier for intraday trading. 
Respond ONLY with valid JSON. No explanation, no markdown.
```

**Prompt (user):**
```
Stock: {ticker}
Current time: {time}
Recent headlines (last 6 hours): {headlines}
Prior regime: {prior_regime or "unknown"}

Classify this stock for today's intraday trading session.

Output exactly:
{
  "regime": "trending" | "ranging" | "avoid",
  "direction": "bullish" | "bearish" | "neutral",
  "conviction": 1 | 2 | 3 | 4 | 5,
  "catalyst": "<one sentence max>",
  "avoid_reason": "<only if regime is avoid, else null>"
}

Regime definitions:
- trending: clear directional bias, likely to continue intraday
- ranging: oscillating around a mean, no clear direction
- avoid: earnings today/tomorrow, trading halt risk, very low liquidity, or major conflicting signals
```

**Output validation:**
- Parse JSON strictly. If parsing fails, log error and return prior regime or default `{regime: "ranging", conviction: 2}`.
- Never let a raw float from LLM output reach order logic.

**Few-shot context from ChromaDB:**
Before calling Groq, query `chroma_store.get_similar_contexts(ticker, headlines)` and prepend top 2 results as examples in the prompt.

---

### regime/news_watcher.py

Polls Alpaca News API every 120 seconds per ticker.

**State:**
```python
headline_hashes: dict[str, set[str]]   # ticker -> set of MD5 hashes of seen headlines
```

**Loop:**
```python
async def watch():
    while True:
        for ticker in universe:
            headlines = await fetch_headlines(ticker, lookback_hours=4)
            new_headlines = [h for h in headlines if hash(h) not in seen]
            if new_headlines:
                update seen set
                trigger classifier.classify(ticker, all_headlines)
        await asyncio.sleep(news_poll_interval_seconds)
```

---

### regime/regime_store.py

Simple in-memory dict with thread-safe read/write.

```python
store: dict[str, RegimeState]

def get(ticker: str) -> RegimeState
def set(ticker: str, state: RegimeState)
```

---

### risk/gate.py

Hard-coded logic. No LLM. No exceptions.

```python
def check(ticker: str, direction: str, qty: int, stop_distance: float,
          portfolio: Portfolio, config: Config) -> GateResult
```

**Checks in order:**
1. `daily_loss_limit_hit` → REJECT, reason: "daily loss limit"
2. `portfolio.daily_pnl_pct() < -config.daily_loss_limit_pct` → set flag, REJECT
3. `portfolio.open_risk_pct() >= config.max_portfolio_heat_pct` → REJECT, reason: "portfolio heat"
4. `trade_risk = qty * stop_distance; trade_risk / nav > config.max_trade_risk_pct` → REJECT, reason: "trade risk"
5. `portfolio.sector_count(get_sector(ticker)) >= config.max_sector_positions` → REJECT, reason: "sector concentration"
6. All pass → APPROVE

**GateResult:**
```python
@dataclass
class GateResult:
    approved: bool
    reason: str | None
```

Log every rejection with reason, ticker, and timestamp.

---

### memory/chroma_store.py

Stores past regime classification outcomes for few-shot retrieval.

**Collection schema:**
```
collection: "regime_outcomes"
document: "{ticker} | {catalyst} | {headlines_summary}"
metadata: {
  ticker: str,
  regime: str,
  direction: str,
  conviction: int,
  outcome: "profitable" | "unprofitable" | "pending",
  pnl_pct: float,
  date: str
}
```

**Functions:**
- `store_classification(ticker, regime_state, headlines)` — called after every LLM classification
- `update_outcome(ticker, date, pnl_pct)` — called when a position closes, updates outcome
- `get_similar_contexts(ticker, headlines, n=2) -> list[str]` — semantic search, returns formatted strings for few-shot prompt injection

---

### execution/executor.py

The hot path. Called on every tick from `stream.py`.

```python
async def on_tick(ticker: str, price: float, volume: float, timestamp: datetime):
    # 1. Run signal engine
    signal = signal_engine.on_tick(ticker, price, volume)
    if signal is None:
        return

    # 2. Skip if already in a position for this ticker
    if portfolio.has_position(ticker):
        return

    # 3. Build bracket params
    bracket = order_manager.build_bracket(
        ticker, signal.score, signal.regime, signal.atr, price, portfolio.nav
    )

    # 4. Risk gate
    gate = risk_gate.check(ticker, signal.direction, bracket.qty,
                           bracket.stop_distance, portfolio, config)
    if not gate.approved:
        log.debug(f"Gate rejected {ticker}: {gate.reason}")
        return

    # 5. Submit order
    order = await broker.submit_bracket_order(
        ticker, bracket.qty, signal.direction,
        bracket.stop, bracket.target
    )
    portfolio.record_fill(order)
    log.info(f"Order fired: {ticker} {signal.direction} qty={bracket.qty} "
             f"stop={bracket.stop:.2f} target={bracket.target:.2f} "
             f"score={signal.score:.3f} regime={signal.regime} conviction={signal.conviction}")
```

---

### main.py

Boots everything and runs the async event loop.

```python
async def main():
    config = load_config("config.yaml")
    broker = AlpacaBroker(config)
    portfolio = Portfolio(broker)
    regime_store = RegimeStore()
    chroma = ChromaStore()
    bar_store = BarStore()

    # Backfill bars
    for ticker in config.universe.tickers:
        bars = broker.get_bars(ticker, "1Min", limit=100)
        bar_store.backfill(ticker, bars)

    # Morning regime sweep
    classifier = RegimeClassifier(config, chroma)
    news_watcher = NewsWatcher(config, classifier, regime_store)
    await news_watcher.run_morning_sweep()   # classifies all 15 tickers at open

    # Build executor
    signal_engine = SignalEngine(config, bar_store, regime_store)
    order_manager = OrderManager(config)
    risk_gate = RiskGate(config)
    executor = Executor(broker, portfolio, signal_engine, order_manager, risk_gate, config)

    # Start async tasks
    await asyncio.gather(
        stream.start(config.universe.tickers, executor.on_tick),
        news_watcher.watch(),
    )

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Startup Sequence

```
1. Load .env and config.yaml
2. Connect to Alpaca REST, verify paper account
3. Backfill last 100 1-min bars per ticker
4. Run morning regime sweep (LLM classifies all 15 tickers)
5. Start news watcher loop (async)
6. Start WebSocket tick stream (async)
7. Executor begins processing ticks
```

---

## Data Flow Diagram

```
Tick arrives (WebSocket)
        |
        v
  bar_store.update()
        |
        v
  signal_engine.on_tick()
    |           |
    v           v
indicators  regime_store.get()
    |           |
    v           v
  scoring.compute_score()
        |
        v
  score > threshold?  --NO--> return
        |
       YES
        |
        v
  order_manager.build_bracket()
        |
        v
  risk_gate.check()  --REJECT--> log + return
        |
      APPROVE
        |
        v
  broker.submit_bracket_order()
        |
        v
  portfolio.record_fill()


News poll (every 120s)
        |
  new headline detected?  --NO--> sleep
        |
       YES
        |
        v
  chroma_store.get_similar_contexts()
        |
        v
  classifier.classify() [Groq]
        |
        v
  regime_store.set()
  chroma_store.store_classification()
```

---

## Key Constraints and Rules

- The LLM never outputs a float that reaches an order. All price levels are computed from ATR in `order_manager.py`.
- `risk/gate.py` has no import from `regime/` — it is regime-unaware. Regime filtering happens in `signal/engine.py` before the gate is ever called.
- All Groq responses are validated as JSON before use. Invalid responses fall back to prior regime or safe default.
- WebSocket reconnect logic is required from day one. Alpaca paper stream drops connections. Use exponential backoff, max 5 retries.
- VWAP resets at 9:30 AM daily. `bar_store` must track session start and `indicators.vwap()` must only use bars from current session.
- ORB levels are not available until 9:45 AM (first 15 minutes closed). Signal engine must handle `orb_high = None` gracefully — treat as no ORB signal, not zero.
- No position is ever held overnight. Add a hard close-all at 3:55 PM in `main.py` via a scheduled coroutine.

---

## Logging

Use Python `logging` with a `RotatingFileHandler`. Two log files:

- `logs/trading.log` — all INFO+ events (order fires, regime changes, gate rejections)
- `logs/debug.log` — DEBUG level (every tick signal score, indicator values)

Log format:
```
2025-01-15 10:23:45.123 | INFO | executor | NVDA LONG qty=12 stop=891.40 target=912.60 score=0.712 regime=trending conviction=4
```

---

## requirements.txt

```
alpaca-py>=0.21.0
groq>=0.9.0
pandas>=2.0.0
pandas-ta>=0.3.14b
chromadb>=0.5.0
python-dotenv>=1.0.0
pyyaml>=6.0
numpy>=1.26.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

---

## Build Order for Claude Code

Implement modules in this order to allow incremental testing at each step:

1. `core/broker.py` — verify Alpaca connection and basic REST calls
2. `signal/bar_store.py` + `signal/indicators.py` — unit test all indicators with synthetic data
3. `signal/scoring.py` — unit test score outputs for known indicator combinations
4. `signal/engine.py` — integration test with mocked bar data
5. `core/portfolio.py` + `risk/gate.py` — unit test all gate rejection scenarios
6. `regime/classifier.py` — test with mocked Groq response, validate JSON parsing and fallback
7. `memory/chroma_store.py` — test store + retrieve cycle
8. `regime/news_watcher.py` — test hash deduplication logic
9. `core/order_manager.py` — test bracket construction, Fibonacci snapping, size calc
10. `execution/executor.py` — integration test full tick-to-order path with mocked broker
11. `core/stream.py` — connect to Alpaca paper stream, verify tick delivery
12. `main.py` — full system boot, paper trading