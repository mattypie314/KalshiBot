from __future__ import annotations

import time
from typing import Any

from kalshibot.kalshi import KalshiClient
from kalshibot.money import mid_price, parse_dollars


def _dollars(raw: object) -> float | None:
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0 or value >= 1:
        return None
    return value


def point_from_candle(candle: dict[str, Any]) -> dict[str, Any] | None:
    ts = candle.get("end_period_ts")
    if ts is None:
        return None
    price = candle.get("price") or {}
    bid = candle.get("yes_bid") or {}
    ask = candle.get("yes_ask") or {}
    last = _dollars(price.get("close_dollars")) or _dollars(price.get("previous_dollars"))
    bid_c = _dollars(bid.get("close_dollars"))
    ask_c = _dollars(ask.get("close_dollars"))
    mid = None
    if bid_c is not None and ask_c is not None:
        mid = (bid_c + ask_c) / 2.0
    yes = last or mid or ask_c or bid_c
    if yes is None:
        return None
    return {
        "ts": int(ts),
        "yes": round(yes, 4),
        "bid": bid_c,
        "ask": ask_c,
        "last": last,
    }


def pick_interval(hours: float) -> int:
    if hours <= 8:
        return 1
    if hours <= 48:
        return 60
    return 1440


async def market_chart(
    kalshi: KalshiClient,
    series_ticker: str,
    ticker: str,
    hours: float = 6.0,
) -> dict[str, Any]:
    hours = min(72.0, max(1.0, float(hours)))
    interval = pick_interval(hours)
    end_ts = int(time.time())
    start_ts = end_ts - int(hours * 3600)
    data = await kalshi.get_json(
        f"/series/{series_ticker}/markets/{ticker}/candlesticks",
        params={
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": interval,
            "include_latest_before_start": "true",
        },
    )
    points = [p for p in (point_from_candle(c) for c in data.get("candlesticks") or []) if p]
    live: dict[str, Any] = {}
    try:
        raw = await kalshi.get_json(f"/markets/{ticker}")
        market = raw.get("market") or raw
        bid = parse_dollars(market.get("yes_bid_dollars"))
        ask = parse_dollars(market.get("yes_ask_dollars"))
        mid = mid_price(bid, ask)
        live = {
            "yes_bid": bid,
            "yes_ask": ask,
            "yes": mid,
            "status": market.get("status"),
            "title": market.get("title") or market.get("yes_sub_title"),
            "close_time": market.get("close_time"),
        }
        if mid is not None:
            points.append({"ts": end_ts, "yes": round(mid, 4), "bid": bid, "ask": ask, "last": mid})
    except Exception:
        live = {}
    seen: set[int] = set()
    unique: list[dict[str, Any]] = []
    for point in points:
        if point["ts"] in seen:
            unique[-1] = point
            continue
        seen.add(point["ts"])
        unique.append(point)
    unique.sort(key=lambda p: p["ts"])
    first = unique[0]["yes"] if unique else None
    last = unique[-1]["yes"] if unique else None
    change = None if first in (None, 0) or last is None else last - first
    return {
        "ticker": ticker,
        "series_ticker": series_ticker,
        "interval": interval,
        "hours": hours,
        "points": unique,
        "live": live,
        "change": None if change is None else round(change, 4),
    }
