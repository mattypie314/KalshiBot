from __future__ import annotations

import logging
from typing import Iterable

import httpx

from kalshibot.assets import Asset

logger = logging.getLogger(__name__)


class SpotService:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._cache: dict[str, float] = {}

    async def prices_for(self, assets: Iterable[Asset]) -> dict[str, float]:
        unique = {asset.key: asset for asset in assets}
        out: dict[str, float] = {}
        for asset in unique.values():
            price = await self._price(asset)
            if price is not None:
                out[asset.key] = price
        return out

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
