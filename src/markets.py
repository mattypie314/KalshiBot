"""Discover current/next-hour BTC and ETH threshold books."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

KNOWN_THRESHOLD_SERIES = ("KXBTCD", "KXETHD")
KNOWN_RANGE_SERIES = ("KXBTC", "KXETH")
FIFTEEN_SERIES = ("KXBTC15M", "KXETH15M")
THRESHOLD_SERIES = KNOWN_THRESHOLD_SERIES

_ASSET_FROM_SERIES = (
    (re.compile(r"ETH"), "ETH"),
    (re.compile(r"BTC|BITCOIN"), "BTC"),
)
_TICKER_STRIKE = re.compile(r"-T([0-9]+(?:\.[0-9]+)?)$")
_OR_ABOVE = re.compile(
    r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s+or\s+above",
    re.IGNORECASE,
)
_ABOVE = re.compile(
    r"(?:above|over|at\s+least|>=|≥)\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HourlyMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    asset: str
    title: str
    yes_sub_title: str
    threshold: float
    strike_type: str
    close_time: datetime
    status: str
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    yes_bid_size: float
    yes_ask_size: float
    no_bid_size: float
    no_ask_size: float
    rules_primary: str
    rules_secondary: str
    settlement_source: str
    exchange_index: int | None
    used_15m_fallback: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def spread(self) -> float:
        if self.yes_ask <= 0 or self.yes_bid <= 0:
            return 1.0
        return max(0.0, self.yes_ask - self.yes_bid)

    @property
    def minutes_left(self) -> float:
        return max(0.0, (self.close_time - datetime.now(timezone.utc)).total_seconds() / 60.0)


def parse_dollars(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.replace(",", "").replace("$", "").strip()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_contract_price(dollars: object, cents: object = None) -> float | None:
    parsed = parse_dollars(dollars)
    if parsed is not None:
        return min(1.0, max(0.0, parsed))
    parsed = parse_dollars(cents)
    if parsed is None:
        return None
    if parsed > 1.0:
        parsed = parsed / 100.0
    return min(1.0, max(0.0, parsed))


def parse_close_time(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def asset_from_text(*parts: str) -> str | None:
    blob = " ".join(p or "" for p in parts).upper()
    for pattern, asset in _ASSET_FROM_SERIES:
        if pattern.search(blob):
            return asset
    return None


def parse_threshold(market: dict[str, Any]) -> tuple[float | None, str]:
    strike_type = str(market.get("strike_type") or "").lower() or "greater"
    floor = parse_dollars(market.get("floor_strike"))
    if floor is not None:
        return floor, strike_type or "greater"
    custom = market.get("custom_strike") or {}
    floor = parse_dollars(custom.get("floor_strike"))
    if floor is not None:
        return floor, strike_type or "greater"
    blob = f"{market.get('yes_sub_title') or ''} {market.get('subtitle') or ''} {market.get('title') or ''}"
    for pattern in (_OR_ABOVE, _ABOVE):
        match = pattern.search(blob)
        if match:
            return parse_dollars(match.group(1)), "greater"
    match = _TICKER_STRIKE.search(str(market.get("ticker") or ""))
    if match:
        return parse_dollars(match.group(1)), "greater"
    return None, strike_type


def settlement_label(event: dict[str, Any], market: dict[str, Any]) -> str:
    sources = event.get("settlement_sources") or []
    names = []
    for row in sources:
        if isinstance(row, dict):
            name = str(row.get("name") or "").strip()
            url = str(row.get("url") or "").strip()
            if name and url:
                names.append(f"{name} ({url})")
            elif name:
                names.append(name)
    rules = str(market.get("rules_primary") or event.get("rules_primary") or "")
    if names:
        return "; ".join(names)
    if "CF Benchmarks" in rules or "BRTI" in rules or "RTI" in rules:
        return "CF Benchmarks RTI (from market rules)"
    return "unknown (see market rules)"


def in_current_or_next_hour(close: datetime, now: datetime | None = None) -> bool:
    """Keep closes at the end of this clock hour or the next one.

    A 7:00 AM print while it is 6:43 AM is the current hour's settlement.
    """
    now = now or datetime.now(timezone.utc)
    if close.tzinfo is None:
        close = close.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    close = close.astimezone(timezone.utc)
    if close <= now:
        return False
    this_hour_end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    next_hour_end = this_hour_end + timedelta(hours=1)
    return close <= next_hour_end + timedelta(seconds=1)


def is_fifteen_series(series_ticker: str) -> bool:
    return str(series_ticker or "").upper().endswith("15M")


def is_threshold_hourly_series(series: dict[str, Any]) -> bool:
    ticker = str(series.get("ticker") or "").upper()
    title = str(series.get("title") or "").lower()
    freq = str(series.get("frequency") or "").lower()
    if ticker in THRESHOLD_SERIES:
        return True
    if is_fifteen_series(ticker):
        return False
    asset = asset_from_text(ticker, title)
    if asset not in {"BTC", "ETH"}:
        return False
    hourly = freq in {"hourly", "hour", "1h", "hours"} or "hour" in title
    threshold = "above" in title or "below" in title or "threshold" in title
    return hourly and threshold


def _quote(market: dict[str, Any]) -> tuple[float, float, float, float]:
    yes_bid = parse_contract_price(market.get("yes_bid_dollars"), market.get("yes_bid"))
    yes_ask = parse_contract_price(market.get("yes_ask_dollars"), market.get("yes_ask"))
    no_bid = parse_contract_price(market.get("no_bid_dollars"), market.get("no_bid"))
    no_ask = parse_contract_price(market.get("no_ask_dollars"), market.get("no_ask"))
    if yes_bid is None:
        yes_bid = 0.0
    if yes_ask is None:
        yes_ask = 0.0
    if no_ask is None and yes_bid is not None:
        no_ask = max(0.0, min(1.0, 1.0 - yes_bid))
    if no_bid is None and yes_ask is not None:
        no_bid = max(0.0, min(1.0, 1.0 - yes_ask))
    return yes_bid or 0.0, yes_ask or 0.0, no_bid or 0.0, no_ask or 0.0


def market_from_api(
    market: dict[str, Any],
    event: dict[str, Any],
    *,
    used_15m_fallback: bool = False,
) -> HourlyMarket | None:
    status = str(market.get("status") or "").lower()
    if status not in {"open", "active"}:
        return None
    close = parse_close_time(market.get("close_time") or event.get("strike_date") or event.get("close_time"))
    if close is None:
        return None
    threshold, strike_type = parse_threshold(market)
    if threshold is None or strike_type not in {"greater", "greater_or_equal", "at_least", ""}:
        return None
    series = str(
        market.get("series_ticker")
        or event.get("series_ticker")
        or str(event.get("event_ticker") or "").split("-", 1)[0]
    ).upper()
    asset = asset_from_text(series, event.get("title") or "", market.get("title") or "")
    if asset not in {"BTC", "ETH"}:
        return None
    yes_bid, yes_ask, no_bid, no_ask = _quote(market)
    return HourlyMarket(
        ticker=str(market.get("ticker") or ""),
        event_ticker=str(market.get("event_ticker") or event.get("event_ticker") or ""),
        series_ticker=series,
        asset=asset,
        title=str(event.get("title") or market.get("title") or ""),
        yes_sub_title=str(market.get("yes_sub_title") or market.get("subtitle") or ""),
        threshold=threshold,
        strike_type=strike_type or "greater",
        close_time=close,
        status=status,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        yes_bid_size=parse_dollars(market.get("yes_bid_size_fp")) or 0.0,
        yes_ask_size=parse_dollars(market.get("yes_ask_size_fp")) or 0.0,
        no_bid_size=parse_dollars(market.get("no_bid_size_fp")) or 0.0,
        no_ask_size=parse_dollars(market.get("no_ask_size_fp")) or 0.0,
        rules_primary=str(market.get("rules_primary") or ""),
        rules_secondary=str(market.get("rules_secondary") or ""),
        settlement_source=settlement_label(event, market),
        exchange_index=market.get("exchange_index") if isinstance(market.get("exchange_index"), int) else None,
        used_15m_fallback=used_15m_fallback,
    )


def nearest_strikes(markets: Iterable[HourlyMarket], spot: float, limit: int) -> list[HourlyMarket]:
    ranked = sorted(markets, key=lambda m: abs(m.threshold - spot))
    return ranked[:limit]


class MarketDiscovery:
    def __init__(self, client: Any) -> None:
        self.client = client

    def discover(
        self,
        assets: list[str],
        *,
        now: datetime | None = None,
        allow_15m_fallback: bool = True,
        max_per_asset: int = 12,
        spots: dict[str, float] | None = None,
    ) -> list[HourlyMarket]:
        now = now or datetime.now(timezone.utc)
        wanted = {a.upper() for a in assets}
        hourly = self._load_series(THRESHOLD_SERIES, now, used_15m=False)
        if not hourly and allow_15m_fallback:
            logger.info("No hourly BTC/ETH threshold books in the current/next hour; falling back to 15m")
            hourly = self._load_series(FIFTEEN_SERIES, now, used_15m=True, require_hour_window=False)
        out: list[HourlyMarket] = []
        spots = spots or {}
        for asset in wanted:
            subset = [m for m in hourly if m.asset == asset]
            spot = spots.get(asset)
            if spot and subset:
                subset = nearest_strikes(subset, spot, max_per_asset)
            else:
                subset = subset[:max_per_asset]
            out.extend(subset)
        return out

    def next_settlements(self, markets: list[HourlyMarket]) -> list[str]:
        seen: dict[str, HourlyMarket] = {}
        for market in markets:
            key = f"{market.asset}:{market.close_time.isoformat()}"
            seen.setdefault(key, market)
        rows = []
        for market in sorted(seen.values(), key=lambda m: m.close_time):
            rows.append(
                f"{market.asset} {market.close_time.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
                f"({market.series_ticker} {market.settlement_source})"
            )
        return rows

    def _load_series(
        self,
        series_tickers: tuple[str, ...],
        now: datetime,
        *,
        used_15m: bool,
        require_hour_window: bool = True,
    ) -> list[HourlyMarket]:
        found: list[HourlyMarket] = []
        for series in series_tickers:
            try:
                events = self.client.open_events(series, limit=20)
            except Exception as exc:  # noqa: BLE001 — public scan should survive one bad series
                logger.warning("Events failed for %s: %s", series, exc)
                continue
            for event in events:
                for raw in event.get("markets") or []:
                    market = market_from_api(raw, event, used_15m_fallback=used_15m)
                    if market is None:
                        continue
                    if require_hour_window and not in_current_or_next_hour(market.close_time, now):
                        continue
                    if used_15m:
                        # 15m fallback: only the live window (close in the future, < 20 min).
                        secs = (market.close_time - now).total_seconds()
                        if secs <= 0 or secs > 20 * 60:
                            continue
                    found.append(market)
        return found
