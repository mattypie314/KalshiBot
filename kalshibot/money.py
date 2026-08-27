from __future__ import annotations

from datetime import datetime, timezone


def parse_dollars(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clamp_prob(value: float) -> float:
    return min(0.99, max(0.01, value))


def mid_price(bid: float | None, ask: float | None) -> float | None:
    if bid is None and ask is None:
        return None
    if bid is None:
        return ask
    if ask is None:
        return bid
    if ask < bid:
        return bid
    return (bid + ask) / 2.0


def parse_close_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def years_until(close: datetime | None, now: datetime | None = None) -> float | None:
    if close is None:
        return None
    now = now or datetime.now(timezone.utc)
    seconds = (close - now).total_seconds()
    return seconds / (365.25 * 24 * 3600)
