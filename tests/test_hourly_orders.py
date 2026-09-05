"""Hourly client must see crypto-shard rests, not just the default order page."""

import httpx

from src.kalshi_client import KalshiClient, sign_path_from_url, unwrap_order


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


def test_get_positions_merges_crypto_shard():
    def handler(request: httpx.Request) -> httpx.Response:
        idx = request.url.params.get("exchange_index")
        if idx == "2":
            return httpx.Response(
                200,
                json={
                    "market_positions": [
                        {"ticker": "KXBTCD-1", "position_fp": "2.00", "exchange_index": 2}
                    ]
                },
            )
        return httpx.Response(200, json={"market_positions": []})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = KalshiClient("https://external-api.kalshi.com/trade-api/v2", client=http)
    try:
        rows = client.get_positions(count_filter="position")
    finally:
        client.close()
    assert any(row["ticker"] == "KXBTCD-1" for row in rows)


def test_sign_path_includes_trade_api_prefix_and_drops_query():
    assert (
        sign_path_from_url("https://demo-api.kalshi.co/trade-api/v2", "/portfolio/balance?x=1")
        == "/trade-api/v2/portfolio/balance"
    )
