from __future__ import annotations

from kalshibot.assets import identify_asset

FIFTEEN_SERIES = (
    "KXBTC15M",
    "KXETH15M",
    "KXSOL15M",
    "KXGOLD15M",
    "KXSILVER15M",
)

CRYPTO_SHARD = 2
METALS_SHARD = 0

HOURLY_FREQUENCIES = {"hourly", "hour", "1h", "hours"}
HOURLY_MAX_SECONDS = 75 * 60


def series_code(ticker: str) -> str:
    return str(ticker or "").upper().split("-", 1)[0]


def is_daily_ticker(ticker: str) -> bool:
    """KXETHD / KXBTCD / KXDOGED and their market tickers. Not 15-minute series."""
    code = series_code(ticker)
    return bool(code) and code.endswith("D") and not code.endswith("15M")


def shard_for_series(series_ticker: str, title: str = "") -> int:
    asset = identify_asset(series_ticker, title)
    if asset and asset.key in {"GOLD", "SILVER", "COPPER", "WTI", "BRENT", "NATGAS", "RBOB"}:
        return METALS_SHARD
    return CRYPTO_SHARD


def is_hourly_series(series: dict) -> bool:
    freq = str(series.get("frequency") or "").lower()
    title = str(series.get("title") or "").lower()
    ticker = str(series.get("ticker") or "").upper()
    if is_daily_ticker(ticker) or "daily" in freq or "daily" in title:
        return False
    if "15" in freq or ticker.endswith("15M"):
        return False
    if ticker.endswith("H") or freq in HOURLY_FREQUENCIES:
        return True
    return "hour" in title and "15" not in title


def is_campaign_hourly_universe(series: dict) -> bool:
    category = str(series.get("category") or "")
    if category not in {"Crypto", "Commodities"}:
        return False
    return is_hourly_series(series)
