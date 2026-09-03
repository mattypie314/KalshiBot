"""Parse Kalshi's CF Benchmarks passthrough (BRTI / ERTI)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from src.clock import parse_ts, to_et

INDEX_BY_ASSET = {"BTC": "BRTI", "ETH": "ERTI"}
SETTLEMENT_WINDOW_SECONDS = 60


def parse_cf_index_value(blob: object) -> float | None:
    """Pull a positive index level out of CF / Kalshi envelope shapes."""
    if blob is None or blob == "" or blob == {} or blob == []:
        return None
    if isinstance(blob, bool):
        return None
    if isinstance(blob, (int, float)):
        return float(blob) if blob > 0 else None
    if isinstance(blob, str):
        try:
            number = float(blob.strip())
        except ValueError:
            return None
        return number if number > 0 else None
    if isinstance(blob, list):
        for item in blob:
            parsed = parse_cf_index_value(item)
            if parsed:
                return parsed
        return None
    if not isinstance(blob, dict):
        return None
    for key in (
        "value",
        "VALUE",
        "indexValue",
        "index_value",
        "price",
        "last",
        "payload",
        "data",
        "values",
        "elements",
    ):
        if key not in blob:
            continue
        parsed = parse_cf_index_value(blob[key])
        if parsed:
            return parsed
    return None


def index_id_for(asset: str) -> str | None:
    return INDEX_BY_ASSET.get(str(asset or "").upper())


def official_index_label(asset: str, source: str) -> str:
    """BRTI / ERTI when the print is the settlement index; otherwise PROXY."""
    if str(source or "").strip().lower() == "cfbenchmarks":
        return index_id_for(asset) or "PROXY"
    return "PROXY"


def is_official_index_label(label: str) -> bool:
    return str(label or "").strip().upper() in set(INDEX_BY_ASSET.values())


def _tick_time(item: dict[str, Any]) -> datetime | None:
    for key in ("time", "timestamp", "ts", "t", "date", "serverTime"):
        if key not in item:
            continue
        parsed = parse_ts(item[key])
        if parsed is not None:
            return parsed
    return None


def parse_cf_history_ticks(blob: object) -> list[tuple[datetime, float]]:
    """Pull (time, value) ticks from a CF / Kalshi history envelope."""
    ticks: list[tuple[datetime, float]] = []

    def walk(node: object) -> None:
        if node is None or node == "" or node == {} or isinstance(node, bool):
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        value = None
        for key in ("value", "VALUE", "indexValue", "index_value", "price", "last"):
            if key in node:
                value = parse_cf_index_value(node[key])
                if value:
                    break
        when = _tick_time(node)
        if value and when is not None:
            ticks.append((when, value))
        for key in ("payload", "data", "values", "elements", "history"):
            if key in node:
                walk(node[key])

    walk(blob)
    ticks.sort(key=lambda item: item[0])
    return ticks


def settlement_window(close_time: datetime) -> tuple[datetime, datetime]:
    """Last 60 seconds before the hourly close (Kalshi's official print window)."""
    close = to_et(close_time)
    start = close - timedelta(seconds=SETTLEMENT_WINDOW_SECONDS)
    return start, close


def average_settlement_window(
    ticks: list[tuple[datetime, float]],
    close_time: datetime,
) -> float | None:
    """Simple average of official index ticks in the minute before close.

    This is the Kalshi print: 60 one-second BRTI/ERTI samples, not a Coinbase last tick.
    """
    start, end = settlement_window(close_time)
    values = [value for when, value in ticks if start <= to_et(when) < end and value > 0]
    if not values:
        return None
    return sum(values) / len(values)


def history_query_timestamp(close_time: datetime, *, timespan: str = "HOUR") -> str:
    """UTC timestamp truncated to the CF history timespan that contains the print window."""
    start, _end = settlement_window(close_time)
    utc = start.astimezone(timezone.utc)
    if timespan.upper() == "MINUTE":
        utc = utc.replace(second=0, microsecond=0)
    else:
        utc = utc.replace(minute=0, second=0, microsecond=0)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def official_yes(*, settlement_print: float, strike: float) -> bool:
    """Yes wins if the official 60s average finishes above the line; No wins at or below."""
    return float(settlement_print) > float(strike)
