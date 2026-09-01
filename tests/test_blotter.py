import asyncio
from unittest.mock import AsyncMock

import httpx

from kalshibot.campaign.blotter import map_kalshi_order, map_kalshi_position, positions_from_fills
from kalshibot.campaign.engine import CampaignEngine
from kalshibot.config import Settings
from kalshibot.kalshi import KalshiClient


def test_map_kalshi_position_no_contracts():
    row = map_kalshi_position(
        {
            "ticker": "KXBNB-26SEP0118-B687",
            "position_fp": "-25.00",
            "market_exposure_dollars": "22.25",
        }
    )
    assert row["side"] == "no"
    assert row["count"] == 25.0
    assert row["cost"] == 22.25
    assert row["fill"] == 0.89
    assert row["source"] == "kalshi"


def test_map_kalshi_position_legacy_int_fields():
    row = map_kalshi_position(
        {
            "ticker": "KXETH-1",
            "position_fp": "",
            "position": 6,
            "market_exposure": 318,
        }
    )
    assert row["side"] == "yes"
    assert row["count"] == 6.0
    assert row["cost"] == 3.18


def test_map_kalshi_position_skips_flat():
    assert map_kalshi_position({"ticker": "KXBTC-1", "position_fp": "0.00"}) is None


def test_map_kalshi_order_resting_no():
    row = map_kalshi_order(
        {
            "order_id": "ord-1",
            "ticker": "KXBTC15M-TEST",
            "outcome_side": "no",
            "yes_price_dollars": "0.1100",
            "no_price_dollars": "0.8900",
            "remaining_count_fp": "10.00",
            "status": "resting",
        }
    )
    assert row["side"] == "no"
    assert row["price"] == 0.89
    assert row["count"] == 10.0
    assert row["cost"] == 8.9


def test_positions_from_fills_nets_no_lot():
    rows = positions_from_fills(
        [
            {
                "ticker": "KXBNB-26SEP0118-B687",
                "outcome_side": "no",
                "count_fp": "25.00",
                "yes_price_dollars": "0.1100",
                "no_price_dollars": "0.8900",
            }
        ]
    )
    assert len(rows) == 1
    assert rows[0]["side"] == "no"
    assert rows[0]["count"] == 25.0
    assert rows[0]["cost"] == 22.25


def test_get_positions_drops_flat_and_skips_legacy_filter():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/portfolio/positions")
        assert "count_filter=position" not in str(request.url)
        return httpx.Response(
            200,
            json={
                "market_positions": [
                    {"ticker": "FLAT", "position_fp": "0.00", "market_exposure_dollars": "0"},
                    {
                        "ticker": "KXBNB-26SEP0118-B687",
                        "position_fp": "-25.00",
                        "market_exposure_dollars": "22.25",
                    },
                ],
                "event_positions": [],
                "cursor": "",
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KalshiClient("https://external-api.kalshi.com/trade-api/v2", 5.0, client=http, min_interval=0)
    rows = asyncio.run(client.get_positions())
    asyncio.run(http.aclose())
    assert [row["ticker"] for row in rows] == ["KXBNB-26SEP0118-B687"]


def _engine(tmp_path):
    path = tmp_path / "crypto-campaign.json"
    http = httpx.AsyncClient()
    engine = CampaignEngine(
        cfg=Settings(tracker_path=str(path), kalshi_live=False, kalshi_min_interval=0),
        client=http,
    )
    engine.tracker.load()
    engine.kalshi.api_key_id = "test-key"
    engine.kalshi._private_key = object()
    return engine


def test_map_kalshi_position_pnl_dollars():
    row = map_kalshi_position(
        {
            "ticker": "KXBTC15M-1",
            "position_fp": "4.00",
            "market_exposure_dollars": "2.40",
            "realized_pnl_dollars": "-0.18",
        }
    )
    assert row["pnl"] == -0.18


def test_public_status_uses_kalshi_positions(tmp_path):
    engine = _engine(tmp_path)
    engine.kalshi.get_orders = AsyncMock(return_value=[])
    engine.kalshi.get_positions = AsyncMock(
        return_value=[
            {
                "ticker": "KXBNB-26SEP0118-B687",
                "position_fp": "-25.00",
                "market_exposure_dollars": "22.25",
            }
        ]
    )
    payload = asyncio.run(engine.public_status())
    asyncio.run(engine.aclose())
    assert payload["positions_source"] == "kalshi"
    assert payload["open_tickets"][0]["ticker"] == "KXBNB-26SEP0118-B687"
    assert payload["open_tickets"][0]["side"] == "no"
    assert payload["rests_source"] == "kalshi"
    assert payload["rests"] == []


def test_public_status_keeps_positions_if_orders_fail(tmp_path):
    engine = _engine(tmp_path)
    engine.kalshi.get_orders = AsyncMock(side_effect=RuntimeError("orders down"))
    engine.kalshi.get_positions = AsyncMock(
        return_value=[{"ticker": "KXETH-1", "position_fp": "4.00", "market_exposure_dollars": "2.40"}]
    )
    payload = asyncio.run(engine.public_status())
    asyncio.run(engine.aclose())
    assert payload["positions_source"] == "kalshi"
    assert payload["open_tickets"][0]["ticker"] == "KXETH-1"
    assert payload["rests_source"] == "local"
    assert "orders" in payload["blotter_error"]


def test_public_status_uses_fills_when_positions_empty(tmp_path):
    engine = _engine(tmp_path)
    engine.kalshi.get_orders = AsyncMock(return_value=[])
    engine.kalshi.get_positions = AsyncMock(return_value=[])
    engine.kalshi.get_fills = AsyncMock(
        return_value=[
            {
                "ticker": "KXBNB-26SEP0118-B687",
                "outcome_side": "no",
                "count_fp": "25.00",
                "yes_price_dollars": "0.1100",
                "no_price_dollars": "0.8900",
            }
        ]
    )
    payload = asyncio.run(engine.public_status())
    asyncio.run(engine.aclose())
    assert payload["open_tickets"][0]["ticker"] == "KXBNB-26SEP0118-B687"
    assert payload["open_tickets"][0]["side"] == "no"
    assert payload["open_tickets"][0]["count"] == 25.0


def test_public_status_splits_cash_portfolio_and_updates_pnl(tmp_path):
    engine = _engine(tmp_path)
    engine.kalshi.get_orders = AsyncMock(return_value=[])
    engine.kalshi.get_positions = AsyncMock(
        return_value=[
            {
                "ticker": "KXBTC15M-1",
                "position_fp": "10.00",
                "market_exposure_dollars": "6.00",
                "realized_pnl_dollars": "0.40",
            }
        ]
    )
    engine.kalshi.get_balance = AsyncMock(
        return_value={"balance_dollars": "40.00", "portfolio_value": 4800}
    )
    payload = asyncio.run(engine.public_status())
    asyncio.run(engine.aclose())
    assert payload["kalshi_cash"] == 40.0
    assert payload["kalshi_total_value"] == 48.0
    assert payload["equity"] == 48.0
    assert payload["kalshi_cash"] != payload["equity"]
    assert payload["pnl"] == 2.4
    assert payload["realized"] == 2.4
    assert payload["open_cost"] == 6.0
