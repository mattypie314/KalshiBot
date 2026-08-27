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


def shard_for_series(series_ticker: str, title: str = "") -> int:
    asset = identify_asset(series_ticker, title)
    if asset and asset.key in {"GOLD", "SILVER", "COPPER", "WTI", "BRENT", "NATGAS", "RBOB"}:
        return METALS_SHARD
    return CRYPTO_SHARD


def is_hourly_series(series: dict) -> bool:
    freq = str(series.get("frequency") or "").lower()
    title = str(series.get("title") or "").lower()
    ticker = str(series.get("ticker") or "").upper()
    if "15" in freq or ticker.endswith("15M"):
        return False
    if freq in HOURLY_FREQUENCIES:
        return True
    return "hour" in title and "15" not in title and "daily" not in title


def is_campaign_hourly_universe(series: dict) -> bool:
    category = str(series.get("category") or "")
    if category not in {"Crypto", "Commodities"}:
        return False
    return is_hourly_series(series)
