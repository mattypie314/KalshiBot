"""RSA-PSS signed Kalshi client. Public GETs work without keys."""

from __future__ import annotations

import base64
import logging
import os
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)


class RateLimitedError(Exception):
    pass


class AuthConfigError(Exception):
    pass


class ForbiddenError(Exception):
    """Kalshi 403 — often cloud IPs. Do not retry-storm."""


def unwrap_order(data: Any) -> dict[str, Any]:
    """Kalshi sometimes nests the order under `order`."""
    if not isinstance(data, dict):
        return {}
    inner = data.get("order")
    if isinstance(inner, dict) and data.get("order_id") in (None, ""):
        return inner
    return data


def _expand_path(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path or ""))).resolve()


def load_private_key(path: str):
    pem = _expand_path(path).read_bytes()
    return serialization.load_pem_private_key(pem, password=None)


def sign_path_from_url(base_url: str, path: str) -> str:
    parsed = urlparse(base_url)
    prefix = parsed.path.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return f"{prefix}{path.split('?', 1)[0]}"


def signed_headers(key_id: str, private_key, method: str, sign_path: str) -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method.upper()}{sign_path}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
    }


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 20.0,
        api_key_id: str = "",
        private_key_path: str = "",
        trading_base_url: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.trading_base_url = (trading_base_url or base_url).rstrip("/")
        self._owns = client is None
        self._http = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "KalshiHourly/0.1 (+https://github.com/mattypie314/KalshiBot)"},
        )
        self.api_key_id = (api_key_id or "").strip().strip('"').strip("'")
        self._private_key_path = private_key_path
        self._private_key = (
            load_private_key(private_key_path) if self.api_key_id and private_key_path else None
        )
        self.read_only = False

    @property
    def can_trade(self) -> bool:
        return bool(self.api_key_id and self._private_key) and not self.read_only

    def close(self) -> None:
        if self._owns:
            self._http.close()

    def _auth_headers(self, method: str, path: str, base_url: str) -> dict[str, str]:
        if not self._private_key:
            return {}
        return signed_headers(self.api_key_id, self._private_key, method, sign_path_from_url(base_url, path))

    def _request(
        self,
        method: str,
        path: str,
        *,
        base_url: str | None = None,
        auth: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        root = (base_url or self.base_url).rstrip("/")
        url = f"{root}{path}"
        headers = dict(kwargs.pop("headers", {}) or {})
        if auth:
            headers.update(self._auth_headers(method, path, root))
        last_error: Exception | None = None
        for attempt in range(4):
            response = self._http.request(method, url, headers=headers, **kwargs)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 0.6 * (2**attempt)
                except ValueError:
                    delay = 0.6 * (2**attempt)
                delay = min(delay, 12.0) + random.random() * 0.25
                logger.warning("Kalshi 429 on %s %s; backoff %.1fs", method, path, delay)
                time.sleep(delay)
                last_error = RateLimitedError(f"429 on {method} {path}")
                continue
            if response.status_code == 403:
                body = (response.text or "").strip().replace("\n", " ")[:240]
                logger.error("Kalshi 403 on %s %s — %s. Not retry-storming.", method, path, body)
                raise ForbiddenError(f"403 on {method} {path}: {body}")
            if 500 <= response.status_code < 600:
                time.sleep(0.4 * (2**attempt))
                last_error = httpx.HTTPStatusError(
                    f"{response.status_code} on {path}",
                    request=response.request,
                    response=response,
                )
                continue
            if response.status_code >= 400:
                body = (response.text or "").strip().replace("\n", " ")[:400]
                raise httpx.HTTPStatusError(
                    f"{response.status_code} on {path}: {body or '(empty)'}",
                    request=response.request,
                    response=response,
                )
            return response
        if isinstance(last_error, RateLimitedError):
            raise last_error
        raise last_error or RateLimitedError("request failed")

    def get_json(self, path: str, params: dict[str, Any] | None = None, *, auth: bool = False) -> dict[str, Any]:
        base = self.trading_base_url if auth else None
        return self._request("GET", path, params=params, auth=auth, base_url=base).json()

    def post_json(self, path: str, payload: dict[str, Any], *, auth: bool = True) -> dict[str, Any]:
        if auth and not self.can_trade:
            raise AuthConfigError("signed client required for POST")
        response = self._request("POST", path, base_url=self.trading_base_url, auth=auth, json=payload)
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def delete(self, path: str, params: dict[str, Any] | None = None) -> None:
        if not self.can_trade:
            raise AuthConfigError("signed client required for DELETE")
        self._request("DELETE", path, base_url=self.trading_base_url, auth=True, params=params)

    def open_events(self, series_ticker: str, limit: int = 20) -> list[dict[str, Any]]:
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
            try:
                data = self.get_json("/events", params=params, auth=False)
            except ForbiddenError:
                # Public market-data path; one retry without extra headers is enough.
                raise
            batch = list(data.get("events") or [])
            events.extend(batch)
            cursor = data.get("cursor")
            if not batch or not cursor:
                break
        return events[:limit]

    def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Place a limit order. Prefer the events route; fall back to /portfolio/orders."""
        body = dict(payload)
        body.setdefault("exchange_index", -1)
        try:
            return unwrap_order(self.post_json("/portfolio/events/orders", body))
        except ForbiddenError:
            self.read_only = True
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                body.pop("exchange_index", None)
                try:
                    return unwrap_order(self.post_json("/portfolio/events/orders", body))
                except httpx.HTTPStatusError:
                    return unwrap_order(self.post_json("/portfolio/orders", payload))
            raise

    def create_order_v2(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.create_order(payload)

    def cancel_order(self, order_id: str, ticker: str | None = None) -> None:
        params: dict[str, Any] = {"exchange_index": -1}
        if ticker:
            params["market_ticker"] = ticker
        try:
            self.delete(f"/portfolio/events/orders/{order_id}", params=params)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                self.delete(f"/portfolio/orders/{order_id}")
                return
            raise

    def _paged_orders(self, params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(rows) < limit:
            page = dict(params)
            page["limit"] = min(200, limit - len(rows))
            if cursor:
                page["cursor"] = cursor
            data = self.get_json("/portfolio/orders", params=page, auth=True)
            batch = [unwrap_order(row) if isinstance(row, dict) else row for row in (data.get("orders") or [])]
            rows.extend(batch)
            cursor = data.get("cursor") or None
            if not batch or not cursor:
                break
        return rows[:limit]

    def get_orders(self, *, status: str = "resting", limit: int = 200) -> list[dict[str, Any]]:
        """List resting orders across shards.

        A default GET /portfolio/orders often returns only one shard. Crypto
        (KXBTCD / KXETHD) lives on exchange_index 2, so a nonempty default
        page is not enough — keep scanning 2/0/1 and merge.
        """
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        found: dict[str, dict[str, Any]] = {}
        for extra in ({}, {"exchange_index": 2}, {"exchange_index": 0}, {"exchange_index": 1}):
            try:
                rows = self._paged_orders({**params, **extra}, limit)
            except ForbiddenError:
                self.read_only = True
                if found:
                    break
                return []
            except httpx.HTTPStatusError:
                continue
            for row in rows:
                key = str(row.get("order_id") or row.get("ticker") or "")
                if key:
                    found[key] = row
        return list(found.values())[:limit]

    def get_fills(self, *, limit: int = 100) -> list[dict[str, Any]]:
        try:
            data = self.get_json("/portfolio/fills", params={"limit": min(200, limit)}, auth=True)
        except ForbiddenError:
            self.read_only = True
            return []
        return list(data.get("fills") or [])

    def get_cf_values(self, index_id: str) -> dict[str, Any]:
        """Kalshi CF Benchmarks passthrough. Requires a signed live key."""
        return self.get_json("/cfbenchmarks/values", params={"id": index_id}, auth=True)

    def get_cf_history(
        self,
        index_id: str,
        *,
        timestamp: str,
        timespan: str = "HOUR",
    ) -> dict[str, Any]:
        """Historical BRTI / ETHUSD_RTI ticks via Kalshi's CF Benchmarks passthrough."""
        return self.get_json(
            "/cfbenchmarks/history/values",
            params={"id": index_id, "timespan": timespan, "timestamp": timestamp},
            auth=True,
        )

    def get_market(self, ticker: str) -> dict[str, Any]:
        """Fresh public quote for one ticker. Used to requote before a live post."""
        data = self.get_json(f"/markets/{ticker}", auth=False)
        if isinstance(data, dict) and isinstance(data.get("market"), dict):
            return data["market"]
        return data if isinstance(data, dict) else {}

    def get_balance(self) -> dict[str, Any]:
        return self.get_json("/portfolio/balance", auth=True)

    def auth_status(self) -> dict[str, Any]:
        pem = _expand_path(self._private_key_path) if self._private_key_path else None
        head = ""
        if pem and pem.is_file():
            head = pem.read_text(errors="replace").splitlines()[0] if pem.stat().st_size else ""
        return {
            "can_trade": self.can_trade,
            "key_id_set": bool(self.api_key_id),
            "key_id_len": len(self.api_key_id),
            "pem_path": str(pem) if pem else "",
            "pem_exists": bool(pem and pem.is_file()),
            "pem_looks_private": "PRIVATE KEY" in head,
            "trading_host": self.trading_base_url,
        }
