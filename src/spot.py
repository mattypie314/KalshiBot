"""BTC/ETH last price and 1h realized vol. Proxy only — not Kalshi settlement."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
import httpx

logger = logging.getLogger(__name__)

BINANCE_SYMBOL = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
COINBASE_PRODUCT = {"BTC": "BTC-USD", "ETH": "ETH-USD"}
FALLBACK_VOL = {"BTC": 0.004, "ETH": 0.005}


@dataclass
class SpotSnapshot:
    prices: dict[str, float] = field(default_factory=dict)
    hourly_vol: dict[str, float] = field(default_factory=dict)
    source: str = "unknown"
    vol_source: dict[str, str] = field(default_factory=dict)
    note: str = (
        "Spot/vol are exchange proxies, not the official Kalshi settlement index "
        "(typically CF Benchmarks RTI)."
    )


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


class SpotService:
    def __init__(self, http: httpx.Client | None = None, preferred: str = "coinbase") -> None:
        self._owns = http is None
        self._http = http or httpx.Client(timeout=15.0, headers={"User-Agent": "KalshiHourly/0.1"})
        self.preferred = preferred.lower()

    def close(self) -> None:
        if self._owns:
            self._http.close()

    def snapshot(
        self,
        assets: list[str],
        fallbacks: dict[str, float] | None = None,
    ) -> SpotSnapshot:
        fallbacks = fallbacks or dict(FALLBACK_VOL)
        snap = SpotSnapshot()
        for asset in assets:
            price, src = self._price(asset)
            if price:
                snap.prices[asset] = price
                snap.source = src
            vol, vol_src = self._vol(asset, fallbacks.get(asset, FALLBACK_VOL.get(asset, 0.004)))
            snap.hourly_vol[asset] = vol
            snap.vol_source[asset] = vol_src
        return snap

    def _price(self, asset: str) -> tuple[float | None, str]:
        order = [self.preferred, "coinbase", "binance"]
        seen: set[str] = set()
        for name in order:
            if name in seen:
                continue
            seen.add(name)
            try:
                if name == "binance":
                    price = self._binance_price(asset)
                else:
                    price = self._coinbase_price(asset)
            except Exception as exc:  # noqa: BLE001
                text = str(exc)
                if "451" in text:
                    logger.debug("%s spot blocked (451) for %s; trying next source", name, asset)
                else:
                    logger.info("%s spot failed for %s: %s", name, asset, exc)
                continue
            if price and price > 0:
                return price, name
        return None, "none"

    def _vol(self, asset: str, fallback: float) -> tuple[float, str]:
        order = [self.preferred, "coinbase", "binance"]
        seen: set[str] = set()
        for name in order:
            if name in seen:
                continue
            seen.add(name)
            try:
                if name == "binance":
                    vol = self._binance_vol(asset)
                else:
                    vol = self._coinbase_vol(asset)
            except Exception as exc:  # noqa: BLE001
                text = str(exc)
                if "451" in text:
                    logger.debug("%s vol blocked (451) for %s; trying next source", name, asset)
                else:
                    logger.info("%s vol failed for %s: %s", name, asset, exc)
                continue
            if vol is not None:
                return min(0.05, max(0.001, vol)), f"{name}-realized"
        return fallback, "fallback"

    def _binance_price(self, asset: str) -> float | None:
        symbol = BINANCE_SYMBOL[asset]
        response = self._http.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": symbol},
        )
        response.raise_for_status()
        return float(response.json()["price"])

    def _binance_vol(self, asset: str) -> float | None:
        symbol = BINANCE_SYMBOL[asset]
        response = self._http.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": 240},
        )
        response.raise_for_status()
        rows = response.json()
        closes = [float(row[4]) for row in rows if row and len(row) > 4]
        return hourly_vol_from_closes(closes, 60)

    def _coinbase_price(self, asset: str) -> float | None:
        product = COINBASE_PRODUCT[asset]
        response = self._http.get(f"https://api.coinbase.com/v2/prices/{product}/spot")
        response.raise_for_status()
        return float(response.json()["data"]["amount"])

    def _coinbase_vol(self, asset: str) -> float | None:
        product = COINBASE_PRODUCT[asset]
        end = int(time.time())
        start = end - 4 * 3600
        response = self._http.get(
            f"https://api.exchange.coinbase.com/products/{product}/candles",
            params={
                "granularity": 60,
                "start": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
                "end": datetime.fromtimestamp(end, tz=timezone.utc).isoformat(),
            },
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            return None
        closes = [float(r[4]) for r in reversed(rows) if r and len(r) > 4 and r[4]]
        return hourly_vol_from_closes(closes, 60)
