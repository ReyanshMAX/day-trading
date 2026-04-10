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
    max_portfolio_heat_pct: float
    max_sector_positions: int
    daily_loss_limit_pct: float


@dataclass
class SignalConfig:
    entry_threshold: float
    atr_period: int
    ema_fast: int
    ema_slow: int
    rsi_period: int
    vwap_deviation_bands: list[float]
    orb_window_minutes: int


@dataclass
class RegimeConfig:
    news_poll_interval_seconds: int
    min_conviction_to_trade: int


@dataclass
class RRProfile:
    stop_atr_mult: float
    target_atr_mult: float
    size_multiplier_by_conviction: dict[int, float]


@dataclass
class Config:
    universe: UniverseConfig
    account: AccountConfig
    risk: RiskConfig
    signal: SignalConfig
    regime: RegimeConfig
    rr_profiles: dict[str, RRProfile]

    # Env-loaded secrets
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_url: str = "https://data.alpaca.markets"
    groq_api_key: str = ""


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
            max_portfolio_heat_pct=float(raw["risk"]["max_portfolio_heat_pct"]),
            max_sector_positions=int(raw["risk"]["max_sector_positions"]),
            daily_loss_limit_pct=float(raw["risk"]["daily_loss_limit_pct"]),
        ),
        signal=SignalConfig(
            entry_threshold=float(raw["signal"]["entry_threshold"]),
            atr_period=int(raw["signal"]["atr_period"]),
            ema_fast=int(raw["signal"]["ema_fast"]),
            ema_slow=int(raw["signal"]["ema_slow"]),
            rsi_period=int(raw["signal"]["rsi_period"]),
            vwap_deviation_bands=[float(v) for v in raw["signal"]["vwap_deviation_bands"]],
            orb_window_minutes=int(raw["signal"]["orb_window_minutes"]),
        ),
        regime=RegimeConfig(
            news_poll_interval_seconds=int(raw["regime"]["news_poll_interval_seconds"]),
            min_conviction_to_trade=int(raw["regime"]["min_conviction_to_trade"]),
        ),
        rr_profiles=rr_profiles,
        alpaca_api_key=alpaca_api_key,
        alpaca_secret_key=alpaca_secret_key,
        alpaca_base_url=alpaca_base_url,
        alpaca_data_url=alpaca_data_url,
        groq_api_key=groq_api_key,
    )

    log.info("Config loaded. NAV=%.0f, tickers=%d", nav, len(tickers))
    return config
