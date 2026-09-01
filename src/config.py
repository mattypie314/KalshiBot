from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_RATE_LIMITED = 3


class HourlySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env",),
        extra="ignore",
        env_ignore_empty=True,
        case_sensitive=False,
    )

    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    kalshi_private_key: str = ""  # raw PEM from Actions secret
    kalshi_base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    kalshi_demo_url: str = "https://demo-api.kalshi.co/trade-api/v2"
    use_demo: bool = True
    live_trading: bool = False
    confirm_live: str = "NO"

    bankroll: float = 46.36
    min_net_edge: float = 0.06
    soft_net_edge: float = 0.04
    max_risk_pct: float = 0.05
    max_risk_dollars: float = 3.00
    preferred_risk_dollars: float = 2.00
    kelly_mult: float = 0.25

    assets: str = "BTC,ETH"
    max_markets_per_asset: int = 12
    max_ideas_per_run: int = 1
    min_minutes_left: float = 3
    max_spread: float = 0.06
    min_visible_depth_contracts: int = 5

    spot_source: str = "binance"
    hourly_vol_fallback_btc: float = 0.004
    hourly_vol_fallback_eth: float = 0.005

    request_timeout_seconds: float = 20.0
    artifacts_dir: str = "artifacts"
    state_path: str = "artifacts/hourly_state.json"

    @field_validator("confirm_live", mode="before")
    @classmethod
    def _upper_confirm(cls, value: object) -> str:
        return str(value or "NO").strip().upper()

    @property
    def asset_list(self) -> list[str]:
        return [part.strip().upper() for part in self.assets.split(",") if part.strip()]

    @property
    def live_enabled(self) -> bool:
        return bool(self.live_trading) and self.confirm_live == "YES"

    @property
    def trading_base_url(self) -> str:
        return self.kalshi_demo_url if self.use_demo else self.kalshi_base_url

    def ensure_private_key_file(self) -> str:
        """Write KALSHI_PRIVATE_KEY PEM to a temp path when Actions injects it."""
        if self.kalshi_private_key_path:
            return self.kalshi_private_key_path
        pem = (self.kalshi_private_key or os.environ.get("KALSHI_PRIVATE_KEY") or "").strip()
        if not pem:
            return ""
        dest = Path.home() / ".kalshi" / "hourly_private_key.pem"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not pem.endswith("\n"):
            pem += "\n"
        dest.write_text(pem)
        dest.chmod(0o600)
        self.kalshi_private_key_path = str(dest)
        return self.kalshi_private_key_path


def load_settings() -> HourlySettings:
    settings = HourlySettings()
    settings.ensure_private_key_file()
    return settings
