from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from kalshibot.auth import load_private_key, sign_path_from_url, signed_headers


def _position_qty(row: dict[str, Any]) -> float:
    for key in ("position_fp", "position", "position_count"):
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


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
                body = (response.text or "").strip().replace("\n", " ")[:400]
                last_error = httpx.HTTPStatusError(
                    f"{exc} Kalshi said: {body or '(empty body)'}",
                    request=response.request,
                    response=response,
                )
                if 500 <= response.status_code < 600:
                    await asyncio.sleep(0.4 * (2**attempt))
                    continue
                raise last_error from exc
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

    async def delete(self, path: str, params: dict[str, Any] | None = None) -> None:
        await self._request("DELETE", path, params=params)

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
        """Place a V2 order. Always auto-route by ticker.

        Sending a guessed `exchange_index` (for example crypto shard 2) makes
        Kalshi 404 when the market lives on another shard — that is what the
        GitHub campaign was hitting on leftover BNB daily practice tickets.
        """
        body = dict(payload)
        body["exchange_index"] = -1
        try:
            return await self.post_json("/portfolio/events/orders", body)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                body.pop("exchange_index", None)
                return await self.post_json("/portfolio/events/orders", body)
            raise

    async def cancel_order(self, order_id: str, ticker: str | None = None) -> None:
        """Cancel a V2 order. Auto-route by ticker so crypto (shard 2) is not sent to shard 0."""
        params: dict[str, Any] = {"exchange_index": -1}
        if ticker:
            params["market_ticker"] = ticker
        await self.delete(f"/portfolio/events/orders/{order_id}", params=params)

    async def get_balance(self) -> dict[str, Any]:
        return await self.get_json("/portfolio/balance")

    async def _paged(
        self,
        path: str,
        list_key: str,
        params: dict[str, Any],
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(rows) < limit:
            page = dict(params)
            page["limit"] = min(200, limit - len(rows))
            if cursor:
                page["cursor"] = cursor
            data = await self.get_json(path, params=page)
            batch = list(data.get(list_key) or [])
            if not batch and list_key == "market_positions":
                batch = list(data.get("positions") or [])
            rows.extend(batch)
            cursor = data.get("cursor") or None
            if not batch or not cursor:
                break
        return rows[:limit]

    async def get_orders(self, *, status: str = "resting", limit: int = 200) -> list[dict[str, Any]]:
        """List portfolio orders. Resting orders always come from GET /portfolio/orders."""
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        found: dict[str, dict[str, Any]] = {}
        for extra in ({}, {"exchange_index": 2}, {"exchange_index": 0}, {"exchange_index": 1}):
            try:
                rows = await self._paged("/portfolio/orders", "orders", {**params, **extra}, limit=limit)
            except httpx.HTTPStatusError:
                continue
            for row in rows:
                key = str(row.get("order_id") or row.get("ticker") or "")
                if key:
                    found[key] = row
            # Do not stop after a nonempty default page. Crypto rests live on
            # shard 2 and are omitted from the unscoped list.
        return list(found.values())[:limit]

    async def get_positions(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """List open market positions across shards.

        Do not use count_filter=position. That keys off the legacy `position`
        int, and crypto event contracts often only send `position_fp`, which
        made the Positions tab look empty.
        """
        found: dict[str, dict[str, Any]] = {}

        async def collect(params: dict[str, Any]) -> None:
            cursor: str | None = None
            scanned = 0
            while scanned < 1000 and len(found) < limit:
                page = dict(params)
                page["limit"] = min(200, limit)
                if cursor:
                    page["cursor"] = cursor
                data = await self.get_json("/portfolio/positions", params=page)
                batch = list(data.get("market_positions") or data.get("positions") or [])
                scanned += len(batch)
                for row in batch:
                    ticker = str(row.get("ticker") or row.get("market_ticker") or "")
                    if ticker and _position_qty(row) != 0:
                        found[ticker] = row
                cursor = (data.get("cursor") or "").strip() or None
                if not batch or not cursor:
                    break

        try:
            await collect({"count_filter": "total_traded"})
        except httpx.HTTPStatusError:
            pass
        if not found:
            try:
                await collect({})
            except httpx.HTTPStatusError:
                pass
        if not found:
            for shard in (2, 0, 1):
                try:
                    await collect({"exchange_index": shard})
                except httpx.HTTPStatusError:
                    continue
                if found:
                    break
        return list(found.values())[:limit]

    async def get_fills(self, *, limit: int = 200) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for extra in ({}, {"exchange_index": 2}, {"exchange_index": 0}, {"exchange_index": 1}):
            try:
                rows = await self._paged("/portfolio/fills", "fills", extra, limit=limit)
            except httpx.HTTPStatusError:
                continue
            for row in rows:
                key = str(row.get("fill_id") or row.get("trade_id") or "")
                if key:
                    found[key] = row
            if extra == {} and found:
                break
        return list(found.values())[:limit]
