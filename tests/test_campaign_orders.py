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


def _engine(tmp_path, bankroll=35.0):
    path = tmp_path / "crypto-campaign.json"
    http = httpx.AsyncClient()
    engine = CampaignEngine(
        cfg=Settings(tracker_path=str(path), kalshi_live=False, kalshi_min_interval=0, campaign_bankroll=bankroll),
        client=http,
    )
    engine.tracker.load()
    engine.tracker.state["bankroll"] = bankroll
    return engine


def test_enter_limit_posts_maker_not_taker(tmp_path):
    from kalshibot.assets import asset_by_key
    from kalshibot.campaign.playbook import evaluate_idea

    engine = _engine(tmp_path, 35)
    idea = evaluate_idea(
        yes_bid=0.49,
        yes_ask=0.51,
        model_yes=0.72,
        sigma=0.4,
        secs_left=600,
        equity=35.0,
    )
    assert idea.sit_out is None
    pick = {
        "series": "KXBTC15M",
        "event": {"title": "BTC 15m"},
        "market": {"ticker": "KXBTC15M-TEST", "title": "BTC above"},
        "asset": asset_by_key("BTC"),
        "spot": 100.0,
        "strike": 99.5,
        "spec_kind": "greater",
        "cap": None,
        "secs": 600,
        "close": None,
        "hourly_vol": 0.0045,
        "model_yes": 0.72,
        "sigma": 0.4,
        "idea": idea,
        "yes_bid": 0.49,
        "yes_ask": 0.51,
        "exchange_index": 2,
        "loop": "fifteen",
    }
    engine._load_candidates = AsyncMock(return_value=[{"row": True}])
    engine._score_market = AsyncMock(return_value=pick)
    engine.live = True
    engine.kalshi.create_order_v2 = AsyncMock(
        return_value={"order_id": "ord-limit", "fill_count": 0, "average_fill_price": 0}
    )

    actions = asyncio.run(engine._enter_limit(["KXBTC15M"], "fifteen", skip_last=180))
    asyncio.run(engine.aclose())
    payload = engine.kalshi.create_order_v2.await_args.args[0]
    assert payload["post_only"] is True
    assert payload["time_in_force"] == "good_till_canceled"
    assert payload["self_trade_prevention_type"] == "maker"
    assert payload["price"] == "0.5000"
    assert any("post-only" in a for a in actions)
    rests = [r for r in engine.tracker.state["rests"] if r.get("status") == "open"]
    assert len(rests) == 1
    assert all(t.get("status") != "open" for t in engine.tracker.state["tickets"])


def test_dry_limit_does_not_fake_a_fill(tmp_path):
    from kalshibot.assets import asset_by_key
    from kalshibot.campaign.playbook import evaluate_idea

    engine = _engine(tmp_path, 35)
    idea = evaluate_idea(
        yes_bid=0.49,
        yes_ask=0.51,
        model_yes=0.72,
        sigma=0.4,
        secs_left=600,
        equity=35.0,
    )
    pick = {
        "series": "KXBTC15M",
        "event": {"title": "BTC 15m"},
        "market": {"ticker": "KXBTC15M-TEST", "title": "BTC above"},
        "asset": asset_by_key("BTC"),
        "spot": 100.0,
        "strike": 99.5,
        "spec_kind": "greater",
        "cap": None,
        "secs": 600,
        "close": None,
        "hourly_vol": 0.0045,
        "model_yes": 0.72,
        "sigma": 0.4,
        "idea": idea,
        "yes_bid": 0.49,
        "yes_ask": 0.51,
        "exchange_index": 2,
    }
    engine._load_candidates = AsyncMock(return_value=[{"row": True}])
    engine._score_market = AsyncMock(return_value=pick)
    engine.kalshi.create_order_v2 = AsyncMock(side_effect=AssertionError("dry run must not order"))
    actions = asyncio.run(engine._enter_limit(["KXBTC15M"], "fifteen", skip_last=180))
    asyncio.run(engine.aclose())
    assert engine.kalshi.create_order_v2.await_count == 0
    assert any("DRY post-only" in a for a in actions)
    assert all(t.get("status") != "open" for t in engine.tracker.state["tickets"])


def test_revenge_sit_out_after_a_loss(tmp_path):
    from datetime import datetime, timezone

    engine = _engine(tmp_path, 35)
    engine.tracker.state["last_loss_at"] = datetime.now(timezone.utc).isoformat()
    engine.kalshi.series_for_category = AsyncMock(return_value=[])
    engine.kalshi.open_events = AsyncMock(side_effect=AssertionError("revenge must not scan"))
    engine.kalshi.create_order_v2 = AsyncMock(side_effect=AssertionError("revenge must not order"))
    result = asyncio.run(engine.fire("fifteen"))
    asyncio.run(engine.aclose())
    assert any("revenge" in a.lower() for a in result["actions"])


def test_no_side_sizes_against_the_no_cost(tmp_path):
    from kalshibot.assets import asset_by_key
    from kalshibot.campaign.playbook import evaluate_idea

    engine = _engine(tmp_path, 35)
    idea = evaluate_idea(
        yes_bid=0.79,
        yes_ask=0.81,
        model_yes=0.20,
        sigma=1.2,
        secs_left=600,
        equity=35.0,
    )
    assert idea.sit_out is None
    assert idea.side == "no"
    pick = {
        "series": "KXBTC15M",
        "event": {"title": "BTC 15m"},
        "market": {"ticker": "KXBTC15M-NO", "title": "BTC above"},
        "asset": asset_by_key("BTC"),
        "spot": 100.0,
        "strike": 110.0,
        "spec_kind": "greater",
        "cap": None,
        "secs": 600,
        "close": None,
        "hourly_vol": 0.0045,
        "model_yes": 0.20,
        "sigma": 1.2,
        "idea": idea,
        "yes_bid": 0.79,
        "yes_ask": 0.81,
        "exchange_index": 2,
    }
    engine._load_candidates = AsyncMock(return_value=[{"row": True}])
    engine._score_market = AsyncMock(return_value=pick)
    engine.live = True
    engine.kalshi.create_order_v2 = AsyncMock(
        return_value={"order_id": "ord-no", "fill_count": 0, "average_fill_price": 0}
    )
    asyncio.run(engine._enter_limit(["KXBTC15M"], "fifteen", skip_last=180))
    asyncio.run(engine.aclose())
    payload = engine.kalshi.create_order_v2.await_args.args[0]
    assert payload["side"] == "ask"
    assert payload["price"] == "0.8000"
    count = float(payload["count"])
    no_cost = 0.20
    assert count * no_cost <= 0.10 * 35 + 1e-9
    assert count * no_cost >= 0.25


def test_set_bankroll_keeps_realized(tmp_path):
    from kalshibot.campaign.tracker import Tracker

    path = tmp_path / "crypto-campaign.json"
    tracker = Tracker(path, 15.0)
    tracker.load()
    tracker.state["realized"] = -1.25
    tracker.set_bankroll(35.0)
    tracker.save()
    again = Tracker(path, 15.0)
    state = again.load()
    assert state["bankroll"] == 35.0
    assert state["realized"] == -1.25

