from __future__ import annotations

from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _kalshi_env_files() -> tuple[str, ...]:
    files = [".env"]
    home_env = Path.home() / ".kalshi" / "env"
    if home_env.is_file():
        files.append(str(home_env))
    return tuple(files)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_kalshi_env_files(), extra="ignore", env_ignore_empty=True)

    kalshi_base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    series_per_section: int = 12
    max_events_per_series: int = 3
    max_markets_per_event: int = 4
    cache_ttl_seconds: int = 60
    request_timeout_seconds: float = 20.0
    max_concurrency: int = 3
    min_edge: float = 0.02
    kalshi_min_interval: float = 0.3
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    kalshi_live: bool = False
    kalshi_auto: bool = True
    tracker_path: str = "~/.kalshi/crypto-campaign.json"
    campaign_bankroll: float = 15.0
    skip_last_seconds: float = 180.0
    hourly_max_seconds: float = 75 * 60
    maker_skip_last_seconds: float = 180.0
    # Small-account playbook. Raise campaign_bankroll as the book grows;
    # leave the percents unless you want a more aggressive book.
    kelly_fraction: float = 0.33
    typical_risk_min: float = 0.03
    typical_risk_max: float = 0.05
    risk_cap: float = 0.08
    risk_hard_max: float = 0.10
    small_bankroll: float = 20.0
    small_bankroll_risk: float = 0.03
    min_net_edge: float = 0.04
    target_net_edge: float = 0.06
    model_buffer: float = 0.025
    max_open_ideas: int = 2
    max_new_ideas_per_fire: int = 1
    min_time_seconds: float = 180.0
    min_stake: float = 0.25
    thin_spread: float = 0.03
    edge_decay_floor: float = 0.02
    revenge_seconds: float = 15 * 60
    maker_join_min: float = 0.74
    maker_join_max: float = 0.93
    maker_min_seconds: float = 15.0
    maker_max_seconds: float = 180.0
    maker_min_spread: float = 0.01
    maker_max_new: int = 2
    maker_risk_cap: float = 0.03
    maker_taker_net_min: float = -0.02

    @model_validator(mode="after")
    def default_kalshi_key_files(self) -> Settings:
        home = Path.home() / ".kalshi"
        if not self.kalshi_api_key_id:
            for name in ("api_key_id", "key_id"):
                path = home / name
                if path.is_file():
                    self.kalshi_api_key_id = path.read_text().strip()
                    break
        if self.kalshi_api_key_id and not self.kalshi_private_key_path:
            pem = home / "kalshi_private_key.pem"
            if pem.is_file():
                self.kalshi_private_key_path = str(pem)
        return self


settings = Settings()
