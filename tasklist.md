# Orchestration Tasklist

Generated from audit.md on 2026-04-13.

## Step 0 — Audit
- [x] DONE: audit.md written. 40+ issues found across severity levels.

## Step 1 — Correctness Bugs

### 1A: execution/executor.py
- [x] DONE: Fix crypto take-profit not cancelling stop-loss order; fix has_position check order; fix dead risk_gate DI param

### 1B: core/order_manager.py
- [x] DONE: Fix size_multiplier conviction=0 edge, fix nav staleness from config, fix Fibonacci snapping widening stop silently

### 1C: risk/gate.py + core/portfolio.py
- [x] DONE: Fix gate mutating portfolio (pure function violation); fix record_fill using order.qty not filled_qty; fix record_close fill price 0 silent bad P&L; fix nav never updated

### 1D: signals/indicators.py + signals/scoring.py (was signal/)
- [x] DONE: Fix macd() column order bug; fix orb() using iterrows (perf); fix orb() counting bars not wall-clock minutes; fix vwap() ffill across sessions; fix ranging bearish scoring logic

### 1E: regime/classifier.py + regime/news_watcher.py
- [x] DONE: Fix news_watcher.watch() sequential to parallel; fix _fetch_headlines() sync call in async context; fix fallback_regime() ignoring prior_regime; fix ChromaDB write failure silently returning fallback

### 1F: core/stream.py + core/broker.py
- [x] DONE: Fix broker.py blocking sync calls in async; fix _submit_crypto_orders not async; fix stream.py using _run_forever() private method; fix get_bars() multiplier insufficient; fix submit_market_order return type annotation

## Step 2 — Signal Engine Improvements
- [x] DONE: 2A Indicator robustness
- [x] DONE: 2B Score weight calibration
- [x] DONE: 2C ORB hardening
- [x] DONE: 2D VWAP mean reversion filter

## Step 3 — Risk Gate Improvements
- [x] DONE: 3A Gate statelessness and slippage
- [x] DONE: 3B Position duration guard

## Step 4 — Execution Path Improvements
- [x] DONE: 4A Order fill detection and race condition
- [x] DONE: 4B Trailing soft target hardening
- [x] DONE: 4C Async hot path audit

## Step 5 — Regime Classifier Improvements
- [x] DONE: 5A Prompt quality
- [x] DONE: 5B Classification caching

## Step 6 — Memory Improvements
- [x] DONE: 6A Outcome tracking completeness

## Step 7 — Final Cleanup
- [x] DONE: 7A Dead code and config consolidation
- [x] DONE: 7B Final test suite — 75/75 passing, 0 failures

## Known Failures
(none yet)
