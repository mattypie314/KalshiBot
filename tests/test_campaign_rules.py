from datetime import datetime
from zoneinfo import ZoneInfo

from kalshibot.campaign.rules import (
    already_there,
    classify_favorite,
    flatten_reason,
    in_maker_window,
    maker_join_ok,
    open_cost,
    room,
    size_for_conviction,
)
from kalshibot.campaign.universe import is_campaign_hourly_universe, shard_for_series


def test_room_and_open_cost():
    tickets = [
        {"pot": "fifteen", "status": "open", "cost": 1.75},
        {"pot": "hourly", "status": "open", "cost": 3.0},
        {"pot": "fifteen", "status": "flat", "cost": 2.0},
    ]
    assert open_cost(tickets, "fifteen") == 1.75
    assert room(5.0, 0.25, 1.75) == 3.5


def test_sizing_matches_grokbot():
    assert size_for_conviction("fifteen", "thin") == 0.50
    assert size_for_conviction("fifteen", "real") == 1.75
    assert size_for_conviction("fifteen", "fat") == 2.50
    assert size_for_conviction("hourly", "thin") == 1.0
    assert size_for_conviction("hourly", "real") == 3.5
    assert size_for_conviction("hourly", "fat") == 5.0


def test_favorite_spot_vs_target_agrees_with_book():
    fav = classify_favorite(spot=79650, strike=79600, yes_bid=0.80, yes_ask=0.82, model_yes=0.88)
    assert fav is not None
    assert fav.side == "yes"
    assert fav.conviction in {"real", "fat"}
    no_fav = classify_favorite(spot=79500, strike=79600, yes_bid=0.20, yes_ask=0.22, model_yes=0.18)
    assert no_fav is not None
    assert no_fav.side == "no"


def test_favorite_rejects_book_disagreement():
    assert classify_favorite(spot=79650, strike=79600, yes_bid=0.20, yes_ask=0.22, model_yes=0.88) is None


def test_mid_book_is_thin_not_real_favorite():
    fav = classify_favorite(spot=4642, strike=4590, yes_bid=0.52, yes_ask=0.53, model_yes=0.99)
    assert fav is not None
    assert fav.conviction == "thin"
    assert not fav.is_real_or_better


def test_maker_band():
    fav = classify_favorite(spot=100, strike=99, yes_bid=0.80, yes_ask=0.82, model_yes=0.85)
    assert fav is not None and maker_join_ok(fav)
    fat = classify_favorite(spot=100, strike=90, yes_bid=0.96, yes_ask=0.97, model_yes=0.99)
    assert fat is not None and fat.conviction == "fat"
    assert not maker_join_ok(fat)


def test_flatten_take_profit_and_stops():
    ticket = {
        "side": "yes",
        "fill": 0.80,
        "count": 2.0,
        "filled_at": "2026-08-27T12:00:00Z",
    }
    assert flatten_reason(ticket, yes_bid=0.83, yes_ask=0.84) == "take_profit_2c"
    assert flatten_reason(ticket, yes_bid=0.99, yes_ask=0.99) == "bid_99"
    cheap = dict(ticket)
    cheap["count"] = 10
    assert flatten_reason(cheap, yes_bid=0.74, yes_ask=0.76) == "down_50c"
    assert flatten_reason(ticket, yes_bid=0.70, yes_ask=0.72) == "down_pct"


def test_legacy_18_percent_flatten():
    ticket = {
        "side": "yes",
        "fill": 0.80,
        "count": 1.0,
        "filled_at": "2026-08-27T06:50:00Z",  # 2:50 AM ET
    }
    # 12% drop is not 18% yet
    assert flatten_reason(ticket, yes_bid=0.70, yes_ask=0.72) is None
    assert flatten_reason(ticket, yes_bid=0.65, yes_ask=0.66) == "down_pct"


def test_maker_window_minutes():
    et = ZoneInfo("America/New_York")
    assert in_maker_window(datetime(2026, 8, 27, 10, 57, tzinfo=et))
    assert not in_maker_window(datetime(2026, 8, 27, 10, 10, tzinfo=et))


def test_empty_tracker_file_starts_fresh(tmp_path):
    from kalshibot.campaign.tracker import Tracker

    path = tmp_path / "crypto-campaign.json"
    path.write_text("")
    tracker = Tracker(path)
    state = tracker.load()
    assert state["pots"]["fifteen"]["bankroll"] == 5.0
    assert path.read_text().startswith("{")


def test_empty_kalshi_live_is_false(monkeypatch):
    monkeypatch.setenv("KALSHI_LIVE", "")
    from kalshibot.config import Settings

    assert Settings().kalshi_live is False


def test_already_there_skips_99_book():
    fav = classify_favorite(spot=800, strike=100, yes_bid=0.99, yes_ask=1.00, model_yes=0.99)
    assert fav is not None
    assert already_there(fav)
    real = classify_favorite(spot=100, strike=99, yes_bid=0.80, yes_ask=0.82, model_yes=0.85)
    assert real is not None
    assert not already_there(real)


def test_hourly_universe_and_shards():
    assert is_campaign_hourly_universe({"category": "Crypto", "frequency": "hourly", "ticker": "KXBTC", "title": "BTC hour"})
    assert not is_campaign_hourly_universe({"category": "Crypto", "frequency": "daily", "ticker": "KXETHD", "title": "ETH daily"})
    assert shard_for_series("KXGOLD15M", "Gold 15-minute") == 0
    assert shard_for_series("KXBTC15M", "BTC 15 min") == 2
