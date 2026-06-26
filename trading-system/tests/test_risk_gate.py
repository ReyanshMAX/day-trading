"""Unit tests for risk/gate.py.

All offline. Tests every rejection scenario explicitly.
"""

import pytest
from core.portfolio import Portfolio, Position
from core.config import (
    Config, UniverseConfig, AccountConfig, RiskConfig, SignalConfig, RegimeConfig, RRProfile,
    LlmConfig,
)
from risk.gate import check, GateResult


def make_config(nav: float = 100_000.0) -> Config:
    return Config(
        universe=UniverseConfig(tickers=["NVDA", "AAPL", "MSFT", "TSLA", "META"]),
        account=AccountConfig(paper=True, nav=nav),
        risk=RiskConfig(
            max_trade_risk_pct=0.01,
            max_portfolio_heat_pct=0.06,
            max_position_pct=0.10,
            max_sector_positions=4,
            daily_loss_limit_pct=0.03,
            max_position_duration_minutes=90,
        ),
        signal=SignalConfig(
            entry_threshold=0.55, atr_period=14, ema_fast=9, ema_slow=21, rsi_period=14,
            vwap_deviation_bands=[1.0, 2.0, 2.5], orb_window_minutes=15,
        ),
        regime=RegimeConfig(news_poll_interval_seconds=120, min_conviction_to_trade=3),
        llm=LlmConfig(groq_model="llama-3.3-70b-versatile"),
        rr_profiles={
            "trending": RRProfile(1.5, 3.0, {1: 0.25, 2: 0.5, 3: 0.75, 4: 1.0, 5: 1.25}),
            "ranging": RRProfile(1.0, 1.5, {1: 0.0, 2: 0.25, 3: 0.5, 4: 0.75, 5: 1.0}),
        },
        alpaca_api_key="test", alpaca_secret_key="test", groq_api_key="test",
    )


def make_portfolio(nav: float = 100_000.0) -> Portfolio:
    p = Portfolio(nav=nav)
    return p


def _add_position(portfolio: Portfolio, ticker: str, stop_dist: float, qty: int, side: str = "long"):
    """Helper to add a position with known risk."""
    entry = 100.0
    stop = entry - stop_dist if side == "long" else entry + stop_dist
    portfolio.positions[ticker] = Position(
        ticker=ticker, qty=qty, avg_entry=entry, stop=stop, target=110.0, side=side
    )


def test_reject_daily_loss_limit_flag():
    portfolio = make_portfolio()
    portfolio.daily_loss_limit_hit = True
    result = check("NVDA", "long", 10, 1.0, portfolio, make_config())
    assert not result.approved
    assert result.reason == "daily loss limit"


def test_reject_and_set_flag_when_pnl_below_threshold():
    portfolio = make_portfolio()
    portfolio.daily_pnl = -3001.0  # -3.001% of 100k
    assert not portfolio.daily_loss_limit_hit
    result = check("NVDA", "long", 10, 1.0, portfolio, make_config())
    assert not result.approved
    assert result.reason == "daily loss limit"
    # Gate is pure — it must NOT mutate portfolio; instead it signals via set_loss_limit
    assert not portfolio.daily_loss_limit_hit
    assert result.set_loss_limit is True


def test_reject_portfolio_heat():
    portfolio = make_portfolio()
    # open_risk = (entry - stop) * qty = 2 * 3050 = 6100 = 6.1% of 100k
    _add_position(portfolio, "NVDA", stop_dist=2.0, qty=3050)
    result = check("AAPL", "long", 1, 1.0, portfolio, make_config())
    assert not result.approved
    assert result.reason == "portfolio heat"


def test_reject_trade_risk_too_high():
    portfolio = make_portfolio()
    # trade risk = 100 * 15 = 1500 = 1.5% of 100k > 1%
    result = check("NVDA", "long", 100, 15.0, portfolio, make_config())
    assert not result.approved
    assert result.reason == "trade risk"


def test_reject_sector_concentration():
    portfolio = make_portfolio()
    # 4 tech positions already
    for ticker in ["NVDA", "AMD", "AAPL", "MSFT"]:
        _add_position(portfolio, ticker, stop_dist=1.0, qty=1)
    result = check("GOOGL", "long", 1, 1.0, portfolio, make_config())
    assert not result.approved
    assert result.reason == "sector concentration"


def test_approve_all_clear():
    portfolio = make_portfolio()
    result = check("NVDA", "long", 5, 1.0, portfolio, make_config())
    assert result.approved
    assert result.reason is None


def test_reject_non_positive_nav():
    portfolio = make_portfolio(nav=-1000.0)
    result = check("NVDA", "long", 10, 1.0, portfolio, make_config())
    assert not result.approved
    assert "NAV" in result.reason


def test_gate_does_not_mutate_portfolio_on_rejection():
    portfolio = make_portfolio()
    portfolio.daily_loss_limit_hit = True
    positions_before = dict(portfolio.positions)
    pnl_before = portfolio.daily_pnl
    check("NVDA", "long", 10, 1.0, portfolio, make_config())
    assert portfolio.positions == positions_before
    assert portfolio.daily_pnl == pnl_before


def test_reject_short_stop_too_wide():
    portfolio = make_portfolio()
    # atr=2.0, stop_distance=5.0 → 5.0 > 2 * 2.0 → reject
    result = check("NVDA", "short", 10, 5.0, portfolio, make_config(), atr=2.0)
    assert not result.approved
    assert result.reason == "short stop too wide"


def test_approve_short_stop_within_limit():
    portfolio = make_portfolio()
    # atr=2.0, stop_distance=3.9 → 3.9 < 2 * 2.0 = 4.0 → approve
    result = check("NVDA", "short", 10, 3.9, portfolio, make_config(), atr=2.0)
    assert result.approved


def test_short_stop_guard_skipped_when_atr_zero():
    portfolio = make_portfolio()
    # atr=0 → guard disabled regardless of stop_distance
    result = check("NVDA", "short", 10, 999.0, portfolio, make_config(), atr=0.0)
    # Only blocked by trade risk (999*10=9990 > 1% of 100k), not by stop guard
    assert not result.approved
    assert result.reason == "trade risk"


def test_short_stop_guard_not_applied_to_long():
    portfolio = make_portfolio()
    # Long trade with very wide stop — should not trigger the short guard
    # qty=1, stop_distance=0.5 → trade_risk=0.5 < 1% of 100k → approved
    result = check("NVDA", "long", 1, 0.5, portfolio, make_config(), atr=0.1)
    assert result.approved
