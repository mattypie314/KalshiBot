from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        timeout: float,
        client: httpx.AsyncClient | None = None,
        min_interval: float = 0.3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "KalshiBot/0.1 (+https://github.com/mkubit85/KalshiBot)"},
        )
        self._min_interval = min_interval
        self._gate = asyncio.Lock()
        self._next_ok = 0.0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _pace(self) -> None:
        async with self._gate:
            now = asyncio.get_running_loop().time()
            wait = self._next_ok - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_ok = asyncio.get_running_loop().time() + self._min_interval

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(6):
            await self._pace()
            response = await self._client.get(url, params=params)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 0.5 * (2**attempt)
                except ValueError:
                    delay = 0.5 * (2**attempt)
                await asyncio.sleep(min(delay, 10.0) + random.random() * 0.25)
                last_error = httpx.HTTPStatusError("429", request=response.request, response=response)
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if 500 <= response.status_code < 600:
                    await asyncio.sleep(0.4 * (2**attempt))
                    continue
                raise
            return response.json()
        assert last_error is not None
        raise last_error

    async def series_for_category(self, category: str) -> list[dict[str, Any]]:
        data = await self.get_json("/series", params={"category": category, "include_volume": "true"})
        return list(data.get("series") or [])

    async def open_events(self, series_ticker: str, limit: int) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(events) < limit:
            params: dict[str, Any] = {
                "series_ticker": series_ticker,
                "status": "open",
                "with_nested_markets": "true",
                "limit": min(200, limit - len(events)),
            }
            if cursor:
                params["cursor"] = cursor
            data = await self.get_json("/events", params=params)
            batch = list(data.get("events") or [])
            events.extend(batch)
            cursor = data.get("cursor")
            if not batch or not cursor:
                break
        return events[:limit]
