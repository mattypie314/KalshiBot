from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from kalshibot.auth import load_private_key, sign_path_from_url, signed_headers


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        timeout: float,
        client: httpx.AsyncClient | None = None,
        min_interval: float = 0.3,
        api_key_id: str = "",
        private_key_path: str = "",
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
        self.api_key_id = api_key_id
        self._private_key = load_private_key(private_key_path) if api_key_id and private_key_path else None

    @property
    def can_trade(self) -> bool:
        return bool(self.api_key_id and self._private_key)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        if not self._private_key:
            return {}
        return signed_headers(self.api_key_id, self._private_key, method, sign_path_from_url(self.base_url, path))

    async def _pace(self) -> None:
        async with self._gate:
            now = asyncio.get_running_loop().time()
            wait = self._next_ok - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_ok = asyncio.get_running_loop().time() + self._min_interval

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self.base_url}{path}"
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update(self._auth_headers(method, path))
        last_error: Exception | None = None
        for attempt in range(6):
            await self._pace()
            response = await self._client.request(method, url, headers=headers, **kwargs)
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
            return response
        assert last_error is not None
        raise last_error

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._request("GET", path, params=params)
        return response.json()

    async def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._request("POST", path, json=payload)
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    async def delete(self, path: str) -> None:
        await self._request("DELETE", path)

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

    async def create_order_v2(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.post_json("/portfolio/events/orders", payload)

    async def cancel_order(self, order_id: str) -> None:
        await self.delete(f"/portfolio/events/orders/{order_id}")
