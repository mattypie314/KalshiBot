from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

from kalshibot.campaign.fifteen import (
    CPI_DATES,
    enough_room,
    fifteen_session_date,
    fifteen_stake,
    fifteen_stopped,
    fifteen_window_id,
    fifteen_window_start,
    fifteen_working,
    half_sigma_move,
    in_fifteen_entry_window,
    in_fifteen_revenge,
    in_fifteen_settlement,
    news_blackout,
    next_et_midnight,
    pass_fail,
    record_fifteen_result,
    revenge_until_after_loss,
    strike_decided,
)

ET = ZoneInfo("America/New_York")


def _et(hour, minute, day=28, month=8, year=2026):
    return datetime(year, month, day, hour, minute, tzinfo=ET)


def test_entry_window_is_first_two_to_four_minutes():
    assert in_fifteen_entry_window(_et(10, 2))
    assert in_fifteen_entry_window(_et(10, 3))
    assert in_fifteen_entry_window(_et(10, 4))
    assert in_fifteen_entry_window(_et(10, 17))
    assert in_fifteen_entry_window(_et(10, 32))
    assert in_fifteen_entry_window(_et(10, 49))
    assert not in_fifteen_entry_window(_et(10, 0))
    assert not in_fifteen_entry_window(_et(10, 1))
    assert not in_fifteen_entry_window(_et(10, 5))
    assert not in_fifteen_entry_window(_et(10, 12))


def test_settlement_is_minute_zero_of_the_window():
    assert in_fifteen_settlement(_et(10, 0))
    assert in_fifteen_settlement(_et(10, 15))
    assert in_fifteen_settlement(_et(10, 30))
    assert in_fifteen_settlement(_et(10, 45))
    assert not in_fifteen_settlement(_et(10, 2))
    assert fifteen_window_start(_et(10, 17)) == _et(10, 15)
    assert fifteen_window_id(_et(10, 17)).endswith("10:15:00-04:00") or "10:15:00" in fifteen_window_id(_et(10, 17))


def test_pass_when_fair_clears_mid_by_four_cents_and_spread_fits():
    decision = pass_fail(model_yes=0.62, yes_bid=0.54, yes_ask=0.56, secs_left=12 * 60, sigma=0.4)
    assert decision.passed
    assert decision.side == "yes"
    assert decision.join_price == 0.54
    assert decision.line.startswith("PASS")
    assert "fair 0.62 vs mid 0.55" in decision.line


def test_pass_no_joins_the_live_yes_ask():
    decision = pass_fail(model_yes=0.38, yes_bid=0.54, yes_ask=0.56, secs_left=12 * 60, sigma=0.4)
    assert decision.passed
    assert decision.side == "no"
    assert decision.join_price == 0.56


def test_fail_within_four_cents():
    decision = pass_fail(model_yes=0.56, yes_bid=0.54, yes_ask=0.56, secs_left=12 * 60, sigma=0.4)
    assert not decision.passed
    assert "FAIL" in decision.line
    assert "only 1¢" in decision.line or "only 2¢" in decision.line


def test_fail_when_spread_wider_than_edge():
    decision = pass_fail(model_yes=0.60, yes_bid=0.48, yes_ask=0.58, secs_left=12 * 60, sigma=0.4)
    assert not decision.passed
    assert "spread" in decision.line


def test_fail_under_eight_minutes_unless_decided():
    early = pass_fail(model_yes=0.70, yes_bid=0.54, yes_ask=0.56, secs_left=6 * 60, sigma=0.4)
    assert not early.passed
    assert "8m" in early.line
    decided = pass_fail(model_yes=0.98, yes_bid=0.90, yes_ask=0.92, secs_left=5 * 60, sigma=2.4)
    assert decided.passed
    assert strike_decided(0.98, 0.4)
    assert strike_decided(0.50, 2.0)


def test_fail_news_candle():
    decision = pass_fail(
        model_yes=0.70,
        yes_bid=0.54,
        yes_ask=0.56,
        secs_left=12 * 60,
        sigma=0.4,
        news="CPI",
    )
    assert not decision.passed
    assert "CPI" in decision.line


def test_news_blackout_cpi_and_fomc_fixtures():
    assert (2026, 9, 11) in CPI_DATES
    cpi = datetime(2026, 9, 11, 8, 30, tzinfo=ET)
    assert news_blackout(cpi) == "CPI"
    assert news_blackout(datetime(2026, 9, 11, 10, 0, tzinfo=ET)) is None
    fomc = datetime(2026, 9, 16, 14, 0, tzinfo=ET)
    assert news_blackout(fomc) == "FOMC"
    with patch.dict("os.environ", {"NEWS_BLACKOUT": "1"}):
        assert news_blackout(_et(10, 3)) == "NEWS_BLACKOUT"


def test_one_idea_per_window():
    now = _et(10, 3)
    wid = fifteen_window_id(now)
    state = {
        "tickets": [{"status": "open", "loop": "fifteen", "window_id": wid, "ticker": "KXBTC15M-A"}],
        "rests": [],
    }
    assert fifteen_working(state, now)
    state["tickets"][0]["status"] = "flat"
    assert not fifteen_working(state, now)
    state["rests"] = [{"status": "open", "loop": "fifteen", "window_id": wid}]
    assert fifteen_working(state, now)


def test_revenge_skips_the_next_window_only():
    loss_at = _et(10, 8)
    state = {}
    record_fifteen_result(state, pnl=-0.40, now=loss_at)
    until = revenge_until_after_loss(loss_at)
    assert until == _et(10, 30)
    assert in_fifteen_revenge(state, _et(10, 17))
    assert in_fifteen_revenge(state, _et(10, 29))
    assert not in_fifteen_revenge(state, _et(10, 32))


def test_three_fifteen_losses_stop_the_session():
    now = _et(10, 8)
    state = {}
    assert record_fifteen_result(state, -0.2, now) is None
    assert record_fifteen_result(state, -0.2, now + timedelta(minutes=30)) is None
    msg = record_fifteen_result(state, -0.2, now + timedelta(minutes=60))
    assert msg is not None
    assert "Three 15m losses" in msg
    assert fifteen_stopped(state, now + timedelta(minutes=61))
    assert not fifteen_stopped(state, next_et_midnight(now))
    assert fifteen_session_date(now) == "2026-08-28"


def test_win_resets_the_fifteen_streak():
    now = _et(10, 8)
    state = {}
    record_fifteen_result(state, -0.2, now)
    record_fifteen_result(state, -0.2, now)
    record_fifteen_result(state, 0.10, now)
    assert state["fifteen_loss_streak"] == 0
    record_fifteen_result(state, -0.2, now)
    assert state["fifteen_loss_streak"] == 1
    assert not fifteen_stopped(state, now)


def test_size_is_three_to_five_percent_of_total_value():
    assert fifteen_stake(100.0, 100.0) == 4.0
    assert fifteen_stake(100.0, 2.0) == 2.0
    assert enough_room(3.0, 100.0)
    assert not enough_room(2.0, 100.0)


def test_half_sigma_move():
    assert not half_sigma_move(100.0, 100.0, 0.0045)
    assert half_sigma_move(100.5, 100.0, 0.0045)
    assert not half_sigma_move(100.1, 100.0, 0.0045)
