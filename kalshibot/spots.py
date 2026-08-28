from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Iterable

import httpx

from kalshibot.assets import Asset
from kalshibot.models import QUIET_HOUR_VOL, HOURS_PER_YEAR

logger = logging.getLogger(__name__)


class SpotService:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._cache: dict[str, float] = {}
        self._vol_cache: dict[str, float] = {}

    def clear(self) -> None:
        self._cache.clear()
        self._vol_cache.clear()

    async def prices_for(self, assets: Iterable[Asset]) -> dict[str, float]:
        unique = {asset.key: asset for asset in assets}
        out: dict[str, float] = {}
        for asset in unique.values():
            price = await self._price(asset)
            if price is not None and price > 0:
                out[asset.key] = price
        return out

    async def hourly_vol(self, asset: Asset) -> float:
        if asset.key in self._vol_cache:
            return self._vol_cache[asset.key]
        vol = None
        if asset.coinbase:
            vol = await self._coinbase_realized(asset.coinbase)
        if vol is None:
            vol = await self._yahoo_realized(asset.yahoo)
        if vol is None:
            vol = QUIET_HOUR_VOL.get(asset.key) or (asset.annual_vol / math.sqrt(HOURS_PER_YEAR))
        vol = min(0.05, max(0.001, vol))
        self._vol_cache[asset.key] = vol
        return vol

    async def _price(self, asset: Asset) -> float | None:
        if asset.key in self._cache:
            return self._cache[asset.key]
        if asset.coinbase:
            price = await self._coinbase(asset.coinbase)
            if price is not None:
                self._cache[asset.key] = price
                return price
        price = await self._yahoo(asset.yahoo)
        if price is not None:
            self._cache[asset.key] = price
        return price

    async def _coinbase(self, product: str) -> float | None:
        url = f"https://api.coinbase.com/v2/prices/{product}/spot"
        try:
            response = await self._client.get(url, timeout=15.0)
            response.raise_for_status()
            amount = response.json().get("data", {}).get("amount")
            return float(amount)
        except Exception as exc:  # noqa: BLE001 — network fallback is expected
            logger.debug("Coinbase spot failed for %s: %s", product, exc)
            return None

    async def _yahoo(self, symbol: str) -> float | None:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        try:
            response = await self._client.get(
                url,
                params={"interval": "1m", "range": "1d"},
                timeout=15.0,
                headers={"User-Agent": "Mozilla/5.0 KalshiBot/0.1"},
            )
            response.raise_for_status()
            result = (response.json().get("chart") or {}).get("result") or []
            if not result:
                return None
            meta = result[0].get("meta") or {}
            price = meta.get("regularMarketPrice") or meta.get("previousClose")
            return float(price) if price is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Yahoo spot failed for %s: %s", symbol, exc)
            return None

    async def _coinbase_realized(self, product: str) -> float | None:
        end = int(time.time())
        start = end - 4 * 3600
        url = f"https://api.exchange.coinbase.com/products/{product}/candles"
        try:
            response = await self._client.get(
                url,
                params={"granularity": 60, "start": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(), "end": datetime.fromtimestamp(end, tz=timezone.utc).isoformat()},
                timeout=15.0,
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                return None
            closes = [float(r[4]) for r in reversed(rows) if r and len(r) > 4 and r[4]]
            return hourly_vol_from_closes(closes, 60)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Coinbase candles failed for %s: %s", product, exc)
            return None

    async def _yahoo_realized(self, symbol: str) -> float | None:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        try:
            response = await self._client.get(
                url,
                params={"interval": "1m", "range": "1d"},
                timeout=15.0,
                headers={"User-Agent": "Mozilla/5.0 KalshiBot/0.1"},
            )
            response.raise_for_status()
            result = (response.json().get("chart") or {}).get("result") or []
            if not result:
                return None
            quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
            raw = [c for c in (quote.get("close") or []) if c]
            closes = [float(c) for c in raw[-240:]]
            return hourly_vol_from_closes(closes, 60)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Yahoo realized vol failed for %s: %s", symbol, exc)
            return None


def hourly_vol_from_closes(closes: list[float], seconds_per_bar: int) -> float | None:
    rets: list[float] = []
    for prev, nxt in zip(closes, closes[1:]):
        if prev > 0 and nxt > 0:
            rets.append(math.log(nxt / prev))
    if len(rets) < 20:
        return None
    mean = sum(rets) / len(rets)
    var = sum((item - mean) ** 2 for item in rets) / (len(rets) - 1)
    std = math.sqrt(max(var, 0.0))
    bars_per_hour = 3600 / max(seconds_per_bar, 1)
    return std * math.sqrt(bars_per_hour)
