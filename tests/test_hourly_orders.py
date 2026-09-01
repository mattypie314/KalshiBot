"""Hourly client must see crypto-shard rests, not just the default order page."""

import asyncio

import httpx

from kalshibot.kalshi import KalshiClient as CampaignClient
from src.kalshi_client import KalshiClient, unwrap_order


def test_unwrap_order_reads_nested_payload():
    assert unwrap_order({"order": {"order_id": "n1", "remaining_count": "11.00"}})["order_id"] == "n1"
    assert unwrap_order({"order_id": "flat"})["order_id"] == "flat"


def test_hourly_get_orders_merges_crypto_shard_when_default_page_has_rows():
    def handler(request: httpx.Request) -> httpx.Response:
        idx = request.url.params.get("exchange_index")
        if idx == "2":
            return httpx.Response(
                200,
                json={
                    "orders": [
                        {
                            "order_id": "01a05e91-8fe8-7bf5-a82a-137077be3d4e",
                            "ticker": "KXETHD-26SEP0117-T2399.99",
                        }
                    ]
                },
            )
        if idx in {"0", "1"}:
            return httpx.Response(200, json={"orders": []})
        return httpx.Response(
            200,
            json={
                "orders": [
                    {"order_id": "01a04101-4770-71f5-b74b-86c14e3ef01f", "ticker": "KXOTHER-1"},
                ]
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = KalshiClient("https://external-api.kalshi.com/trade-api/v2", client=http)
    try:
        orders = client.get_orders(status="resting")
    finally:
        client.close()
    ids = {row["order_id"] for row in orders}
    assert "01a05e91-8fe8-7bf5-a82a-137077be3d4e" in ids
    assert "01a04101-4770-71f5-b74b-86c14e3ef01f" in ids


def test_campaign_get_orders_keeps_scanning_after_default_page():
    def handler(request: httpx.Request) -> httpx.Response:
        idx = request.url.params.get("exchange_index")
        if idx == "2":
            return httpx.Response(
                200,
                json={"orders": [{"order_id": "crypto-1", "ticker": "KXETHD-1"}]},
            )
        if idx in {"0", "1"}:
            return httpx.Response(200, json={"orders": []})
        return httpx.Response(
            200,
            json={"orders": [{"order_id": "other-1", "ticker": "KXOTHER-1"}]},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = CampaignClient(
        "https://external-api.kalshi.com/trade-api/v2",
        5.0,
        client=http,
        min_interval=0,
    )
    orders = asyncio.run(client.get_orders(status="resting"))
    asyncio.run(http.aclose())
    ids = {row["order_id"] for row in orders}
    assert ids == {"crypto-1", "other-1"}
