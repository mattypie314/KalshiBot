from datetime import datetime
from zoneinfo import ZoneInfo

from kalshibot.campaign.rules import (
    already_there,
    classify_favorite,
    flatten_reason,
    in_maker_window,
    in_pay_band,
    maker_join_ok,
    open_cost,
    room,
    size_for_conviction,
)
from kalshibot.campaign.universe import HOURLY_MAX_SECONDS, is_campaign_hourly_universe, is_daily_ticker, shard_for_series


def test_room_and_open_cost():
    tickets = [
        {"status": "open", "cost": 1.75},
        {"status": "open", "cost": 3.0},
        {"status": "flat", "cost": 2.0},
    ]
    assert open_cost(tickets) == 4.75
    assert room(15.0, 0.25, 4.75) == 10.5


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
    no_fav = classify_favorite(spot=99, strike=100, yes_bid=0.18, yes_ask=0.20, model_yes=0.17)
    assert no_fav is not None and no_fav.side == "no"
    assert maker_join_ok(no_fav)


def test_maker_spread_is_taker_breakeven_not_a_model_misprice():
    from kalshibot.campaign.rules import maker_spread_ok, taker_net_edge

    fair = classify_favorite(spot=100, strike=99.8, yes_bid=0.80, yes_ask=0.82, model_yes=0.83)
    assert fair is not None
    assert abs(taker_net_edge(fair)) < 0.02
    assert maker_spread_ok(fair, 0.80, 0.82)
    stale = classify_favorite(spot=99, strike=100, yes_bid=0.80, yes_ask=0.82, model_yes=0.40)
    assert stale is None  # book disagrees with spot
    rich = classify_favorite(spot=100, strike=99, yes_bid=0.80, yes_ask=0.82, model_yes=0.70)
    assert rich is not None
    assert not maker_spread_ok(rich, 0.80, 0.82)


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
    assert state["bankroll"] == 15.0
    assert state["realized"] == 0.0
    assert "pots" not in state
    assert path.read_text().startswith("{")


def test_empty_kalshi_live_is_false(monkeypatch):
    monkeypatch.setenv("KALSHI_LIVE", "")
    from kalshibot.config import Settings

    assert Settings().kalshi_live is False


def test_already_there_skips_99_book():
    fav = classify_favorite(spot=800, strike=100, yes_bid=0.99, yes_ask=1.00, model_yes=0.99)
    assert fav is not None
    assert already_there(fav)
    assert not in_pay_band(fav)
    real = classify_favorite(spot=100, strike=99, yes_bid=0.80, yes_ask=0.82, model_yes=0.85)
    assert real is not None
    assert not already_there(real)
    assert in_pay_band(real)


def test_pay_band_is_74_to_96():
    cheap = classify_favorite(spot=100, strike=99, yes_bid=0.70, yes_ask=0.72, model_yes=0.80)
    assert cheap is not None
    assert not in_pay_band(cheap)
    lock = classify_favorite(spot=800, strike=100, yes_bid=0.97, yes_ask=0.98, model_yes=0.99)
    assert lock is not None
    assert not in_pay_band(lock)


def test_hourly_universe_ignores_daily_d_tickers():
    assert is_campaign_hourly_universe({"category": "Crypto", "frequency": "hourly", "ticker": "KXBTC", "title": "BTC hour"})
    assert is_campaign_hourly_universe({"category": "Crypto", "frequency": "hourly", "ticker": "KXZECH", "title": "ZEC hourly"})
    assert not is_campaign_hourly_universe({"category": "Crypto", "frequency": "hourly", "ticker": "KXBTCD", "title": "BTC hour"})
    assert not is_campaign_hourly_universe({"category": "Crypto", "frequency": "daily", "ticker": "KXETHD", "title": "ETH daily"})
    assert not is_campaign_hourly_universe({"category": "Crypto", "frequency": "", "ticker": "KXDOGED", "title": "DOGE"})
    assert not is_campaign_hourly_universe({"category": "Crypto", "frequency": "", "ticker": "KXBNBD", "title": "BNB"})
    assert is_daily_ticker("KXBTCD-26AUG2817-T75999.99")
    assert is_daily_ticker("KXETHD")
    assert not is_daily_ticker("KXBTC15M")
    assert not is_daily_ticker("KXBTC")
    assert HOURLY_MAX_SECONDS == 75 * 60
    assert shard_for_series("KXGOLD15M", "Gold 15-minute") == 0
    assert shard_for_series("KXBTC15M", "BTC 15 min") == 2


def test_legacy_pots_fold_into_one_book(tmp_path):
    from kalshibot.campaign.tracker import Tracker

    path = tmp_path / "crypto-campaign.json"
    path.write_text(
        """
        {
          "pots": {
            "fifteen": {"bankroll": 5.0, "realized": -1.72, "stopped": true, "stop_reason": "x"},
            "hourly": {"bankroll": 10.0, "realized": -0.30, "stopped": false, "stop_reason": null}
          },
          "tickets": []
        }
        """
    )
    tracker = Tracker(path)
    state = tracker.load()
    assert "pots" not in state
    assert state["bankroll"] == 15.0
    assert state["realized"] == -2.02
    assert "stopped" not in state
