from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
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
    tracker_path: str = "~/.kalshi/crypto-campaign.json"
    fifteen_bankroll: float = 5.0
    hourly_bankroll: float = 10.0
    pot_stop: float = -0.50
    skip_last_seconds: float = 60.0
    maker_skip_last_seconds: float = 15.0


settings = Settings()
