"""Config loader — single source of truth for all settings.

Reads config.yaml and .env. Returns a Config dataclass injected into every module.
No other module reads env vars directly.
"""

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)


@dataclass
class UniverseConfig:
    tickers: list[str]


@dataclass
class AccountConfig:
    paper: bool
    nav: float


@dataclass
class RiskConfig:
    max_trade_risk_pct: float
    max_position_pct: float
    max_portfolio_heat_pct: float
    max_sector_positions: int
    daily_loss_limit_pct: float
    max_position_duration_minutes: int
    asset_cache_refresh_seconds: int = 60
    duration_check_interval_seconds: int = 300


@dataclass
class SignalConfig:
    entry_threshold: float
    atr_period: int
    ema_fast: int
    ema_slow: int
    rsi_period: int
    vwap_deviation_bands: list[float]
    orb_window_minutes: int
    min_bars: int = 30
    confidence_threshold: float = 0.6
    atr_spike_multiplier: float = 3.0
    rvol_trend_min: float = 1.5
    rvol_ranging_min: float = 1.3
    no_trade_windows: list = field(default_factory=list)


@dataclass
class RegimeConfig:
    news_poll_interval_seconds: int
    min_conviction_to_trade: int


@dataclass
class LlmConfig:
    groq_model: str
    cache_ttl_minutes: int = 10
    stale_regime_minutes: int = 120


@dataclass
class ExecutionConfig:
    order_retry_sleep_seconds: float = 0.5
    latency_warn_seconds: float = 0.1
    min_trail_increment_atr_fraction: float = 0.1


@dataclass
class MemoryConfig:
    min_outcomes_for_summary: int = 5


@dataclass
class RRProfile:
    stop_atr_mult: float
    target_atr_mult: float
    size_multiplier_by_conviction: dict[int, float]


@dataclass
class ForexConfig:
    pairs: list[str]
    risk_pct: float = 0.005
    adx_trend_threshold: float = 25.0
    size_unit: int = 1000


@dataclass
class Config:
    universe: UniverseConfig
    account: AccountConfig
    risk: RiskConfig
    signal: SignalConfig
    regime: RegimeConfig
    llm: LlmConfig
    rr_profiles: dict[str, RRProfile]
    execution: "ExecutionConfig" = field(default_factory=ExecutionConfig)
    memory: "MemoryConfig" = field(default_factory=MemoryConfig)
    forex: "ForexConfig | None" = None

    # Env-loaded secrets
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_url: str = "https://data.alpaca.markets"
    groq_api_key: str = ""
    finnhub_api_key: str = ""
    polygon_api_key: str = ""
    oanda_api_key: str = ""
    oanda_account_id: str = ""


def load_config(config_path: str | None = None) -> Config:
    """Load config.yaml and .env, validate required fields, return Config."""
    root = Path(__file__).parent.parent
    env_path = root / ".env"
    load_dotenv(dotenv_path=env_path)

    if config_path is None:
        config_path = str(root / "config.yaml")

    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    alpaca_api_key = os.environ.get("ALPACA_API_KEY", "")
    alpaca_secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    alpaca_base_url = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    alpaca_data_url = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets")
    groq_api_key = os.environ.get("GROQ_API_KEY", "")
    finnhub_api_key = os.environ.get("FINNHUB_API_KEY", "")
    polygon_api_key = os.environ.get("POLYGON_API_KEY", "")
    oanda_api_key = os.environ.get("OANDA_API_KEY", "")
    oanda_account_id = os.environ.get("OANDA_ACCOUNT_ID", "")

    assert alpaca_api_key, "ALPACA_API_KEY must be set in .env"
    assert alpaca_secret_key, "ALPACA_SECRET_KEY must be set in .env"
    assert groq_api_key, "GROQ_API_KEY must be set in .env"

    nav = float(raw["account"]["nav"])
    assert nav > 0, "account.nav must be positive"

    tickers = raw["universe"]["tickers"]
    assert len(tickers) > 0, "universe.tickers must not be empty"

    rr_profiles: dict[str, RRProfile] = {}
    for regime_name, profile_raw in raw["rr_profiles"].items():
        conv_map = {
            int(k): float(v)
            for k, v in profile_raw["size_multiplier_by_conviction"].items()
        }
        rr_profiles[regime_name] = RRProfile(
            stop_atr_mult=float(profile_raw["stop_atr_mult"]),
            target_atr_mult=float(profile_raw["target_atr_mult"]),
            size_multiplier_by_conviction=conv_map,
        )

    config = Config(
        universe=UniverseConfig(tickers=tickers),
        account=AccountConfig(
            paper=bool(raw["account"]["paper"]),
            nav=nav,
        ),
        risk=RiskConfig(
            max_trade_risk_pct=float(raw["risk"]["max_trade_risk_pct"]),
            max_position_pct=float(raw["risk"]["max_position_pct"]),
            max_portfolio_heat_pct=float(raw["risk"]["max_portfolio_heat_pct"]),
            max_sector_positions=int(raw["risk"]["max_sector_positions"]),
            daily_loss_limit_pct=float(raw["risk"]["daily_loss_limit_pct"]),
            max_position_duration_minutes=int(raw["risk"]["max_position_duration_minutes"]),
            asset_cache_refresh_seconds=int(raw["risk"].get("asset_cache_refresh_seconds", 60)),
            duration_check_interval_seconds=int(raw["risk"].get("duration_check_interval_seconds", 300)),
        ),
        signal=SignalConfig(
            entry_threshold=float(raw["signal"]["entry_threshold"]),
            atr_period=int(raw["signal"]["atr_period"]),
            ema_fast=int(raw["signal"]["ema_fast"]),
            ema_slow=int(raw["signal"]["ema_slow"]),
            rsi_period=int(raw["signal"]["rsi_period"]),
            vwap_deviation_bands=[float(v) for v in raw["signal"]["vwap_deviation_bands"]],
            orb_window_minutes=int(raw["signal"]["orb_window_minutes"]),
            min_bars=int(raw["signal"].get("min_bars", 30)),
            confidence_threshold=float(raw["signal"].get("confidence_threshold", 0.6)),
            atr_spike_multiplier=float(raw["signal"].get("atr_spike_multiplier", 3.0)),
            rvol_trend_min=float(raw["signal"].get("rvol_trend_min", 1.5)),
            rvol_ranging_min=float(raw["signal"].get("rvol_ranging_min", 1.3)),
            no_trade_windows=raw["signal"].get("no_trade_windows", []),
        ),
        regime=RegimeConfig(
            news_poll_interval_seconds=int(raw["regime"]["news_poll_interval_seconds"]),
            min_conviction_to_trade=int(raw["regime"]["min_conviction_to_trade"]),
        ),
        llm=LlmConfig(
            groq_model=str(raw["llm"]["groq_model"]),
            cache_ttl_minutes=int(raw["llm"].get("cache_ttl_minutes", 10)),
            stale_regime_minutes=int(raw["llm"].get("stale_regime_minutes", 120)),
        ),
        execution=ExecutionConfig(
            order_retry_sleep_seconds=float(raw.get("execution", {}).get("order_retry_sleep_seconds", 0.5)),
            latency_warn_seconds=float(raw.get("execution", {}).get("latency_warn_seconds", 0.1)),
            min_trail_increment_atr_fraction=float(raw.get("execution", {}).get("min_trail_increment_atr_fraction", 0.1)),
        ),
        memory=MemoryConfig(
            min_outcomes_for_summary=int(raw.get("memory", {}).get("min_outcomes_for_summary", 5)),
        ),
        rr_profiles=rr_profiles,
        alpaca_api_key=alpaca_api_key,
        alpaca_secret_key=alpaca_secret_key,
        alpaca_base_url=alpaca_base_url,
        alpaca_data_url=alpaca_data_url,
        groq_api_key=groq_api_key,
        finnhub_api_key=finnhub_api_key,
        polygon_api_key=polygon_api_key,
        oanda_api_key=oanda_api_key,
        oanda_account_id=oanda_account_id,
    )
    forex_raw = raw.get("forex", {})
    if forex_raw:
        config.forex = ForexConfig(
            pairs=forex_raw.get("pairs", []),
            risk_pct=float(forex_raw.get("risk_pct", 0.005)),
            adx_trend_threshold=float(forex_raw.get("adx_trend_threshold", 25.0)),
            size_unit=int(forex_raw.get("size_unit", 1000)),
        )

    log.info("Config loaded. NAV=%.0f, tickers=%d", nav, len(tickers))
    return config
