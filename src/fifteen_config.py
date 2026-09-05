"""Settings for the dedicated BTC/ETH 15-minute bot.

Hourly knobs stay in HourlySettings. This file is the 15m checkout's .env.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config import DEFAULT_BASE_URL, _env_files, _strip_secret, apply_kalshi_shell_env


class FifteenSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_files(),
        extra="ignore",
        env_ignore_empty=True,
        case_sensitive=False,
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

    # Own $5 pot — not hourly BANKROLL / hourly_pot.
    fifteen_pot_start: float = 5.00
    fifteen_pot_ask: float = 10.00
    min_risk_dollars: float = 0.10
    max_risk_dollars: float = 1.50
    preferred_risk_dollars: float = 0.85
    max_risk_pct: float = 0.05
    kelly_mult: float = 0.25

    assets: str = "BTC,ETH"
    series: str = "KXBTC15M,KXETH15M"
    exchange_index: int = 2
    max_markets_per_asset: int = 8
    max_ideas_per_window: int = 1
    require_settlement_index: bool = True
    require_maker: bool = True
    news_pause: bool = False

    # Edge loop: first 2–4 minutes of :00/:15/:30/:45 ET (timer fires at :01).
    edge_loop_min_into: float = 1.0
    edge_loop_max_into: float = 4.0
    # Hard skip under this many minutes unless last-minute maker is decided.
    min_minutes_left: float = 8.0
    mid_tolerance: float = 0.04
    vol_pause_mult: float = 2.0
    max_daily_losses: int = 3
    half_sigma_recheck: bool = True

    last_minute_maker: bool = True
    last_minute_minutes: float = 3.0
    last_minute_min_price: float = 0.74
    last_minute_max_price: float = 0.93
    last_minute_min_risk: float = 0.10
    last_minute_max_risk: float = 0.75
    stack_last_minute_with_edge: bool = False

    stop_frac_of_risk: float = 0.25
    stop_frac_from_fill: float = 0.10
    stop_dollar_cap: float = 0.40
    take_profit_cents: float = 0.02

    spot_source: str = "cfbenchmarks"
    # TEMPORARY HEURISTIC: 90m of 1m bars, still expressed as hourly-equivalent vol
    # so the existing zero-drift model can scale by sqrt(hours_left). Hourly still
    # uses ~4h. Revisit if 15m fair values look systematically off.
    fifteen_vol_lookback_minutes: int = 90
    fifteen_vol_fallback_btc: float = 0.004
    fifteen_vol_fallback_eth: float = 0.005

    request_timeout_seconds: float = 20.0
    artifacts_dir: str = "artifacts"
    pot_path: str = "artifacts/fifteen_pot.json"
    state_path: str = "artifacts/fifteen_state.json"
    last_run_path: str = "artifacts/last_run.json"
    scan_log_path: str = "artifacts/fifteen_scan_log.jsonl"
    paper_log_path: str = "artifacts/fifteen_paper_log.jsonl"
    trade_log_path: str = "artifacts/fifteen_trade_log.jsonl"
    paper_fill_model: str = "assumed-maker-fill"

    @field_validator("paper_fill_model", mode="before")
    @classmethod
    def _paper_fill_model(cls, value: object) -> str:
        text = str(value or "assumed-maker-fill").strip().lower().replace("_", "-")
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

    @field_validator("series", mode="before")
    @classmethod
    def _series(cls, value: object) -> str:
        text = str(value or "KXBTC15M,KXETH15M").upper()
        return text

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
        if self.kalshi_base_url and "tra>" in self.kalshi_base_url:
            self.kalshi_base_url = DEFAULT_BASE_URL
        return self

    @property
    def asset_list(self) -> list[str]:
        return [part.strip().upper() for part in self.assets.split(",") if part.strip()]

    @property
    def series_list(self) -> list[str]:
        return [part.strip().upper() for part in self.series.split(",") if part.strip()]

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
