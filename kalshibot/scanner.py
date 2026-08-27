from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import httpx

from kalshibot.assets import (
    KALSHI_CATEGORY,
    SECTION_LABELS,
    SECTIONS,
    Asset,
    identify_asset,
)
from kalshibot.config import Settings, settings
from kalshibot.kalshi import KalshiClient
from kalshibot.models import (
    confidence_from_spread,
    devig_probs,
    parse_strike,
    price_threshold_prob,
)
from kalshibot.money import mid_price, parse_close_time, parse_dollars, years_until
from kalshibot.spots import SpotService

logger = logging.getLogger(__name__)

SectionId = Literal["crypto", "commodities", "sports"]


@dataclass
class Prediction:
    section: str
    section_label: str
    series_ticker: str
    event_ticker: str
    event_title: str
    market_ticker: str
    market_title: str
    subtitle: str
    status: str
    close_time: str | None
    yes_bid: float | None
    yes_ask: float | None
    market_prob: float | None
    model_prob: float | None
    edge: float
    side: str
    confidence: float
    volume_24h: float
    volume: float
    liquidity: float
    method: str
    rationale: str
    spot: float | None = None
    asset: str | None = None
    score: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "section_label": self.section_label,
            "series_ticker": self.series_ticker,
            "event_ticker": self.event_ticker,
            "event_title": self.event_title,
            "market_ticker": self.market_ticker,
            "market_title": self.market_title,
            "subtitle": self.subtitle,
            "status": self.status,
            "close_time": self.close_time,
            "yes_bid": self.yes_bid,
            "yes_ask": self.yes_ask,
            "market_prob": self.market_prob,
            "model_prob": self.model_prob,
            "edge": round(self.edge, 4),
            "side": self.side,
            "confidence": self.confidence,
            "volume_24h": self.volume_24h,
            "volume": self.volume,
            "liquidity": self.liquidity,
            "method": self.method,
            "rationale": self.rationale,
            "spot": self.spot,
            "asset": self.asset,
            "score": round(self.score, 4),
        }


def _series_volume(series: dict[str, Any]) -> float:
    return parse_dollars(series.get("volume_fp")) or 0.0


def _active_markets(event: dict[str, Any]) -> list[dict[str, Any]]:
    markets = []
    for market in event.get("markets") or []:
        status = str(market.get("status") or "").lower()
        if status in {"active", "open"}:
            markets.append(market)
    return markets


def _executable_edge(model_prob: float, yes_bid: float | None, yes_ask: float | None) -> tuple[float, str]:
    no_ask = (1.0 - yes_bid) if yes_bid is not None else None
    yes_edge = (model_prob - yes_ask) if yes_ask is not None else float("-inf")
    no_edge = ((1.0 - model_prob) - no_ask) if no_ask is not None else float("-inf")
    if yes_edge >= no_edge and math.isfinite(yes_edge):
        return yes_edge, "YES"
    if math.isfinite(no_edge):
        return no_edge, "NO"
    return 0.0, "NONE"


def _score(pred: Prediction) -> float:
    vol = math.log10(1.0 + pred.volume_24h)
    return abs(pred.edge) * (0.4 + pred.confidence) * (1.0 + vol)


class Scanner:
    def __init__(self, cfg: Settings | None = None, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg or settings
        self._owns_http = client is None
        self._http = client or httpx.AsyncClient(
            timeout=self.cfg.request_timeout_seconds,
            headers={"User-Agent": "KalshiBot/0.1"},
        )
        self._kalshi = KalshiClient(self.cfg.kalshi_base_url, self.cfg.request_timeout_seconds, client=self._http)
        self._spots = SpotService(self._http)
        self._lock = asyncio.Lock()
        self._cache: dict[str, Any] = {"at": 0.0, "payload": None}

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def snapshot(self, force: bool = False) -> dict[str, Any]:
        now = asyncio.get_running_loop().time()
        async with self._lock:
            cached = self._cache["payload"]
            if cached and not force and now - float(self._cache["at"]) < self.cfg.cache_ttl_seconds:
                return cached
            payload = await self._scan_all()
            self._cache = {"at": now, "payload": payload}
            return payload

    async def _scan_all(self) -> dict[str, Any]:
        results = await asyncio.gather(
            *(self.scan_section(section) for section in SECTIONS),
            return_exceptions=True,
        )
        sections: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for section, result in zip(SECTIONS, results, strict=True):
            if isinstance(result, Exception):
                logger.exception("Section %s failed", section, exc_info=result)
                sections[section] = {
                    "id": section,
                    "label": SECTION_LABELS[section],
                    "predictions": [],
                    "stats": {"markets": 0, "opportunities": 0},
                }
                errors[section] = str(result)
            else:
                sections[section] = result
        generated_at = datetime.now(timezone.utc).isoformat()
        return {
            "generated_at": generated_at,
            "disclaimer": "Research forecasts only. Not trading advice. No orders are placed.",
            "sections": sections,
            "errors": errors,
        }

    async def scan_section(self, section: SectionId) -> dict[str, Any]:
        category = KALSHI_CATEGORY[section]
        series_list = await self._kalshi.series_for_category(category)
        ranked = sorted(series_list, key=_series_volume, reverse=True)[: self.cfg.series_per_section]
        semaphore = asyncio.Semaphore(self.cfg.max_concurrency)

        async def load(series: dict[str, Any]) -> list[dict[str, Any]]:
            async with semaphore:
                try:
                    return await self._kalshi.open_events(series["ticker"], self.cfg.max_events_per_series)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Events failed for %s: %s", series.get("ticker"), exc)
                    return []

        event_groups = await asyncio.gather(*(load(series) for series in ranked))
        events: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for series, group in zip(ranked, event_groups, strict=True):
            for event in group:
                events.append((series, event))

        assets: list[Asset] = []
        if section != "sports":
            for series, event in events:
                asset = identify_asset(series.get("ticker") or "", event.get("title") or "", event.get("sub_title") or "")
                if asset:
                    assets.append(asset)
        spots = await self._spots.prices_for(assets) if assets else {}

        predictions: list[Prediction] = []
        for series, event in events:
            predictions.extend(self._predict_event(section, series, event, spots))

        predictions.sort(key=lambda p: p.score, reverse=True)
        serialized = [p.as_dict() for p in predictions[:80]]
        opportunities = sum(1 for p in serialized if p["edge"] >= self.cfg.min_edge)
        return {
            "id": section,
            "label": SECTION_LABELS[section],
            "predictions": serialized,
            "stats": {
                "series": len(ranked),
                "events": len(events),
                "markets": len(serialized),
                "opportunities": opportunities,
            },
        }

    def _predict_event(
        self,
        section: str,
        series: dict[str, Any],
        event: dict[str, Any],
        spots: dict[str, float],
    ) -> list[Prediction]:
        markets = _active_markets(event)
        if not markets:
            return []
        if section == "sports" and event.get("mutually_exclusive") and len(markets) >= 2:
            preds = self._sports_mutex(section, series, event, markets)
        else:
            preds = [
                pred
                for market in markets
                if (pred := self._predict_market(section, series, event, market, spots, markets))
            ]
        preds.sort(key=lambda p: p.score, reverse=True)
        return preds[: self.cfg.max_markets_per_event]

    def _predict_market(
        self,
        section: str,
        series: dict[str, Any],
        event: dict[str, Any],
        market: dict[str, Any],
        spots: dict[str, float],
        siblings: list[dict[str, Any]],
    ) -> Prediction | None:
        yes_bid = parse_dollars(market.get("yes_bid_dollars"))
        yes_ask = parse_dollars(market.get("yes_ask_dollars"))
        market_prob = mid_price(yes_bid, yes_ask)
        if market_prob is None:
            return None
        volume_24h = parse_dollars(market.get("volume_24h_fp")) or 0.0
        volume = parse_dollars(market.get("volume_fp")) or 0.0
        liquidity = parse_dollars(market.get("liquidity_dollars")) or 0.0
        close = parse_close_time(market.get("close_time") or event.get("close_time"))
        years = years_until(close)
        asset = identify_asset(series.get("ticker") or "", event.get("title") or "", market.get("title") or "")
        spot = spots.get(asset.key) if asset else None
        spec = parse_strike(market)
        model_prob = None
        method = "market_mid"
        rationale = "No independent model; showing Kalshi midpoint."

        if asset and spot is not None and years is not None and spec.kind != "unknown":
            modeled = price_threshold_prob(spec, spot, max(years, 1e-8), asset.annual_vol)
            if modeled is not None:
                model_prob = modeled
                method = "lognormal_digital"
                strike_txt = spec.floor or spec.cap
                rationale = (
                    f"{asset.display} spot ${spot:,.4f} vs strike {strike_txt} "
                    f"using {asset.annual_vol:.0%} vol."
                )

        if model_prob is None and event.get("mutually_exclusive") and len(siblings) >= 2:
            mids = []
            for sibling in siblings:
                mid = mid_price(
                    parse_dollars(sibling.get("yes_bid_dollars")),
                    parse_dollars(sibling.get("yes_ask_dollars")),
                )
                mids.append(mid if mid is not None else 0.0)
            fair = dict(zip((s.get("ticker") for s in siblings), devig_probs(mids), strict=True))
            model_prob = fair.get(market.get("ticker"))
            if model_prob is not None:
                method = "devig"
                rationale = "Fair probability after removing vig from mutually exclusive outcomes."

        if model_prob is None:
            model_prob = market_prob

        edge, side = _executable_edge(model_prob, yes_bid, yes_ask)
        spread = None
        if yes_bid is not None and yes_ask is not None:
            spread = max(0.0, yes_ask - yes_bid)
        confidence = confidence_from_spread(spread, volume_24h, method != "market_mid")
        pred = Prediction(
            section=section,
            section_label=SECTION_LABELS[section],
            series_ticker=series.get("ticker") or "",
            event_ticker=event.get("event_ticker") or "",
            event_title=event.get("title") or market.get("title") or "",
            market_ticker=market.get("ticker") or "",
            market_title=market.get("title") or "",
            subtitle=market.get("yes_sub_title") or event.get("sub_title") or "",
            status=str(market.get("status") or ""),
            close_time=market.get("close_time"),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            market_prob=market_prob,
            model_prob=model_prob,
            edge=edge,
            side=side,
            confidence=confidence,
            volume_24h=volume_24h,
            volume=volume,
            liquidity=liquidity,
            method=method,
            rationale=rationale,
            spot=spot,
            asset=asset.display if asset else None,
        )
        pred.score = _score(pred)
        return pred

    def _sports_mutex(
        self,
        section: str,
        series: dict[str, Any],
        event: dict[str, Any],
        markets: list[dict[str, Any]],
    ) -> list[Prediction]:
        parsed: list[tuple[dict[str, Any], float | None, float | None, float]] = []
        for market in markets:
            bid = parse_dollars(market.get("yes_bid_dollars"))
            ask = parse_dollars(market.get("yes_ask_dollars"))
            mid = mid_price(bid, ask)
            if mid is None:
                continue
            parsed.append((market, bid, ask, mid))
        if len(parsed) < 2:
            return [
                pred
                for market, _, _, _ in parsed
                if (pred := self._predict_market(section, series, event, market, {}, markets))
            ]
        fair_vals = devig_probs([mid for _, _, _, mid in parsed])
        preds: list[Prediction] = []
        for (market, bid, ask, mid), fair in zip(parsed, fair_vals, strict=True):
            volume_24h = parse_dollars(market.get("volume_24h_fp")) or 0.0
            edge, side = _executable_edge(fair, bid, ask)
            spread = (ask - bid) if bid is not None and ask is not None else None
            pred = Prediction(
                section=section,
                section_label=SECTION_LABELS[section],
                series_ticker=series.get("ticker") or "",
                event_ticker=event.get("event_ticker") or "",
                event_title=event.get("title") or "",
                market_ticker=market.get("ticker") or "",
                market_title=market.get("title") or "",
                subtitle=market.get("yes_sub_title") or "",
                status=str(market.get("status") or ""),
                close_time=market.get("close_time"),
                yes_bid=bid,
                yes_ask=ask,
                market_prob=mid,
                model_prob=fair,
                edge=edge,
                side=side,
                confidence=confidence_from_spread(spread, volume_24h, True),
                volume_24h=volume_24h,
                volume=parse_dollars(market.get("volume_fp")) or 0.0,
                liquidity=parse_dollars(market.get("liquidity_dollars")) or 0.0,
                method="devig",
                rationale="Sports moneyline de-vigged so mutually exclusive prices sum to 100%.",
            )
            pred.score = _score(pred)
            preds.append(pred)
        preds.sort(key=lambda p: p.score, reverse=True)
        return preds[: self.cfg.max_markets_per_event]
