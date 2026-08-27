from __future__ import annotations

from typing import Any

import httpx


class KalshiClient:
    def __init__(self, base_url: str, timeout: float, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "KalshiBot/0.1 (+https://github.com/mkubit85/KalshiBot)"},
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._client.get(f"{self.base_url}{path}", params=params)
        response.raise_for_status()
        return response.json()

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
