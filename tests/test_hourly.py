import asyncio
from unittest.mock import AsyncMock

from kalshibot.campaign.hourly import (
    HOURLY_SERIES,
    MIN_NET_EDGE,
    NO_EDGE,
    format_hourly,
    grade_tape,
    hourly_stake,
    pick_atm,
)
from kalshibot.fees import TAKER_K, fee_points
from tests.test_campaign_orders import _engine


def test_pick_atm_is_nearest_strike_to_spot():
    rows = [
        {"ticker": "far", "spot": 100.0, "strike": 110.0},
        {"ticker": "atm", "spot": 100.0, "strike": 100.5},
        {"ticker": "also-far", "spot": 100.0, "strike": 90.0},
    ]
    pick = pick_atm(rows)
    assert pick is not None
    assert pick["ticker"] == "atm"


def test_hourly_stake_is_three_to_five_percent():
    assert hourly_stake(100.0, 100.0) == 4.0
    assert hourly_stake(100.0, 2.0) == 2.0
    assert hourly_stake(0.0, 10.0) == 0.0


def test_grade_tape_fails_under_six_percent_net():
    tape = grade_tape(
        ticker="KXBTC15M-X",
        model_yes=0.52,
        yes_bid=0.49,
        yes_ask=0.51,
        secs_left=600,
        strike=100.0,
        spot=100.0,
    )
    assert tape.passed is False
    assert tape.net_edge < MIN_NET_EDGE
    assert format_hourly(tape) == NO_EDGE


def test_grade_tape_passes_when_net_clears_six_percent():
    tape = grade_tape(
        ticker="KXBTC15M-X",
        model_yes=0.72,
        yes_bid=0.49,
        yes_ask=0.51,
        secs_left=600,
        strike=99.5,
        spot=100.0,
    )
    mid = 0.50
    expected = (0.72 - mid) - fee_points(mid, TAKER_K)
    assert expected >= MIN_NET_EDGE
    assert tape.passed is True
    assert tape.side == "yes"
    card = format_hourly(tape)
    assert card != NO_EDGE
    assert "Market: KXBTC15M-X" in card
    assert "Filter: Pass" in card
    assert "3–5% of bankroll, limit only" in card
    assert "Skip reason" not in card


def test_fail_card_is_only_no_actionable_edge():
    tape = grade_tape(
        ticker="KXBTC15M-X",
        model_yes=0.52,
        yes_bid=0.49,
        yes_ask=0.51,
        secs_left=600,
        strike=100.0,
        spot=100.0,
    )
    assert format_hourly(tape) == "No actionable edge."


def _tape_pick(**kwargs):
    from kalshibot.assets import asset_by_key

    pick = {
        "series": HOURLY_SERIES,
        "event": {"title": "BTC 15m"},
        "market": {"ticker": "KXBTC15M-ATM", "title": "BTC above"},
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
        "yes_bid": 0.49,
        "yes_ask": 0.51,
        "exchange_index": 2,
    }
    pick.update(kwargs)
    return pick


def test_enter_hourly_posts_post_only_limit_on_pass(tmp_path):
    engine = _engine(tmp_path, 50)
    engine._load_candidates = AsyncMock(return_value=[{"row": True}])
    engine._score_market = AsyncMock(return_value=_tape_pick())
    engine.live = True
    engine.kalshi.create_order_v2 = AsyncMock(
        return_value={"order_id": "h1", "fill_count": 0, "average_fill_price": 0}
    )
    actions = asyncio.run(engine._enter_hourly())
    asyncio.run(engine.aclose())
    payload = engine.kalshi.create_order_v2.await_args.args[0]
    assert payload["ticker"] == "KXBTC15M-ATM"
    assert payload["post_only"] is True
    assert payload["time_in_force"] == "good_till_canceled"
    assert payload["self_trade_prevention_type"] == "maker"
    assert payload["side"] == "bid"
    card = actions[0]
    assert card.startswith("Market:")
    assert "Filter: Pass" in card
    rests = [r for r in engine.tracker.state["rests"] if r.get("status") == "open"]
    assert len(rests) == 1
    assert rests[0]["loop"] == "hourly"
    assert rests[0]["kind"] == "btc15m_tape"


def test_hourly_fire_notifies_pass_card_not_cash_sync(tmp_path):
    engine = _engine(tmp_path, 50)
    engine.tracker.state["kalshi_cash"] = 50.0
    engine._quotes_for_open_tickets = AsyncMock(return_value={})
    engine._manage_open = AsyncMock(return_value=[])
    engine._manage_rests = AsyncMock(return_value=[])
    engine._enter_hourly = AsyncMock(
        return_value=[
            "Market: KXBTC15M-ATM\nMinutes left / strike / spot: 10m / 99.5 / 100\n"
            "Yes mid vs model fair: 0.50 vs 0.72\nEdge after fees: +20.2%\n"
            "Filter: Pass\nSize if pass: 3–5% of bankroll, limit only"
        ]
    )
    result = asyncio.run(engine.fire("hourly"))
    asyncio.run(engine.aclose())
    told = [row for row in engine.tracker.state["log"] if row.get("tell_matt")]
    assert told
    assert told[-1]["message"].startswith("Market:")
    assert any(a.startswith("Market:") for a in result["actions"])


def test_hourly_fire_notifies_no_actionable_edge(tmp_path):
    engine = _engine(tmp_path, 50)
    engine._quotes_for_open_tickets = AsyncMock(return_value={})
    engine._manage_open = AsyncMock(return_value=[])
    engine._manage_rests = AsyncMock(return_value=[])
    engine._enter_hourly = AsyncMock(return_value=[NO_EDGE])
    result = asyncio.run(engine.fire("hourly"))
    asyncio.run(engine.aclose())
    assert result["actions"][-1] == NO_EDGE
    assert engine.tracker.state["log"][-1]["message"] == NO_EDGE
    assert engine.tracker.state["log"][-1]["tell_matt"] is True


def test_hourly_does_not_revenge_sit_after_last_loss(tmp_path):
    from datetime import datetime, timezone

    engine = _engine(tmp_path, 50)
    engine.tracker.state["last_loss_at"] = datetime.now(timezone.utc).isoformat()
    engine.tracker.save()
    engine._quotes_for_open_tickets = AsyncMock(return_value={})
    engine._manage_open = AsyncMock(return_value=[])
    engine._manage_rests = AsyncMock(return_value=[])
    engine._enter_hourly = AsyncMock(return_value=[NO_EDGE])
    result = asyncio.run(engine.fire("hourly"))
    asyncio.run(engine.aclose())
    assert engine._enter_hourly.await_count == 1
    assert not any("revenge" in a.lower() for a in result["actions"])
    assert result["actions"][-1] == NO_EDGE
