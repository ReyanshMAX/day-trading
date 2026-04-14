# Agentic Day Trading System — Critical Audit

Generated: 2026-04-13  
Scope: every `.py` file in `trading-system/`, cross-referenced against `TRADING_SYSTEM_SPEC.md` and `CLAUDE.md`.

---

## Summary of severity categories used below

- **CRITICAL** — will cause incorrect orders, silent money loss, or unhandled crashes in live trading
- **HIGH** — material logic error, missing guard, or spec violation that will surface in normal operation
- **MEDIUM** — incorrect under specific but realistic conditions, or degrades system quality visibly
- **LOW** — code smell, brittleness, minor spec deviation, or missing defensive check

---

## `core/config.py`

### [HIGH] `max_position_pct` not in `TRADING_SYSTEM_SPEC.md`

`RiskConfig` includes `max_position_pct: float` (line 35) and `load_config()` reads `raw["risk"]["max_position_pct"]` (line 129). The spec's `config.yaml` block has no such field. It was added post-spec in `config.yaml` (`max_position_pct: 0.10`). The spec tests (`test_risk_gate.py`) pass this field manually, so the tests pass, but the field is entirely absent from the spec contract. If someone deploys from the spec's example `config.yaml` (which lacks this key), `load_config()` crashes with `KeyError`.

### [MEDIUM] Validation is `assert`-based — killed by `-O` flag

Lines 97–105 use bare `assert` for all validation (`assert alpaca_api_key`, `assert nav > 0`, `assert len(tickers) > 0`). Python's `-O` (optimize) flag strips all asserts. The correct pattern is explicit `if` + `raise ValueError(...)`. Not an issue in dev, but it is a spec ground rule violation (`CLAUDE.md` says validation must happen).

### [MEDIUM] No validation of `rr_profiles` keys

`load_config()` parses whatever `rr_profiles` keys exist in YAML without asserting that "trending" and "ranging" are both present. If one is missing, downstream `order_manager.build_bracket()` falls back to "ranging" (with a log warning), which silently changes sizing for trending signals. No error is raised at startup.

### [LOW] `Config.nav` is static — never updated from live account

`Config.account.nav` is set to the YAML value (100000) at startup and never updated. `Portfolio.nav` is initialized from this value and also never updated. The live paper account NAV changes continuously as P&L accumulates. Risk checks based on `portfolio.nav` will drift meaningfully over a session. The spec says to call `broker.get_account()` to verify paper NAV; that value is logged but never fed back into `portfolio.nav`.

### [LOW] No validation that `entry_threshold` is in (0, 1)

`entry_threshold` of 0.25 (the current config.yaml value) is much lower than the spec-specified 0.55. No validation catches this. The system will fire orders far more aggressively than intended with no warning.

---

## `core/broker.py`

### [CRITICAL] `submit_bracket_order` and `submit_market_order` are `async` but call synchronous Alpaca methods

`submit_bracket_order` (line 90) and `submit_market_order` (line 192) are declared `async` but call `self._trading.submit_order(request)` (lines 117, 202), which is a synchronous blocking call from `alpaca-py`'s `TradingClient`. Awaiting an `async` function that blocks the event loop will freeze all async tasks — the news watcher, the stream, and all other tickers — for the duration of the HTTP round trip (typically 100–500ms). Under a tick storm this compounds. The correct fix is to run the blocking call in a thread executor: `await asyncio.get_event_loop().run_in_executor(None, self._trading.submit_order, request)`.

### [CRITICAL] `_submit_crypto_orders` is not `async` but is called with `return` (not `await`) from an `async` method

`_submit_crypto_orders` (line 124) is a regular synchronous method. `submit_bracket_order` returns it directly on line 105: `return self._submit_crypto_orders(...)`. Since `submit_bracket_order` is `async`, this means calling `await broker.submit_bracket_order(...)` for a crypto ticker will block the event loop for the duration of up to three sequential synchronous HTTP calls (entry order, stop-loss order). This is doubly wrong — same issue as above, but worse because it makes three blocking calls.

### [HIGH] `exit_qty` for crypto uses `filled_qty` from a market order that almost certainly has not filled yet

Lines 157–159: the code reads `entry_order.filled_qty` to compute `exit_qty`. Alpaca paper market orders for crypto return immediately without a fill confirmation. The conditional `if filled_float > 0` will almost always be `False` for an instant paper fill response, falling back to the fee-haircut estimate. The comment acknowledges this but the logic is brittle. More importantly, `exit_qty` is then used in a stop-limit order. If the actual filled amount differs from the estimate by more than the rounding applied, the stop may cover more quantity than the position, which Alpaca will reject.

### [HIGH] Crypto stop-limit order failure is swallowed — position has no hard stop

Lines 170–182: the stop-loss order submission for crypto is wrapped in a bare `try/except Exception` that logs the error and continues. If the stop-limit fails (Alpaca rejects it, network error, etc.), the position exists in the portfolio with no stop protection. The executor's soft take-profit check runs but only on target hits, not stop triggers. The position is naked on the downside until EOD. This should escalate: either retry, or close the position immediately, or at minimum set an `alert` log that creates a monitoring obligation.

### [HIGH] `get_bars()` `multiplier=3` is insufficient for equities at all times

Line 219: `start = end - timedelta(minutes=limit * 3)` to cover `limit` 1-minute bars for equities. With market gaps (weekends, overnight), a 3x window will span at most ~3 hours of wall-clock time. If `limit=100` and the market opened 2 hours ago, only ~120 minutes of real trading data is requested. The Alpaca IEX feed only returns bars during market hours, so the window must be wide enough to include the most recent full session. The current multiplier is not sufficient for Monday morning backfills (needs to span the weekend gap). Should use calendar-aware logic or a larger multiplier (7–10x).

### [MEDIUM] `_is_crypto()` heuristic is incorrect for some tickers

`_is_crypto()` (line 32): `ticker.endswith("USD") and len(ticker) > 4` would match "MSTR" — but "MSTR" is 4 chars so that case is safe. However, a ticker like "BTCUSD" (6 chars, ends in USD) would be classified as crypto, but Alpaca Crypto uses "BTC/USD" format. Conversely, "AMZUSD" would wrongly be treated as crypto. The heuristic is only safe for the exact symbols in the current config; it breaks the moment any similar-looking equity is added.

### [MEDIUM] `is_tradable()` uses module-level `_asset_cache` dict

`_asset_cache` (line 28) is module-level, shared across all `AlpacaBroker` instances. In tests, this causes cache poisoning between test runs. In production, it means a broker instance created at midnight caches results from a halted market that may resume. The cache is never invalidated during a session, which the spec explicitly wants for performance, but the data can become stale (a halt-resumption during the session would not be detected).

### [MEDIUM] `submit_market_order` return type is `None` but returns `order`

The function signature says `-> None` (line 192) but returns `order` (line 203). The caller in `executor.py` calls `self._portfolio.record_close(close_order)` with the returned order, so the return value is required. The wrong type annotation is misleading and will cause confusion.

### [LOW] `get_bars()` for stocks uses `DataFeed.IEX` — IEX feed has ~15-minute delay for non-subscribers

Historical bar fetch on line 236 uses `DataFeed.IEX`. The IEX feed is free but has a 15-minute delay for real-time data. For backfilling bars at market open, this means the most recent 15 minutes of data will be absent from the backfill. The system should use `DataFeed.SIP` (which requires paid Alpaca subscription) or simply document this limitation clearly. Currently there is no log warning.

### [LOW] `StopLossRequest` uses `stop_price` for equities, but Alpaca bracket orders use `stop_price` as a stop-market trigger

Line 114: `StopLossRequest(stop_price=round(stop_price, 2))`. For bracket orders, this submits a stop-market (not stop-limit) leg. On a gap-down open or a fast market, the stop may fill significantly below the stop price. This is the standard behavior for stop-market orders and may be acceptable, but the spec never explicitly chose stop-market over stop-limit for equities. The risk is uncontrolled slippage on stops during volatile conditions.

---

## `core/stream.py`

### [HIGH] `await stream._run_forever()` calls a private method

Line 53: `await stream._run_forever()`. This calls a private (name-mangled by convention) internal method of `alpaca-py`'s `StockDataStream`. The spec explicitly shows `await stream.run()`. Using `_run_forever` is an implementation detail that is not guaranteed to exist across alpaca-py versions. When alpaca-py is upgraded, this will silently break with an `AttributeError`.

### [MEDIUM] Retry counter is correctly reset to 0 inside the `while` loop — but this means a stream that repeatedly connects and immediately drops will retry forever

Lines 45–66: `retries = 0` is set at the top of the while body. If the stream connects successfully but then disconnects 1 second later, `retries` resets to 0 and the loop continues indefinitely. This is actually the intended behavior (to reconnect after transient drops), but it means the `max_retries=5` guard is only effective for consecutive failures before the first successful connection. After any successful connection, the retry budget fully resets. A stream that drops 50 times across a session will reconnect each time. This may be desirable, but the variable name `_MAX_RETRIES` implies a hard cap that does not actually function as one.

### [LOW] `stream.stop_ws()` is called in the except block but `stream` may not have an authenticated connection

Lines 61–64: if `stream = StockDataStream(...)` succeeds but `stream.subscribe_trades(...)` fails, `stop_ws()` is called on an unconnected stream object, which may raise or no-op silently. The outer `try/except` catches this, but it produces noisy misleading log output.

---

## `core/portfolio.py`

### [HIGH] `record_fill()` sets `qty` from `order.qty` (string from Alpaca), not `order.filled_qty`

Line 79: `qty = float(order.qty)`. Alpaca's `Order` object has both `qty` (requested) and `filled_qty` (actually filled). For a market order that partially fills, `order.qty` is the requested quantity, not the filled quantity. Using requested quantity overestimates the position size, which inflates `open_risk()` and distorts subsequent heat checks. Should use `float(order.filled_qty or order.qty)`.

### [HIGH] `record_close()` uses `order.filled_avg_price or 0.0` — a fill price of 0 silently produces wrong P&L

Line 98: `exit_price = float(order.filled_avg_price or 0.0)`. If `filled_avg_price` is `None` or `0`, P&L is computed as `(0 - entry) * qty`, which adds a massive negative PnL to `daily_pnl`. This could falsely trigger the daily loss limit on the next trade. Should at minimum log an error and skip the PnL update when the fill price is unavailable.

### [MEDIUM] `Portfolio.nav` is never updated after fills close positions

As positions close and P&L accumulates, `portfolio.nav` remains at the initial value from config. `open_risk_pct()` and `daily_pnl_pct()` are both denominated against a stale NAV. In a good session, this understates risk. In a bad session, it understates losses (makes the -3% limit harder to trigger). The NAV should be updated after each close: `self.nav += pnl`.

### [MEDIUM] No thread-safety on `positions` dict

`portfolio.positions` is a plain `dict`. In an `asyncio` single-threaded context this is fine within a single event loop, but `record_fill` and `record_close` are called from executor (the tick hot path) while `open_risk()` and `sector_count()` may be read from the gate (also in the event loop). Because Python's asyncio is cooperative and these functions are not `async`, there are no actual data races in the current architecture. However, if any background thread ever touches the portfolio (e.g., a future WebSocket fill-event handler), this will become a race condition with no protection.

### [LOW] `SECTOR_MAP` in `portfolio.py` includes `BTC/USD`, `ETH/USD`, `SOL/USD` (crypto tickers added post-spec)

The spec's `SECTOR_MAP` does not include crypto tickers. These were added, which is correct behavior, but the "crypto" sector uses the same `max_sector_positions=5` (from config.yaml) limit as equities. BTC/USD, ETH/USD, and SOL/USD will all count as "crypto" sector, so at most 5 crypto positions are allowed. This may or may not be intentional, but is unspecified and untested.

### [LOW] `record_fill` logs `qty=%d` format but `qty` is `float` (for crypto)

Line 87: `log.info("Position opened: %s %s qty=%d entry=%.2f", ...)` uses `%d` for `qty`, which is a `float` for crypto fractional units. For a BTC position of `0.0137`, this logs `0`, hiding the actual quantity.

---

## `core/order_manager.py`

### [CRITICAL] `size_multiplier_by_conviction` for "ranging" regime, conviction=1 is `0.0`

Line 76: `size_mult = profile.size_multiplier_by_conviction.get(conviction, 0.5)`. For "ranging" regime with conviction=1, the spec sets multiplier to `0.0`. `qty = max(1, int(base_qty * 0.0))` = `max(1, 0)` = `1`. The spec says conviction=1 in ranging should result in no trade (multiplier=0.0 means size=0, meaning skip). But `max(1, ...)` ensures at least 1 share is always ordered. The code violates the config's intent: a conviction=1 ranging trade should not be placed at all. The gate's `min_conviction_to_trade=3` prevents conv=1 from reaching here in normal flow, but if someone lowers that config value, this silently fires a 1-share ranging trade at minimum conviction.

### [HIGH] `build_bracket()` does not pass `nav` explicitly — uses `self._config.account.nav`

Line 107: `max_notional = self._config.account.nav * self._config.risk.max_position_pct`. As noted above, `config.account.nav` is the YAML static value (100000), not the live NAV. If the account grows to $200k, notional caps are still computed against $100k.

### [HIGH] Fibonacci snapping can silently widen the stop

Lines 94–103: after snapping `raw_stop` to a fib level, `actual_stop_dist = abs(entry - raw_stop)` is recomputed (line 106). If the snapped fib level is further from entry than the ATR-based stop (the guard `snapped_stop < entry` only checks direction, not distance), `actual_stop_dist` grows. The subsequent `compute_base_size()` call uses this larger distance, which reduces quantity. This is correct math but means the stop is now wider than the ATR model intended — the snap is supposed to help with support/resistance alignment, but it can silently increase risk per share without any log of the change in stop distance.

### [MEDIUM] `_is_crypto()` duplicated across three files

`_is_crypto()` is defined identically in `broker.py` (line 31), `order_manager.py` (line 14), and `stream.py` (line 24), and also inlined in `executor.py` (line 52) and `main.py` (line 26). Any change to the crypto detection logic requires updating five separate locations. Should be extracted to a shared utility.

### [MEDIUM] `build_bracket()` raises `ValueError` for notional cap violation

Line 130–133: if a single unit of the asset exceeds `max_notional`, a `ValueError` is raised. In `executor.on_tick()`, the entire tick handler is wrapped in `try/except Exception`, so this ValueError is caught, logged, and silently swallowed. The stock is then skipped. This is arguably the correct behavior, but it means a misconfigured notional cap (e.g., if BRK.A were in the universe) produces a stream of logged errors on every tick rather than a one-time startup warning.

### [LOW] Conviction fallback default of `0.5` in `size_multiplier_by_conviction.get(conviction, 0.5)`

If a conviction value outside 1–5 somehow reaches `build_bracket()` (e.g., the LLM returns 6 and validation misses it), `0.5` is used silently. The correct behavior should be to raise or fallback to 0.

---

## `signals/bar_store.py`

### [HIGH] Minute boundary detection uses `timestamp.minute` — wrong across hour boundaries in edge cases

Line 46: `if timestamp.minute != cur["timestamp"].minute or timestamp.hour != cur["timestamp"].hour`. The hour check prevents a false minute-match across hours (e.g., 09:01 vs 10:01), but if the bar store receives a tick with a timestamp from a different **day** but the same hour and minute, the bar would not be closed. This is a degenerate case (bars should be sequential), but it means a stale `_current` bar from yesterday would be incorrectly updated by today's first same-minute tick before being closed.

### [HIGH] `backfill()` does not clear existing bars before loading

`backfill()` (line 76) appends to the existing deque without checking if it's empty. If `backfill()` is called twice for the same ticker (e.g., on reconnect), bars are duplicated in the deque. The deque's `maxlen=200` will eventually evict old bars, but during the overlap period, indicators computed over the duplicated bars will be incorrect.

### [MEDIUM] `get_bars()` returns bars without sorting by timestamp

Line 64–73: bars are returned in deque insertion order. `backfill()` preserves the broker DataFrame order (which should be ascending). But if `update()` closes bars and appends them to a backfilled deque, the deque maintains insertion order correctly. There is no explicit sort, so if `backfill()` is called with an unsorted DataFrame, indicators downstream will silently use wrong bar sequences. The `df.iterrows()` call in `backfill()` preserves the DataFrame index order; if `broker.get_bars()` returns a non-chronological index (which it shouldn't, but has no explicit sort guarantee after `df.tail(limit)`), the bar store will be corrupted.

### [LOW] Bar timestamps stored as-is from `update()` — timezone handling is implicit

The `timestamp` passed to `update()` comes from the WebSocket handler (Alpaca: `data.timestamp`, which is UTC-aware). It is stored directly in the bar dict and later used by `orb()` (which converts to ET). If any tick source passes a naive datetime, `astimezone()` in `orb()` will raise. The bar store has no assertion that timestamps are timezone-aware.

---

## `signals/indicators.py`

### [HIGH] `orb()` uses `len(orb_bars) < window_minutes` — counts bars, not wall-clock minutes

Line 133: `if len(orb_bars) < window_minutes`. The function counts how many 1-min bars fall in the 9:30–9:45 ET window. If the market has data gaps (no prints for 2 minutes due to illiquidity), `len(orb_bars)` may be 13 when 15 minutes have elapsed. The function will return `(None, None)` even though the ORB window has fully closed. This silently suppresses ORB signal contribution for illiquid tickers like ARKK and SOFI throughout the session.

### [HIGH] `orb()` uses row-by-row `df.iterrows()` — extremely slow on 100+ bars

Line 125: `for ts, row in df.iterrows()`. For a 100-bar DataFrame called on every tick for 15 tickers, this is up to 1500 row iterations per tick. `iterrows()` is the slowest DataFrame iteration method in pandas. Should use boolean indexing: `session_bars = df[(df.index.date == last_date) & ...]`.

### [HIGH] `macd()` column ordering assumption is brittle

Lines 81–82: `cols = result.columns.tolist()` then accesses by position `[cols[0]]`, `[cols[1]]`, `[cols[2]]`. `pandas_ta.macd()` returns columns named `MACD_12_26_9`, `MACDh_12_26_9`, `MACDs_12_26_9` in a defined order (MACD line, histogram, signal). The code assumes index 0=MACD, 1=signal, 2=histogram, but the actual order is MACD, histogram, signal. The returned `(macd_line, signal_line, histogram)` tuple is wrong — `signal_line` will actually contain the histogram, and `histogram` will contain the signal. In `scoring.py`, the check `macd_line > macd_signal` will be comparing the MACD line against the histogram, not the signal line. This is a live scoring bug.

### [MEDIUM] `vwap()` forward-fills across sessions

Line 30: `vwap_series.reindex(df.index, method="ffill")`. If the DataFrame spans multiple sessions (likely after backfill with 100 bars), the last VWAP of the previous session is forward-filled into the next session's bars before any bar of the new session is processed. The first bar of each new session will use the prior session's final VWAP rather than NaN or the new session's first bar's typical price. This causes the first few bars of each day to produce stale VWAP values, potentially triggering or suppressing signals incorrectly at the open.

### [MEDIUM] `vwap_bands()` std is derived from cumulative variance, not rolling std — as the session progresses, early outliers have decreasing weight

The formula `variance = ((typical - vw_mean)**2 * volume).cumsum() / cum_vol` computes a weighted cumulative variance. This is mathematically sound for a session-total weighted std but means the first bar of a session always has std=0 (a single data point), making `vwap + 0*std = vwap`. The bands are meaningless for the first bar and grow slowly. The spec describes "rolling std of (typical_price - vwap) weighted by volume" which differs from what is implemented (cumulative vs rolling). The consequence is that early-session ranging signals via VWAP bands will never fire.

### [MEDIUM] `atr()` and `rsi()` return `float("nan")` on insufficient data, not `0.0` or `None`

Lines 63, 72: both functions return `float("nan")` when pandas-ta returns no result. `nan` then flows into `IndicatorSnapshot` fields. In `scoring.py`, comparisons like `40 < indicators.rsi < 70` with NaN always return `False` (NaN comparison is always False in Python). This means RSI signal component silently drops to 0 when data is insufficient. This is actually safe, but it is not documented and the NaN values will appear in log output as confusing `nan` strings. The score logging in `engine.py` (line 145) will log `rsi=nan`.

### [MEDIUM] `rvol()` approximation ignores time-of-day volume patterns

Line 93–104: `rvol` is computed as current bar volume vs mean of last 20 bars. This ignores that volume at 9:30 AM is structurally higher than at 11 AM, so a 9:30 bar will always show `rvol >> 1.5` even for normal open activity. The spec says "current bar volume / avg volume at this time of day" but the implementation uses last 20 bars of any time of day. This will generate false volume confirmation signals at open for every ticker.

### [LOW] `fibonacci_levels()` does not validate `swing_high > swing_low`

If `swing_high <= swing_low` (which can happen if `detect_swing_high` and `detect_swing_low` return the same value for a flat bar sequence), `r = 0` and all fibonacci levels collapse to `swing_high`. The resulting levels are meaningless but no error or warning is raised. In `executor.py`, the guard `if sh > sl` (line 97) catches this before calling `fibonacci_levels`, but `fibonacci_levels` itself has no guard.

### [LOW] `detect_swing_high()` returns `max(high)`, not a structural swing high

The function is named `detect_swing_high` implying a local maximum with lower highs on both sides (the standard TA definition). The implementation returns the simple maximum of the `high` column over `lookback` bars. This is semantically different — a swing high is a pivot point, not just the max. The mislabeling will cause confusion during any future work that expects true swing-high detection (e.g., for Elliott wave analysis or more sophisticated Fibonacci anchoring).

---

## `signals/scoring.py`

### [HIGH] Trending score renormalization is incorrect — weights don't add to 1 when ORB is excluded

Lines 61–77: the ORB weight is 0.15. When ORB is excluded, `total_weight = 0.85`. The other weights (0.25 + 0.20 + 0.15 + 0.10 + 0.15 = 0.85) are appended **after** the ORB check. So `raw_score` is the sum of the 5 non-ORB weights that fired. `raw_score / total_weight` divides by 0.85. This is correct proportionally **only if** no other components also have weights that sum differently. The math happens to work (dividing by the sum of remaining weights), but the implementation is fragile: if any other weight changes, the hardcoded `total_weight = 0.85` will be wrong without the developer realizing it. The correct pattern is to compute `total_weight` as the sum of weights actually included.

### [HIGH] Ranging score is always positive (long bias) regardless of `regime.direction == "bearish"`

`_score_ranging()` (line 86): the score is accumulated as positive values (0 to 1.0), then on line 121 `if regime.direction == "bearish": score = -score`. But all the conditions in ranging mode (price below VWAP, RSI < 35, near ORB support) specifically describe oversold / mean-reversion-long setups. A bearish ranging regime would call for the opposite (price above VWAP, RSI > 65, near ORB resistance). The code blindly negates the long score to get a short score without checking short-bias conditions. A bearish ranging regime will produce a negative score derived from long-bias indicators, which makes no logical sense.

### [MEDIUM] `IndicatorSnapshot.vwap_std` defaults to `0.0`

Line 26: `vwap_std: float = 0.0`. In `_score_ranging()`, the VWAP band check gates on `if indicators.vwap_std > 0` (line 93). If `vwap_std` is never populated (e.g., if the engine fails to extract it), the largest component (weight 0.35) is always excluded. Since `total_w += 0.35` unconditionally, the maximum achievable score in ranging mode is `(0.25 + 0.20 + 0.20) / 1.00 = 0.65`. The threshold is 0.55 (in config.yaml it's actually 0.25, but tests use 0.55). This is a silent reduction in the effective score range that is not documented.

### [LOW] `compute_score()` returns `float | None` but `None` is only for `regime="avoid"` — the engine already filters avoid before calling `compute_score`

In `engine.py` line 51: `if regime_state.regime == "avoid": return None`. The engine already short-circuits before calling `compute_score()`. So `compute_score()` will never actually receive an avoid regime in practice. The `None` return for avoid in `compute_score()` is unreachable code in the live system. Tests test it through `compute_score()` directly, which is fine, but this creates a confusing double-check.

---

## `signals/engine.py`

### [HIGH] `log_scores_loop()` calls `asyncio.create_task()` inside a loop without `await` on the task

Lines 121–123: inside `log_scores_loop`, after staggering with `await asyncio.sleep(stagger * i)`, `asyncio.create_task(self._ticker_log_loop(ticker, interval))` is called but the task handle is not stored. If the task raises an unhandled exception, it becomes a "never-retrieved" exception that is silently swallowed by Python's event loop (logged to stderr only, not to the trading log). This is a fire-and-forget task creation that violates the spec's "no unhandled exceptions" requirement.

### [MEDIUM] `on_tick()` calls `_compute()` which gets 100 bars every tick — no caching

`_compute()` (line 41) calls `self._bar_store.get_bars(ticker, 100)` which constructs a new `pd.DataFrame` from the deque on every call. `on_tick()` is called for every trade tick which, for a liquid stock like NVDA, can be hundreds per second. Constructing a 100-row DataFrame and computing 8+ indicators on every tick is prohibitively expensive. The signal doesn't change meaningfully between ticks within the same minute. There should be a per-minute throttle: only recompute indicators when a new bar has closed.

### [LOW] `vwap_std` is derived from band key `"-{cfg.vwap_deviation_bands[0]}"` — assumes lower band has smaller key index

Lines 61–62: `std_key = f"-{cfg.vwap_deviation_bands[0]}"`. If `vwap_deviation_bands = [1.0, 2.0, 2.5]`, this gives `"-1.0"`. The engine extracts `vwap_std` as `abs(vwap_val - lower_band_1.0)`, which is `1.0 * std`. This works only for bands with deviation=1.0. If the first band were 2.0, `vwap_std` would be 2x the actual std. The derivation is indirect and brittle; std should be extracted from `vwap_bands()` directly.

---

## `regime/classifier.py`

### [MEDIUM] `store_classification()` is called after every successful LLM call — including during morning sweep

Line 101: after every successful classification, the result is stored in ChromaDB. The morning sweep calls classify for all 15 tickers. If any ticker's ChromaDB write fails (ChromaDB is a local persistent store that can become locked or corrupt), the exception propagates out of the inner `try/except` block (it's not inside the try). Actually — looking at the code, `store_classification` is called on line 101, which is inside the `try` block that only catches `json.JSONDecodeError`, `KeyError`, `AssertionError`. The outer `except Exception` on line 107 would catch a ChromaDB write failure, returning the fallback regime instead of the successfully-classified one. So a ChromaDB write failure during an otherwise successful classification returns a fallback regime, silently discarding the correct result.

### [MEDIUM] LLM model is hardcoded as `"llama-3.1-8b-instant"` — not in config

Line 76: `model="llama-3.1-8b-instant"`. The model is not in `config.yaml` or `Config`. This violates the CLAUDE.md ground rule: "Never hardcode values that exist in `config.yaml`. Always read from config." The model should be configurable, especially since Groq's model availability changes.

### [LOW] `_USER_TEMPLATE` includes `{few_shot}` as a raw block — no separator between few-shot and the JSON instruction

The few-shot block ends with a `\n` and the JSON output instruction follows. If the few-shot block is non-empty, the LLM may treat the prior examples as part of the current instruction, potentially confusing the model about what format is requested. A clear separator (e.g., "---") or restructured prompt would reduce this risk.

### [LOW] `prior_regime` parameter to `classify()` is unused in the `fallback_regime()` call

The spec says: "if parsing fails, log error and return prior regime or default". The function signature accepts `prior_regime: str | None` but `fallback_regime()` ignores it entirely, always returning `{regime: "ranging", conviction: 2, direction: "neutral"}`. If the prior regime was "trending" with conviction 4, a JSON parse error causes a sudden drop to ranging/conviction=2, which may suppress all trading for that ticker until the next successful classification.

---

## `regime/news_watcher.py`

### [HIGH] `watch()` processes tickers sequentially with `for ticker in self._active_tickers()`

Line 103–104: `for ticker in ... await self._process_ticker(ticker)`. Each `_process_ticker` call awaits `self._classifier.classify()`, which in turn awaits the Groq API (100–500ms). With 15 tickers, a single pass takes 15 × ~300ms ≈ 4.5 seconds minimum. The spec explicitly says: "Do not call them sequentially — all 15 should fire in parallel." `run_morning_sweep()` does use `asyncio.gather()` correctly, but `watch()` does not. The continuous watch loop is sequential, meaning the effective refresh interval is `news_poll_interval_seconds + (N_tickers × groq_latency)`.

### [MEDIUM] `_fetch_headlines()` is synchronous but called inside an async context

Line 38: `_fetch_headlines()` calls `self._news_client.get_news(request)`, which is a synchronous HTTP call from the `alpaca-py` `NewsClient`. It is called from `_process_ticker()` (async), which is called from `watch()` (async) without `run_in_executor`. This blocks the event loop for each HTTP call.

### [LOW] No backoff on failed news fetches

Line 44: `log.warning("Failed to fetch news for %s: %s", ...)` and returns `[]`. A rate-limited or temporarily failing Alpaca News API will silently suppress headline fetches and never retry until the next `news_poll_interval_seconds` cycle. No exponential backoff, no rate-limit awareness.

### [LOW] `_active_tickers()` filters by `SECTOR_MAP.get(t) == "crypto"` — crypto tickers not in SECTOR_MAP return "other"

Line 63: tickers with no sector mapping return `None` from `SECTOR_MAP.get(t)`, which is not equal to "crypto", so they are excluded after hours. This works for the current universe (all crypto tickers are in the map), but is silently broken for any crypto ticker not in `SECTOR_MAP`.

---

## `regime/regime_store.py`

### [LOW] No thread-safety on `_store` dict

Same concern as Portfolio. The `RegimeStore._store` dict is accessed from both the news watcher loop (writes via `set()`) and the signal engine (reads via `get()`) in the same event loop. As long as asyncio's cooperative scheduling is not violated, this is safe. But it is undocumented and fragile.

### [LOW] `get()` returns `None` for unknown tickers — but the engine falls back to a default regime

In `engine._compute()` line 48: `if regime_state is None: regime_state = RegimeState(regime="ranging", ...)`. This hardcoded fallback bypasses `min_conviction_to_trade=3` (since the fallback has conviction=2, which is below the threshold, so the engine returns None). This means tickers that never got classified will never trade. This is acceptable behavior, but it should be documented in the code because it is the implicit gating mechanism for tickers that failed morning sweep classification.

---

## `risk/gate.py`

### [HIGH] Gate mutates `portfolio.daily_loss_limit_hit` — it is not a pure function

Line 42: `portfolio.daily_loss_limit_hit = True`. The spec (CLAUDE.md Phase 4.2) states: "The gate must be a pure function — no state, no side effects, no I/O." The spec test (test_risk_gate.py line 104–111) tests that the gate does not mutate the portfolio on flag-already-set rejections, but does not test the mutation on the P&L threshold rejection path. The gate violates its own contract: it does mutate state. This is arguably intentional (the flag must be set somewhere), but violates the spec's explicit "pure function" requirement and makes the gate harder to test and reason about.

### [MEDIUM] `now = datetime.datetime.now().isoformat()` uses local time, not UTC

Line 33: `now = datetime.datetime.now().isoformat()` (naive datetime). Log timestamps from `gate.py` will be in local time (whatever the server's timezone is), while all other log timestamps from `logging_setup.py` use the `logging` framework's asctime (also local time but controlled by the formatter). On a server running UTC (e.g., a cloud VM), this is fine. On a dev machine in PST, gate log lines say 7:30 AM while the trading action is at 14:30 UTC, creating confusing mixed-timezone logs. Should use `datetime.datetime.now(tz=datetime.timezone.utc).isoformat()`.

### [LOW] `check()` receives `direction` as a parameter but never uses it

Line 23: `direction: str` is accepted but not used in any of the five gate checks. It is logged on the APPROVE path (line 75). The spec includes direction in the check signature, but direction is regime-aware information that the gate is supposed to be unaware of (spec: "risk/gate.py has no import from regime/ — it is regime-unaware"). Having direction available but unused is misleading.

---

## `memory/chroma_store.py`

### [HIGH] `get_similar_contexts()` uses `where={"ticker": ticker} if count > 1 else None`

Line 68: the `where` filter is applied only when `count > 1`. When `count == 1`, the filter is removed, meaning any single-entry collection will return that entry regardless of ticker. If NVDA and AAPL are both in the collection (count=2), the filter is applied. But if only NVDA has one entry (total count=1 after a partial startup), AAPL's few-shot query returns NVDA's data — cross-ticker contamination. The condition should be `None` only when `count == 0`, and the ticker filter should always be applied when data exists.

### [MEDIUM] `store_classification()` uses `date.today()` in UTC context — may be off by one near midnight ET

Line 27: `doc_id = f"{ticker}_{date.today().isoformat()}"`. `date.today()` uses local machine time. If the server is in UTC and it's after midnight UTC but before 5 AM ET (before midnight ET), `date.today()` returns tomorrow's ET date for the document ID. When `update_outcome()` is called the next day, it constructs the same doc ID and may or may not find the record depending on whether the date rolled over. Should use ET timezone explicitly: `datetime.now(tz=ZoneInfo("America/New_York")).date()`.

### [MEDIUM] ChromaDB `PersistentClient` path is hardcoded relative to the file location

Line 14: `_DB_PATH = str(Path(__file__).parent.parent / "chroma_db")`. This anchors the ChromaDB path to the `trading-system/` directory. If the script is run from a different working directory, the relative path may differ. Should be configurable via config or at least an environment variable.

### [LOW] `update_outcome()` overwrites the entire metadata dict but doesn't update `regime`, `direction`, or `catalyst` fields

Lines 43–53: `meta["pnl_pct"] = pnl_pct` and `meta["outcome"] = "profitable"/"unprofitable"` are set. All other fields (regime, direction, conviction, catalyst) are preserved from the original classification. This is correct behavior, but if the regime classification was wrong (e.g., LLM said "trending" but it was a ranging day), the ChromaDB record associates the wrong regime with the outcome. Few-shot retrieval will then reinforce bad classifications.

---

## `execution/executor.py`

### [HIGH] Soft take-profit for crypto does not cancel the pending stop-loss order

Lines 55–73: when price hits the crypto take-profit target, a market close order is submitted. However, the hard stop-loss GTC order that was placed when the position was entered (in `broker._submit_crypto_orders()`) is still live. Alpaca will attempt to execute both the take-profit market order and the existing stop-loss order. The market order will fill first (being a market order), closing the position. The stop-loss order will then attempt to sell a position that no longer exists, resulting in a short sell. On Alpaca paper accounts, this may be permitted and would open an unintended short position. The stop-loss GTC order must be cancelled before or immediately after the take-profit closes.

### [HIGH] `executor.on_tick()` accesses `self._signal_engine._bar_store` directly — breaks encapsulation

Line 92: `df = self._signal_engine._bar_store.get_bars(ticker, 50)`. The executor reaches into the signal engine's private attribute to fetch bars for Fibonacci computation. This creates a hidden dependency: `_bar_store` is not part of `SignalEngine`'s public API. If the signal engine internals change, the executor silently breaks. The Fibonacci computation should be done inside the signal engine or order manager, not the executor.

### [MEDIUM] `risk_gate` parameter in `Executor.__init__` is unused — `gate_check` is imported directly

Line 31: `self._risk_gate = risk_gate` is stored but never used. The gate is called via the imported module function `gate_check` (line 14: `from risk.gate import check as gate_check`) on line 112. In `main.py`, `executor = Executor(broker, portfolio, signal_engine, order_manager, None, config)` passes `None` for `risk_gate`. The `risk_gate` parameter in the constructor is dead code. This is a spec violation (DI principle) and makes the test code set up a mock `risk_gate` that has no effect.

### [MEDIUM] Executor test patches `execution.executor.gate_check` — tests work despite the DI contradiction above

`test_executor.py` patches `"execution.executor.gate_check"` directly (line 95, 124, 135) to control gate behavior. This works but only because the constructor's `risk_gate` argument is ignored. The test creates a mock `risk_gate` object that is never called. The tests pass for the wrong reason: they test behavior by patching the module-level import, not through the injected dependency.

### [LOW] `on_tick()` checks `has_position()` **after** running the full signal engine

Lines 76–81: `signal = self._signal_engine.on_tick(...)` (which updates the bar store, computes all indicators, etc.) before checking `if self._portfolio.has_position(ticker)`. The position check should come first (it's a fast dict lookup) to avoid wasting compute on indicators for tickers where no order is possible.

---

## `core/stream.py`

### [MEDIUM] `stream.py` docstring says "Crypto tickers are routed to Binance" but the code routes to Coinbase

Line 5–6: docstring says "crypto tickers are routed to Binance's public aggTrade stream (see binance_stream.py)". The actual implementation routes to `coinbase_stream.py`. The `binance_stream.py` file does not exist. The docstring is stale/wrong and will confuse future developers.

### [LOW] `asyncio.gather(*tasks)` in `start()` — if one stream permanently fails (Coinbase returns), the gather returns, ending stream for remaining streams

Line 98: `await asyncio.gather(*tasks)`. If `coinbase_stream.start()` exhausts all retries and returns (logs a critical and exits), `asyncio.gather` will complete for that task but continue for the equity stream. The equity stream continues but the overall gather will resolve once all tasks are done. In `main.py`, `stream_task` wraps this entire `start()` call; if `start()` returns (due to Coinbase giving up), `stream_task` completes silently and the main loop (which is waiting on `shutdown.wait()`) continues without any stream. No alert is raised. The system appears to be running but receives no ticks.

---

## `core/coinbase_stream.py`

### [HIGH] `retries = 0` inside the while loop — retry counter always resets

Line 103: `retries = 0` is set at the top of the while loop body, before the `await _connect_and_stream(...)`. This means `retries` is reset to 0 on every iteration, including after a successful connection that later drops. The `while retries < _MAX_RETRIES` condition will always be `True` on the next iteration (since retries was just reset to 0), making the retry counter infinite. The `_MAX_RETRIES=5` guard is completely ineffective. The function will retry forever. This is actually the same logical bug as in `stream.py` (equity stream), and may be intentional for always-reconnecting streams, but the variable name and guard condition are misleading.

### [MEDIUM] "subscriptions" channel `matches` was renamed to `market_trades` in newer Coinbase Exchange API

The `_subscribe_msg()` (line 37) subscribes to `"channels": ["matches"]`. Coinbase Advanced Trade / Exchange API has been evolving; the `matches` channel may be deprecated or renamed on the production WebSocket feed. This needs verification against the current Coinbase Exchange WebSocket documentation. A silent channel change would result in no errors (WebSocket connects, subscription message is accepted) but no `match` events, meaning no crypto ticks.

### [LOW] No validation of required message fields before accessing them

Line 75–78: `msg["product_id"]`, `msg["price"]`, `msg["size"]`, `msg["time"]` are accessed without `.get()` or try/except guards. If Coinbase sends a `match` message with a missing field (API schema change, error message, etc.), a `KeyError` is raised. The outer `except Exception` (line 88) catches it and logs it, but this means a stream of malformed messages produces a stream of error logs without any alert escalation.

---

## `main.py`

### [HIGH] `close_all_positions_eod()` only closes equity positions — crypto positions are skipped with a log

Lines 50–54: crypto positions are explicitly skipped (`if _is_crypto(ticker): continue`). The rationale ("24/7 market") is logged. However, the soft take-profit in the executor only closes crypto on target hits. If BTC/USD is in a losing position at 3:55 PM ET, it will remain open indefinitely with only the hard stop-loss GTC order protecting it. This may be intentional (crypto trades 24/7), but the hard stop-loss order placed at entry may have parameters that were calibrated for intraday volatility, not overnight holds. The risk profile changes dramatically. The system's "no overnight positions" rule from the spec is violated for crypto.

### [HIGH] EOD close uses `asyncio.sleep(wait_seconds)` — if the system starts after 3:55 PM ET, it schedules close for the next calendar day, not the next trading day

Lines 39–43: if the system starts at 4:00 PM ET, `close_time = close_time + timedelta(days=1)` schedules close for 3:55 PM the next calendar day. If tomorrow is Saturday, the system will sleep for 23 hours and 55 minutes and then attempt to close positions on a weekend when the market is closed. The Alpaca API will reject the market order. Should use a trading calendar check.

### [MEDIUM] `asyncio.gather(stream_task, watch_task, eod_task, score_log_task, return_exceptions=True)` after shutdown — tasks are cancelled but their exceptions are not inspected

Line 107: `await asyncio.gather(..., return_exceptions=True)`. Exceptions from cancelled tasks are swallowed. If a task raises something other than `CancelledError` during shutdown, it is silently ignored. Should log all non-`CancelledError` exceptions from the gather result.

### [MEDIUM] `score_log_task` only logs crypto tickers

Line 92–100: `crypto_tickers = [t for t in config.universe.tickers if _is_crypto(t)]` and `asyncio.create_task(signal_engine.log_scores_loop(crypto_tickers))`. The score logging loop only runs for crypto tickers. Equity tickers (NVDA, AAPL, etc.) have no heartbeat score logging. The spec does not restrict this, but it creates a monitoring blind spot for the majority of the universe.

### [LOW] `portfolio.nav` is initialized from `config.account.nav` (YAML), not from the live account

Line 67: `portfolio = Portfolio(nav=config.account.nav)`. The live account's actual NAV (fetched on line 64: `acct = broker.get_account()`) is logged but not used to initialize the portfolio. If the account has drifted from $100k (e.g., due to prior sessions), all risk calculations use the wrong denominator from the start.

---

## Tests — Cross-Cutting Issues

### [HIGH] `test_indicators.py` `make_bars()` starts at 14:30 UTC — `orb()` function tests pass only because 14:30 UTC = 9:30 AM ET

The test relies on the implicit correspondence between UTC 14:30 and ET 9:30 during EST (UTC-5). During EDT (UTC-4, March–November), 14:30 UTC = 10:30 AM ET, which means the first bar is outside the 9:30–9:45 ORB window. The `test_orb_returns_floats_when_enough_bars` test will fail when run during EDT (daylight saving time). This is a DST-sensitive test that will produce false failures for ~8 months of the year on a developer machine set to ET.

### [MEDIUM] `test_risk_gate.py` does not test that gate does not mutate portfolio when P&L threshold triggers the flag

`test_gate_does_not_mutate_portfolio_on_rejection()` (line 104) only tests the case where `daily_loss_limit_hit` is already set. It does not test the mutation path (check 2) where `daily_loss_limit_hit` gets set by the gate. There is a contradiction: the spec says the gate must be pure (no mutation), but the gate mutates the flag, and the test explicitly asserts the flag was set (line 67: `assert portfolio.daily_loss_limit_hit`). The test simultaneously validates the mutation and is claimed as proof the gate is pure. This is internally inconsistent.

### [MEDIUM] `test_executor.py` mock risk gate is set up via constructor but never actually called

As noted in the executor audit: the `risk_gate` mock in `_make_executor()` (line 74–75) is created and passed to the executor, but the executor ignores it and calls the module-level `gate_check` function. The `with patch("execution.executor.gate_check", ...)` patches are doing the real work. This means the `_make_executor(gate_approved=True/False)` parameter has no effect on the actual gate behavior — only the patch controls it. The test infrastructure is misleading.

### [MEDIUM] `test_signal_scoring.py` does not test bearish trending regime or bearish ranging regime

The test suite only covers bullish direction. The bearish path (negation in `_score_trending` and `_score_ranging`) is untested. The ranging-bearish bug (using long-bias conditions to produce a short score) is not caught by any test.

### [LOW] No test for `bar_store.py` as a unit

There is no `test_bar_store.py`. The bar store's `update()` minute-boundary detection, `backfill()` idempotency, and `get_bars()` edge cases (empty deque, partial fill) are tested only implicitly through the crypto smoke test.

### [LOW] `test_broker_connection.py` asserts `len(df) == 5` for `get_bars("AAPL", "1Min", 5)` — this will fail when market is closed

Line 28: `assert len(df) == 5`. The integration test will fail if the test is run outside market hours when Alpaca returns fewer than 5 bars (or an empty DataFrame). The test requires live market data, making it CI-unfriendly even with the `integration` marker.

---

## Spec Deviations (Factual)

| Item | Spec says | Code does |
|---|---|---|
| `entry_threshold` | 0.55 | config.yaml has 0.25 |
| `max_sector_positions` | 4 | config.yaml has 5 |
| Stream reconnect | `await stream.run()` | `await stream._run_forever()` |
| Tickers | 15 stocks | 18 (added BTC/USD, ETH/USD, SOL/USD) |
| `signal/` directory | named `signal/` | renamed to `signals/` (documented in CLAUDE.md) |
| `rvol` definition | volume at this time of day | last 20 bars regardless of time |
| Gate pure function | yes | mutates `portfolio.daily_loss_limit_hit` |
| `macd()` return order | (macd, signal, histogram) | returns (macd, histogram, signal) due to pandas-ta column order |
| LLM fallback uses prior regime | yes | always returns fixed `ranging/2/neutral` |
| Crypto tickers in universe | not in spec | added post-spec |

---

## Summary — Most Critical Items to Fix First

1. **`broker.py` blocking sync calls in async functions** — will lock the entire event loop on every order and bar fetch
2. **`macd()` column order bug** — MACD signal and histogram are swapped, corrupting scoring for every tick
3. **Executor crypto take-profit does not cancel the hard stop-loss** — may open unintended short positions
4. **`news_watcher.watch()` is sequential, not parallel** — single pass takes 5+ seconds instead of ~300ms
5. **`_score_ranging()` bearish direction uses long-bias conditions** — produces nonsensical signals for bearish ranging tickers
6. **ChromaDB `get_similar_contexts()` cross-ticker contamination** — wrong few-shot examples when count=1
7. **`record_fill()` uses `order.qty` not `order.filled_qty`** — overstates position size
8. **`orb()` uses `iterrows()` on every tick** — O(n) row iteration is a major hot-path performance issue
9. **`stream.py` calls `_run_forever()` private method** — will break on next alpaca-py version update
10. **EOD close schedules for next calendar day, not next trading day** — fails on weekends

