from __future__ import annotations

import os
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_RATE_LIMITED = 3

DEFAULT_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"


def _strip_secret(value: object) -> str:
    text = str(value or "").strip().strip('"').strip("'")
    return text.strip()


def _env_files() -> tuple[str, ...]:
    files = [".env"]
    home_env = Path.home() / ".kalshi" / "env"
    if home_env.is_file():
        files.append(str(home_env))
    return tuple(files)


def apply_kalshi_shell_env(path: Path | None = None, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Load ~/.kalshi/env even when it uses bash `export KEY=value` and $HOME.

    systemd EnvironmentFile cannot parse `export` or expand $HOME — that is why
    the Pi timer logged 'Ignoring invalid environment assignment'.
    """
    dest = environ if environ is not None else os.environ
    path = path or Path.home() / ".kalshi" / "env"
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = os.path.expandvars(os.path.expanduser(_strip_secret(value)))
        dest.setdefault(key, value)
        loaded[key] = dest[key]
    return loaded


class HourlySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_files(),
        extra="ignore",
        env_ignore_empty=True,
        case_sensitive=False,
    )

    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    kalshi_private_key: str = ""  # raw PEM from Actions secret
    kalshi_base_url: str = DEFAULT_BASE_URL
    kalshi_demo_url: str = "https://demo-api.kalshi.co/trade-api/v2"
    use_demo: bool = True
    live_trading: bool = False
    confirm_live: str = "NO"
    # Kill switch. Default on so a stale Pi timer cannot place live orders
    # after this checkout is pulled. Set HALTED=false to resume.
    halted: bool = True

    bankroll: float = 40.00
    min_net_edge: float = 0.06
    soft_net_edge: float = 0.06
    max_risk_pct: float = 0.05
    max_risk_dollars: float = 2.00
    preferred_risk_dollars: float = 1.75
    kelly_mult: float = 0.25
    min_strike_distance_pct: float = 0.005
    min_strike_sigma: float = 1.5
    close_strike_edge: float = 0.10
    vol_pause_mult: float = 2.0

    assets: str = "BTC,ETH"
    max_markets_per_asset: int = 12
    max_ideas_per_run: int = 1
    min_minutes_left: float = 3
    max_spread: float = 0.06
    min_visible_depth_contracts: int = 5

    spot_source: str = "cfbenchmarks"
    hourly_vol_fallback_btc: float = 0.004
    hourly_vol_fallback_eth: float = 0.005

    request_timeout_seconds: float = 20.0
    artifacts_dir: str = "artifacts"
    state_path: str = "artifacts/hourly_state.json"

    @field_validator("confirm_live", mode="before")
    @classmethod
    def _upper_confirm(cls, value: object) -> str:
        return str(value or "NO").strip().upper()

    @field_validator("kalshi_api_key_id", "kalshi_private_key_path", mode="before")
    @classmethod
    def _clean_key_fields(cls, value: object) -> str:
        return _strip_secret(value)

    @model_validator(mode="after")
    def default_kalshi_key_files(self) -> HourlySettings:
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
    def live_enabled(self) -> bool:
        return (not self.halted) and bool(self.live_trading) and self.confirm_live == "YES"

    @property
    def trading_base_url(self) -> str:
        return self.kalshi_demo_url if self.use_demo else self.kalshi_base_url

    def ensure_private_key_file(self) -> str:
        """Write KALSHI_PRIVATE_KEY PEM to a temp path when Actions injects it."""
        if self.kalshi_private_key_path:
            expanded = os.path.expandvars(os.path.expanduser(self.kalshi_private_key_path))
            self.kalshi_private_key_path = expanded
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
    apply_kalshi_shell_env()
    settings = HourlySettings()
    settings.ensure_private_key_file()
    return settings
