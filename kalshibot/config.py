from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    series_per_section: int = 18
    max_events_per_series: int = 4
    max_markets_per_event: int = 4
    cache_ttl_seconds: int = 45
    request_timeout_seconds: float = 20.0
    max_concurrency: int = 6
    min_edge: float = 0.02


settings = Settings()
