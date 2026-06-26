# Agentic Day Trading System

An automated, long-only intraday trading system for US equities. It streams
live trades from Alpaca, scores technical setups, classifies market regime with
an LLM, gates every trade through hard risk limits, and submits bracket orders —
all running on the Alpaca **paper** account.

> Paper trading only. Do not point this at a live account until it has logged
> 30+ days of clean paper performance.

## Architecture

Tick-driven pipeline. A live trade tick flows through:

```
StockDataStream → Executor.on_tick
                    ├─ is_tradable? ──────────────── broker
                    ├─ SignalEngine.score ─────────── signals/ (indicators, scoring)
                    ├─ RegimeStore.get ────────────── regime/ (LLM classifier + news)
                    ├─ RiskGate.evaluate ──────────── risk/ + core/portfolio
                    └─ OrderManager.build_bracket ─── core/order_manager → broker
```

| Module | Responsibility |
|--------|----------------|
| `core/config.py` | Loads `config.yaml` + `.env` into an injected `Config` dataclass |
| `core/broker.py` | Alpaca REST: account, bars, bracket/market orders, tradability |
| `core/stream.py` | Live trade WebSocket with reconnect/backoff |
| `core/portfolio.py` | Open positions, portfolio heat, sector exposure |
| `core/order_manager.py` | Position sizing, ATR stops/targets, Fibonacci snapping |
| `signals/bar_store.py` | Rolling 1-min bar buffer per ticker |
| `signals/indicators.py` | VWAP, ATR, RSI, EMA, ORB, Fibonacci (stateless) |
| `signals/scoring.py` + `engine.py` | Deterministic setup score → long/short/None |
| `regime/classifier.py` | Groq LLM regime classification (trending/ranging/avoid) |
| `regime/news_watcher.py` | Polls Alpaca news, dedupes headlines, retriggers classify |
| `memory/chroma_store.py` | ChromaDB store of past regime outcomes for few-shot context |
| `risk/gate.py` | Pure-function risk checks (daily loss, heat, per-trade, sector) |
| `execution/executor.py` | Wires everything together per tick |
| `main.py` | Boot sequence + EOD close-all coroutine |

## Setup

```bash
cd trading-system
pip install -r requirements.txt
cp .env.example .env   # then fill in your Alpaca paper + Groq keys
```

Required keys in `.env`:

- `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` — Alpaca **paper** trading keys
- `GROQ_API_KEY` — for the regime classifier

All tunable parameters (NAV, tickers, risk limits, weights, intervals) live in
`config.yaml`. Nothing is hardcoded outside it.

## Running

```bash
python main.py
```

Logs stream to `logs/trading.log`. Positions are flattened automatically at
3:55 PM ET.

## Testing

```bash
# Offline suite (synthetic data, no network) — 43 tests
pytest tests/ -m "not integration" -v

# Integration tests (require live Alpaca paper keys in .env)
pytest tests/test_broker_connection.py -m integration -v
```

## Conventions

- No `print()` — `logging` only.
- All config comes from `config.yaml` via the injected `Config`; no module
  reads env vars except `core/config.py`.
- Risk gate is a pure function: it reads portfolio state, never mutates it.
- LLM output is always validated and cast before reaching order logic.
