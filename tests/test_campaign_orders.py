import asyncio
import json
from unittest.mock import AsyncMock, patch

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
    engine._enter_hourly = AsyncMock(return_value=["No actionable edge."])

    result = asyncio.run(engine.fire("hourly"))
    asyncio.run(engine.aclose())
    assert engine.kalshi.create_order_v2.await_count == 0
    assert any("practice ticket" in a for a in result["actions"])
    assert all(t.get("status") != "open" for t in engine.tracker.state["tickets"])


def test_tracker_live_flag_requires_keys(tmp_path):
    engine = _engine(tmp_path)
    engine.tracker.state.setdefault("sizing", {})["live"] = True
    engine.tracker.save()
    engine.tracker.load()
    assert engine.live is False
    engine.kalshi.api_key_id = "test-key"
    engine.kalshi._private_key = object()
    assert engine.live is True
    engine.tracker.state["sizing"]["live"] = False
    engine.tracker.save()
    engine.tracker.load()
    assert engine.live is False
    asyncio.run(engine.aclose())


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
    from unittest.mock import patch

    engine = _engine(tmp_path, 35)
    engine.tracker.state["last_loss_at"] = datetime.now(timezone.utc).isoformat()
    engine.tracker.save()
    engine._enter_maker = AsyncMock(side_effect=AssertionError("revenge must not order"))
    engine.kalshi.create_order_v2 = AsyncMock(side_effect=AssertionError("revenge must not order"))
    with patch("kalshibot.campaign.engine.in_maker_window", return_value=True):
        result = asyncio.run(engine.fire("maker"))
    asyncio.run(engine.aclose())
    assert any("revenge" in a.lower() for a in result["actions"])
    assert engine._enter_maker.await_count == 0


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


def test_hourly_sits_out_when_net_edge_is_thin(tmp_path):
    from kalshibot.assets import asset_by_key
    from kalshibot.campaign.hourly import NO_EDGE

    engine = _engine(tmp_path, 55)
    pick = {
        "series": "KXBTC15M",
        "event": {"title": "BTC 15m"},
        "market": {"ticker": "KXBTC15M-ATM", "title": "BTC above"},
        "asset": asset_by_key("BTC"),
        "spot": 100.0,
        "strike": 100.0,
        "spec_kind": "greater",
        "cap": None,
        "secs": 600,
        "close": None,
        "hourly_vol": 0.0045,
        "model_yes": 0.52,
        "sigma": 0.1,
        "yes_bid": 0.49,
        "yes_ask": 0.51,
        "exchange_index": 2,
    }
    engine._load_candidates = AsyncMock(return_value=[{"row": True}])
    engine._score_market = AsyncMock(return_value=pick)
    engine.live = True
    engine.kalshi.create_order_v2 = AsyncMock(side_effect=AssertionError("hourly must sit out under 6%"))
    actions = asyncio.run(engine._enter_hourly())
    asyncio.run(engine.aclose())
    assert actions == [NO_EDGE]
    assert engine.kalshi.create_order_v2.await_count == 0


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


def test_equity_follows_kalshi_cash_and_respects_a_cap(tmp_path):
    engine = _engine(tmp_path, 15)
    engine.tracker.state["realized"] = 0
    engine.tracker.state["kalshi_cash"] = 42.0
    engine.tracker.state["sizing"] = {"follow_kalshi_cash": True, "bankroll_cap": None}
    assert engine._equity() == 42.0
    engine.tracker.state["sizing"]["bankroll_cap"] = 30.0
    assert engine._equity() == 30.0
    engine.tracker.state["sizing"]["follow_kalshi_cash"] = False
    engine.tracker.state["bankroll"] = 15.0
    assert engine._equity() == 15.0
    asyncio.run(engine.aclose())


def test_sync_kalshi_cash_when_live(tmp_path):
    engine = _engine(tmp_path, 15)
    engine.live = True
    engine.kalshi.api_key_id = "x"
    engine.kalshi._private_key = object()
    engine.kalshi.get_balance = AsyncMock(return_value={"balance_dollars": "48.25"})
    msg = asyncio.run(engine._sync_kalshi_cash())
    asyncio.run(engine.aclose())
    assert engine.tracker.state["kalshi_cash"] == 48.25
    assert "48.25" in (msg or "")


def test_cash_sync_does_not_abort_the_loop(tmp_path):
    engine = _engine(tmp_path, 15)
    engine.live = True
    engine.kalshi.api_key_id = "x"
    engine.kalshi._private_key = object()
    engine.kalshi.get_balance = AsyncMock(return_value={"balance_dollars": "40.00"})
    engine._enter_hourly = AsyncMock(return_value=["No actionable edge."])
    engine.kalshi.create_order_v2 = AsyncMock(side_effect=AssertionError("should sit out, not order"))
    result = asyncio.run(engine.fire("hourly"))
    asyncio.run(engine.aclose())
    joined = " ".join(result["actions"])
    assert "Maker window closed" not in joined
    assert "40.00" in joined


def _maker_pick(**kwargs):
    from kalshibot.assets import asset_by_key
    from kalshibot.campaign.playbook import evaluate_idea

    idea = evaluate_idea(
        yes_bid=0.80,
        yes_ask=0.82,
        model_yes=0.83,
        sigma=0.2,
        secs_left=90,
        equity=35.0,
    )
    pick = {
        "series": "KXBTC15M",
        "event": {"title": "BTC 15m"},
        "market": {"ticker": "KXBTC15M-M", "title": "BTC above"},
        "asset": asset_by_key("BTC"),
        "spot": 100.0,
        "strike": 99.5,
        "spec_kind": "greater",
        "cap": None,
        "secs": 90,
        "close": None,
        "hourly_vol": 0.0045,
        "model_yes": 0.83,
        "sigma": 0.2,
        "idea": idea,
        "yes_bid": 0.80,
        "yes_ask": 0.82,
        "exchange_index": 2,
    }
    pick.update(kwargs)
    return pick


def test_maker_rests_74_to_93_in_last_three_minutes(tmp_path):
    engine = _engine(tmp_path, 35)
    engine._load_candidates = AsyncMock(return_value=[{"row": True}])
    engine._score_market = AsyncMock(return_value=_maker_pick())
    engine.kalshi.create_order_v2 = AsyncMock(side_effect=AssertionError("dry must not order"))
    actions = asyncio.run(engine._enter_maker())
    asyncio.run(engine.aclose())
    assert any("maker rest" in a and "74–93" in a for a in actions)
    rests = [r for r in engine.tracker.state["rests"] if r.get("status") == "open"]
    assert len(rests) == 1
    assert rests[0]["kind"] == "maker_spread"
    assert 0.74 <= rests[0]["price"] <= 0.93
    assert engine.kalshi.create_order_v2.await_count == 0


def test_maker_skips_when_more_than_three_minutes_left(tmp_path):
    engine = _engine(tmp_path, 35)
    engine._load_candidates = AsyncMock(return_value=[{"row": True}])
    engine._score_market = AsyncMock(return_value=_maker_pick(secs=400))
    engine.kalshi.create_order_v2 = AsyncMock(side_effect=AssertionError("too early to rest"))
    actions = asyncio.run(engine._enter_maker())
    asyncio.run(engine.aclose())
    assert engine.kalshi.create_order_v2.await_count == 0
    assert any("last 3 minutes" in a for a in actions)


def test_live_maker_is_post_only_never_ioc(tmp_path):
    engine = _engine(tmp_path, 35)
    engine._load_candidates = AsyncMock(return_value=[{"row": True}])
    engine._score_market = AsyncMock(return_value=_maker_pick())
    engine.live = True
    engine.kalshi.create_order_v2 = AsyncMock(
        return_value={"order_id": "m1", "fill_count": 0, "average_fill_price": 0}
    )
    asyncio.run(engine._enter_maker())
    asyncio.run(engine.aclose())
    payload = engine.kalshi.create_order_v2.await_args.args[0]
    assert payload["post_only"] is True
    assert payload["time_in_force"] == "good_till_canceled"
    assert payload["self_trade_prevention_type"] == "maker"
    assert payload["side"] == "bid"
    assert payload["price"] == "0.8000"


def test_halted_cancels_rests_and_skips_new_tickets(tmp_path):
    engine = _engine(tmp_path, 35)
    engine.tracker.state["sizing"] = {"halted": True, "maker_auto": True, "follow_kalshi_cash": True}
    engine.tracker.state["rests"] = [
        {
            "id": "r1",
            "loop": "hourly",
            "ticker": "KXBNB-26AUG2900-B692",
            "side": "no",
            "status": "open",
            "order_id": "ord-halt",
            "paper": False,
        }
    ]
    engine.tracker.save()
    engine.live = True
    engine.kalshi.cancel_order = AsyncMock()
    engine._enter_hourly = AsyncMock(side_effect=AssertionError("halted must not enter"))
    engine._enter_limit = AsyncMock(side_effect=AssertionError("halted must not enter"))
    engine._fifteen_gate_and_enter = AsyncMock(side_effect=AssertionError("halted must not enter"))
    engine._quotes_for_open_tickets = AsyncMock(return_value={})
    engine._manage_open = AsyncMock(return_value=[])
    result = asyncio.run(engine.fire("hourly"))
    asyncio.run(engine.aclose())
    engine.kalshi.cancel_order.assert_awaited_once()
    assert engine._enter_hourly.await_count == 0
    assert engine._enter_limit.await_count == 0
    assert any("halted until further notice" in a for a in result["actions"])
    assert result["status"]["halted"] is True
    assert engine.tracker.state["rests"][0]["status"] == "canceled"
    assert engine.tracker.state["rests"][0]["exit_reason"] == "halted"


def test_maker_auto_off_skips_new_bids(tmp_path):
    engine = _engine(tmp_path, 35)
    engine.tracker.state["sizing"] = {"maker_auto": False, "follow_kalshi_cash": True}
    engine.tracker.save()
    engine._enter_maker = AsyncMock(side_effect=AssertionError("maker auto is off"))
    with patch("kalshibot.campaign.engine.in_maker_window", return_value=True):
        result = asyncio.run(engine.fire("maker"))
    asyncio.run(engine.aclose())
    assert engine._enter_maker.await_count == 0
    assert any("Maker auto is off" in a for a in result["actions"])
    assert result["status"]["maker_auto"] is False


def test_maker_auto_defaults_on(tmp_path):
    from kalshibot.campaign.tracker import Tracker

    state = Tracker(tmp_path / "crypto-campaign.json").load()
    assert state["sizing"]["maker_auto"] is True


def _fifteen_pick(**kwargs):
    from kalshibot.assets import asset_by_key

    pick = {
        "series": "KXBTC15M",
        "event": {"title": "BTC 15m"},
        "market": {"ticker": "KXBTC15M-EDGE", "title": "BTC above"},
        "asset": asset_by_key("BTC"),
        "spot": 100.0,
        "strike": 99.5,
        "spec_kind": "greater",
        "cap": None,
        "secs": 12 * 60,
        "close": None,
        "hourly_vol": 0.0045,
        "model_yes": 0.62,
        "sigma": 0.4,
        "idea": None,
        "yes_bid": 0.54,
        "yes_ask": 0.56,
        "exchange_index": 2,
    }
    pick.update(kwargs)
    return pick


def _look_window():
    return patch.multiple(
        "kalshibot.campaign.engine",
        in_fifteen_entry_window=lambda now=None: True,
        in_fifteen_settlement=lambda now=None: False,
        news_blackout=lambda now=None: None,
    )


def test_dry_fifteen_posts_rest_does_not_fake_fill(tmp_path):
    engine = _engine(tmp_path, 50)
    engine.tracker.state["kalshi_total_value"] = 50.0
    engine._load_candidates = AsyncMock(return_value=[{"row": True}])
    engine._score_market = AsyncMock(return_value=_fifteen_pick())
    engine.kalshi.create_order_v2 = AsyncMock(side_effect=AssertionError("dry must not order"))
    actions = asyncio.run(engine._enter_fifteen(news=None))
    asyncio.run(engine.aclose())
    assert engine.kalshi.create_order_v2.await_count == 0
    assert any("DRY post-only yes" in a and "PASS" in a for a in actions)
    rests = [r for r in engine.tracker.state["rests"] if r.get("status") == "open"]
    assert len(rests) == 1
    assert rests[0]["loop"] == "fifteen"
    assert rests[0]["price"] == 0.54
    assert all(t.get("status") != "open" for t in engine.tracker.state["tickets"])


def test_live_fifteen_yes_joins_bid_never_ioc(tmp_path):
    engine = _engine(tmp_path, 50)
    engine.tracker.state["kalshi_total_value"] = 50.0
    engine._load_candidates = AsyncMock(return_value=[{"row": True}])
    engine._score_market = AsyncMock(return_value=_fifteen_pick())
    engine.live = True
    engine.kalshi.create_order_v2 = AsyncMock(
        return_value={"order_id": "e1", "fill_count": 0, "average_fill_price": 0}
    )
    asyncio.run(engine._enter_fifteen(news=None))
    asyncio.run(engine.aclose())
    payload = engine.kalshi.create_order_v2.await_args.args[0]
    assert payload["post_only"] is True
    assert payload["time_in_force"] == "good_till_canceled"
    assert payload["self_trade_prevention_type"] == "maker"
    assert payload["side"] == "bid"
    assert payload["price"] == "0.5400"
    count = float(payload["count"])
    assert 0.03 * 50 - 1e-6 <= count * 0.54 <= 0.05 * 50 + 1e-6


def test_live_fifteen_no_joins_yes_ask(tmp_path):
    engine = _engine(tmp_path, 50)
    engine.tracker.state["kalshi_total_value"] = 50.0
    engine._load_candidates = AsyncMock(return_value=[{"row": True}])
    engine._score_market = AsyncMock(return_value=_fifteen_pick(model_yes=0.38))
    engine.live = True
    engine.kalshi.create_order_v2 = AsyncMock(
        return_value={"order_id": "e2", "fill_count": 0, "average_fill_price": 0}
    )
    asyncio.run(engine._enter_fifteen(news=None))
    asyncio.run(engine.aclose())
    payload = engine.kalshi.create_order_v2.await_args.args[0]
    assert payload["side"] == "ask"
    assert payload["price"] == "0.5600"
    assert payload["post_only"] is True


def test_fifteen_one_idea_per_window(tmp_path):
    engine = _engine(tmp_path, 50)
    engine.tracker.state["kalshi_total_value"] = 50.0
    engine._load_candidates = AsyncMock(return_value=[{"row": True}, {"row": True}])
    engine._score_market = AsyncMock(
        side_effect=[
            _fifteen_pick(market={"ticker": "KXBTC15M-A", "title": "A"}),
            _fifteen_pick(market={"ticker": "KXETH15M-B", "title": "B"}, model_yes=0.70),
        ]
    )
    asyncio.run(engine._enter_fifteen(news=None))
    asyncio.run(engine.aclose())
    rests = [r for r in engine.tracker.state["rests"] if r.get("status") == "open"]
    assert len(rests) == 1


def test_fire_fifteen_stays_quiet_outside_look_window(tmp_path):
    engine = _engine(tmp_path, 50)
    engine._enter_fifteen = AsyncMock(side_effect=AssertionError("must not scan all day"))
    with patch("kalshibot.campaign.engine.in_fifteen_entry_window", return_value=False):
        with patch("kalshibot.campaign.engine.in_fifteen_settlement", return_value=False):
            result = asyncio.run(engine.fire("fifteen"))
    asyncio.run(engine.aclose())
    assert engine._enter_fifteen.await_count == 0
    log = engine.tracker.state["log"]
    assert log
    assert log[-1]["tell_matt"] is False


def test_fire_fifteen_fail_skip_in_look_window(tmp_path):
    engine = _engine(tmp_path, 50)
    engine.tracker.state["kalshi_total_value"] = 50.0
    engine._load_candidates = AsyncMock(return_value=[{"row": True}])
    engine._score_market = AsyncMock(return_value=_fifteen_pick(model_yes=0.56))
    with _look_window():
        result = asyncio.run(engine.fire("fifteen"))
    asyncio.run(engine.aclose())
    assert any(a.startswith("FAIL") for a in result["actions"])
    assert engine.tracker.state["log"][-1]["tell_matt"] is True
    assert all(r.get("status") != "open" for r in engine.tracker.state.get("rests", []))


def test_fifteen_revenge_skips_look_window(tmp_path):
    from datetime import datetime, timedelta, timezone

    engine = _engine(tmp_path, 50)
    engine.tracker.state["fifteen_revenge_until"] = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    engine.tracker.save()
    engine._enter_fifteen = AsyncMock(side_effect=AssertionError("revenge window"))
    with _look_window():
        result = asyncio.run(engine.fire("fifteen"))
    asyncio.run(engine.aclose())
    assert engine._enter_fifteen.await_count == 0
    assert any("revenge" in a.lower() for a in result["actions"])


def test_fifteen_loss_does_not_block_hourly_via_last_loss_at(tmp_path):
    engine = _engine(tmp_path, 50)
    ticket = {
        "ticker": "KXBTC15M-X",
        "side": "yes",
        "fill": 0.60,
        "count": 2.0,
        "loop": "fifteen",
        "model_yes": 0.70,
        "status": "open",
        "paper": True,
    }
    asyncio.run(engine._flatten(ticket, 0.40, "down_pct"))
    asyncio.run(engine.aclose())
    assert engine.tracker.state.get("last_loss_at") is None
    assert engine.tracker.state["fifteen_loss_streak"] == 1
    assert engine.tracker.state["fifteen_revenge_until"]


def test_fifteen_three_flatten_losses_stop_session(tmp_path):
    engine = _engine(tmp_path, 50)

    def lose(i):
        return asyncio.run(
            engine._flatten(
                {
                    "ticker": f"KXBTC15M-{i}",
                    "side": "yes",
                    "fill": 0.60,
                    "count": 2.0,
                    "loop": "fifteen",
                    "model_yes": 0.70,
                    "status": "open",
                    "paper": True,
                },
                0.40,
                "down_pct",
            )
        )

    lose(1)
    lose(2)
    msg = lose(3)
    asyncio.run(engine.aclose())
    assert "Three 15m losses" in msg
    from kalshibot.campaign.fifteen import fifteen_stopped

    assert fifteen_stopped(engine.tracker.state)


def test_sync_kalshi_cash_stores_total_value(tmp_path):
    engine = _engine(tmp_path, 15)
    engine.live = True
    engine.kalshi.api_key_id = "x"
    engine.kalshi._private_key = object()
    engine.kalshi.get_balance = AsyncMock(
        return_value={"balance_dollars": "48.25", "portfolio_value": 6125}
    )
    msg = asyncio.run(engine._sync_kalshi_cash())
    asyncio.run(engine.aclose())
    assert engine.tracker.state["kalshi_cash"] == 48.25
    assert engine.tracker.state["kalshi_total_value"] == 61.25
    assert engine._total_value() == 61.25
    assert "48.25" in (msg or "")


def test_total_value_floors_at_cash_when_portfolio_is_smaller(tmp_path):
    engine = _engine(tmp_path, 15)
    engine.tracker.state["kalshi_cash"] = 38.27
    engine.tracker.state["kalshi_total_value"] = 5.46
    engine.tracker.state["sizing"] = {"follow_kalshi_cash": True, "bankroll_cap": None}
    assert engine._total_value() == 38.27
    asyncio.run(engine.aclose())


def test_cancel_404_clears_expired_rest(tmp_path):
    engine = _engine(tmp_path, 50)
    engine.live = True
    engine.tracker.state["rests"].append(
        {
            "id": "ghost",
            "loop": "fifteen",
            "ticker": "KXGOLD15M-26AUG280700-00",
            "status": "open",
            "order_id": "ord-dead",
            "kind": "limit_join",
            "close_at": "2026-08-28T11:00:00+00:00",
            "paper": False,
        }
    )
    engine.kalshi.cancel_order = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "404",
            request=httpx.Request("DELETE", "https://example.test"),
            response=httpx.Response(404, json={"error": {"message": "not found"}}),
        )
    )
    actions = asyncio.run(engine._manage_rests())
    asyncio.run(engine.aclose())
    assert any("expired" in a for a in actions)
    assert all(r.get("status") != "open" for r in engine.tracker.state["rests"])
    assert engine._open_idea_count() == 0
    engine.kalshi.cancel_order.assert_awaited()
    assert engine.kalshi.cancel_order.await_args.kwargs.get("ticker") == "KXGOLD15M-26AUG280700-00"


def test_cancel_order_auto_routes_by_ticker():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"order_id": "ord-1", "reduced_by": "0"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KalshiClient("https://external-api.kalshi.com/trade-api/v2", 5.0, client=http, min_interval=0)
    asyncio.run(client.cancel_order("ord-1", ticker="KXXRP-26AUG2807-B1.4099500"))
    asyncio.run(http.aclose())
    assert "exchange_index=-1" in calls[0]
    assert "market_ticker=KXXRP-26AUG2807-B1.4099500" in calls[0]

