"""Settings for the 15m BTC/ETH bot. Separate artifacts from the hourly scanner."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config import (
    DEFAULT_BASE_URL,
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_RATE_LIMITED,
    _env_files,
    _strip_secret,
    apply_kalshi_shell_env,
)

__all__ = [
    "EXIT_OK",
    "EXIT_CONFIG",
    "EXIT_RATE_LIMITED",
    "FifteenSettings",
    "load_fifteen_settings",
]


class FifteenSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_files(),
        extra="ignore",
        env_ignore_empty=True,
        case_sensitive=False,
        populate_by_name=True,
    )

    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    kalshi_private_key: str = ""
    kalshi_base_url: str = DEFAULT_BASE_URL
    kalshi_demo_url: str = "https://demo-api.kalshi.co/trade-api/v2"
    use_demo: bool = True
    live_trading: bool = False
    confirm_live: str = "NO"
    halted: bool = True

    pot_start: float = Field(default=5.00, validation_alias=AliasChoices("FIFTEEN_POT_START", "POT_START"))
    pot_double: float = Field(default=10.00, validation_alias=AliasChoices("FIFTEEN_POT_DOUBLE", "POT_DOUBLE"))
    bankroll: float = Field(default=5.00, validation_alias=AliasChoices("FIFTEEN_BANKROLL"))
    min_net_edge: float = Field(default=0.04, validation_alias=AliasChoices("FIFTEEN_MIN_NET_EDGE", "MIN_NET_EDGE"))
    max_risk_pct: float = 0.40
    max_risk_dollars: float = Field(default=2.00, validation_alias=AliasChoices("FIFTEEN_MAX_RISK_DOLLARS"))
    preferred_risk_dollars: float = Field(default=1.50, validation_alias=AliasChoices("FIFTEEN_PREFERRED_RISK_DOLLARS"))
    kelly_mult: float = 0.25

    assets: str = "BTC,ETH"
    max_markets_per_asset: int = 8
    max_ideas_per_run: int = Field(default=1, validation_alias=AliasChoices("FIFTEEN_MAX_IDEAS_PER_RUN", "MAX_IDEAS_PER_RUN"))
    min_minutes_left: float = Field(default=8.0, validation_alias=AliasChoices("FIFTEEN_MIN_MINUTES_LEFT", "MIN_MINUTES_LEFT"))
    max_spread: float = 0.10
    spot_source: str = "cfbenchmarks"
    require_settlement_index: bool = True
    require_maker: bool = True
    news_pause: bool = False
    hourly_vol_fallback_btc: float = 0.004
    hourly_vol_fallback_eth: float = 0.005

    request_timeout_seconds: float = 20.0
    artifacts_dir: str = "artifacts"
    state_path: str = Field(default="artifacts/fifteen_state.json", validation_alias=AliasChoices("FIFTEEN_STATE_PATH"))
    scan_log_path: str = Field(default="artifacts/fifteen_scan_log.jsonl", validation_alias=AliasChoices("FIFTEEN_SCAN_LOG_PATH"))
    paper_log_path: str = Field(default="artifacts/fifteen_paper_log.jsonl", validation_alias=AliasChoices("FIFTEEN_PAPER_LOG_PATH"))
    trade_log_path: str = Field(default="artifacts/fifteen_trade_log.jsonl", validation_alias=AliasChoices("FIFTEEN_TRADE_LOG_PATH"))
    pot_path: str = Field(default="artifacts/fifteen_pot.json", validation_alias=AliasChoices("FIFTEEN_POT_PATH"))
    paper_fill_model: str = "assumed-maker-fill"
    cash_out_bid: float = Field(
        default=0.99,
        validation_alias=AliasChoices("CASH_OUT_BID", "FIFTEEN_CASH_OUT_BID"),
    )
    take_profit_cents: float = Field(
        default=0.02,
        validation_alias=AliasChoices("TAKE_PROFIT_CENTS", "FIFTEEN_TAKE_PROFIT_CENTS"),
    )

    @field_validator("paper_fill_model", mode="before")
    @classmethod
    def _paper_fill_model(cls, value: object) -> str:
        text = str(value or "assumed-maker-fill").strip().lower().replace("_", "-")
        if text in {"assumed-maker-fill", "assumed", "maker", "default"}:
            return "assumed-maker-fill"
        if text in {"unfilled", "none", "strict"}:
            return "unfilled"
        return "assumed-maker-fill"

    @field_validator("confirm_live", mode="before")
    @classmethod
    def _upper_confirm(cls, value: object) -> str:
        return str(value or "NO").strip().upper()

    @field_validator("kalshi_api_key_id", "kalshi_private_key_path", mode="before")
    @classmethod
    def _clean_key_fields(cls, value: object) -> str:
        return _strip_secret(value)

    @model_validator(mode="after")
    def default_kalshi_key_files(self) -> FifteenSettings:
        home = Path.home() / ".kalshi"
        pem = home / "kalshi_private_key.pem"
        path = (self.kalshi_private_key_path or "").strip()
        if path.startswith("KALSHI_PRIVATE_KEY_PATH="):
            path = path.split("=", 1)[-1].strip()
        path = os.path.expandvars(os.path.expanduser(path))
        if not path or not Path(path).is_file():
            path = str(pem) if pem.is_file() else path
        self.kalshi_private_key_path = path
        id_file = home / "api_key_id"
        if not id_file.is_file():
            id_file = home / "key_id"
        if id_file.is_file() and (not self.kalshi_api_key_id or path == str(pem)):
            self.kalshi_api_key_id = id_file.read_text().strip()
        return self

    @property
    def asset_list(self) -> list[str]:
        return [part.strip().upper() for part in self.assets.split(",") if part.strip()]

    @property
    def live_enabled(self) -> bool:
        return (not self.halted) and bool(self.live_trading) and self.confirm_live == "YES"

    @property
    def trading_base_url(self) -> str:
        return self.kalshi_demo_url if self.use_demo else self.kalshi_base_url

    def ensure_private_key_file(self) -> str:
        if self.kalshi_private_key_path:
            expanded = os.path.expandvars(os.path.expanduser(self.kalshi_private_key_path))
            self.kalshi_private_key_path = expanded
            return self.kalshi_private_key_path
        pem = (self.kalshi_private_key or os.environ.get("KALSHI_PRIVATE_KEY") or "").strip()
        if not pem:
            return ""
        dest = Path.home() / ".kalshi" / "fifteen_private_key.pem"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not pem.endswith("\n"):
            pem += "\n"
        dest.write_text(pem)
        dest.chmod(0o600)
        self.kalshi_private_key_path = str(dest)
        return self.kalshi_private_key_path


def load_fifteen_settings() -> FifteenSettings:
    apply_kalshi_shell_env()
    settings = FifteenSettings()
    settings.ensure_private_key_file()
    return settings
