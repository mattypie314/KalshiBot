import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from kalshibot.campaign.engine import CampaignEngine
from kalshibot.config import Settings
from kalshibot.kalshi import KalshiClient


def test_default_host_is_external_api(monkeypatch):
    monkeypatch.delenv("KALSHI_BASE_URL", raising=False)
    assert "external-api.kalshi.com" in Settings().kalshi_base_url


def test_create_order_auto_routes_and_retries_on_404():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode()))
        if len(calls) == 1:
            return httpx.Response(404, json={"error": {"message": "unknown shard"}})
        return httpx.Response(
            201,
            json={"order_id": "ord-1", "fill_count": "1.00", "remaining_count": "0.00", "ts_ms": 1},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KalshiClient("https://external-api.kalshi.com/trade-api/v2", 5.0, client=http, min_interval=0)
    result = asyncio.run(
        client.create_order_v2(
            {
                "ticker": "KXBNBD-1",
                "side": "ask",
                "count": "1.00",
                "price": "0.9900",
                "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross",
                "exchange_index": 2,
            }
        )
    )
    asyncio.run(http.aclose())
    assert result["order_id"] == "ord-1"
    assert calls[0]["exchange_index"] == -1
    assert "exchange_index" not in calls[1]


def test_http_error_includes_kalshi_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad price"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KalshiClient("https://external-api.kalshi.com/trade-api/v2", 5.0, client=http, min_interval=0)
    with pytest.raises(httpx.HTTPStatusError) as err:
        asyncio.run(client.post_json("/portfolio/events/orders", {"ticker": "X"}))
    asyncio.run(http.aclose())
    assert "bad price" in str(err.value)


def test_live_fire_drops_practice_tickets_without_ordering(tmp_path):
    path = tmp_path / "crypto-campaign.json"
    http = httpx.AsyncClient()
    engine = CampaignEngine(
        cfg=Settings(tracker_path=str(path), kalshi_live=False, kalshi_min_interval=0),
        client=http,
    )
    engine.tracker.load()
    engine.tracker.state["tickets"].append(
        {
            "id": "1",
            "pot": "hourly",
            "ticker": "KXBNBD-26AUG2803-T524.99",
            "side": "yes",
            "fill": 1.0,
            "count": 4.7,
            "cost": 4.7,
            "status": "open",
            "paper": True,
            "order_id": None,
        }
    )
    engine.tracker.save()
    engine.live = True
    engine.kalshi.create_order_v2 = AsyncMock(side_effect=AssertionError("no live flatten of paper"))
    engine.kalshi.get_json = AsyncMock(side_effect=AssertionError("practice tickets should not be quoted"))
    engine.kalshi.series_for_category = AsyncMock(return_value=[])

    result = asyncio.run(engine.fire("hourly"))
    asyncio.run(engine.aclose())
    assert engine.kalshi.create_order_v2.await_count == 0
    assert any("practice ticket" in a for a in result["actions"])
    assert all(t.get("status") != "open" for t in engine.tracker.state["tickets"])
