from kalshibot.campaign.playbook import Playbook, evaluate_idea, kelly_stake
from kalshibot.models import QUIET_HOUR_VOL, annual_vol_from_hourly, distance_in_sigma
from kalshibot.spots import hourly_vol_from_closes


def test_typical_35_dollar_idea_is_about_two_dollars():
    stake = kelly_stake(35.0, 0.62, 0.52)
    assert 1.50 <= stake <= 2.80


def test_under_20_caps_risk_at_3_percent():
    stake = kelly_stake(15.0, 0.80, 0.50)
    assert stake == 0.45  # 3% of $15


def test_hard_cap_is_10_percent():
    aggressive = Playbook(kelly_fraction=1.0, risk_cap=0.50, risk_hard_max=0.10)
    stake = aggressive.kelly_stake(100.0, 0.90, 0.50)
    assert stake == 10.0


def test_growing_book_only_needs_playbook_percents_or_bankroll():
    bigger = Playbook(risk_cap=0.05, kelly_fraction=0.25)
    stake = bigger.kelly_stake(200.0, 0.62, 0.52)
    assert stake <= 0.05 * 200
    assert stake > 0


def test_filters_sit_out_when_net_edge_is_thin():
    idea = evaluate_idea(
        yes_bid=0.49,
        yes_ask=0.51,
        model_yes=0.54,
        sigma=0.2,
        secs_left=600,
        equity=35.0,
    )
    assert idea.sit_out is not None


def test_filters_pass_a_clear_misprice():
    idea = evaluate_idea(
        yes_bid=0.49,
        yes_ask=0.51,
        model_yes=0.72,
        sigma=0.4,
        secs_left=600,
        equity=35.0,
    )
    assert idea.sit_out is None
    assert idea.side == "yes"
    assert idea.net_edge >= 0.06
    assert idea.join_price == 0.50  # one tick inside the spread


def test_spread_that_eats_the_edge_sits_out():
    idea = evaluate_idea(
        yes_bid=0.40,
        yes_ask=0.50,
        model_yes=0.56,
        sigma=0.1,
        secs_left=600,
        equity=35.0,
    )
    assert idea.sit_out is not None
    assert "spread" in idea.sit_out


def test_not_enough_time_left_sits_out():
    idea = evaluate_idea(
        yes_bid=0.49,
        yes_ask=0.51,
        model_yes=0.80,
        sigma=0.1,
        secs_left=60,
        equity=35.0,
    )
    assert "left" in (idea.sit_out or "")


def test_small_bankroll_demands_six_percent_edge():
    thin_ok_on_35 = evaluate_idea(
        yes_bid=0.50,
        yes_ask=0.51,
        model_yes=0.57,
        sigma=0.2,
        secs_left=600,
        equity=35.0,
    )
    tight = evaluate_idea(
        yes_bid=0.50,
        yes_ask=0.51,
        model_yes=0.57,
        sigma=0.2,
        secs_left=600,
        equity=15.0,
    )
    assert thin_ok_on_35.sit_out is None
    assert tight.sit_out is not None


def test_distance_in_sigma_is_far_when_spot_is_away():
    hour_vol = QUIET_HOUR_VOL["BTC"]
    near = abs(distance_in_sigma(100.0, 100.2, hour_vol, 0.25))
    far = abs(distance_in_sigma(100.0, 110.0, hour_vol, 0.25))
    assert far > near
    assert far > 2


def test_hourly_vol_from_one_minute_closes():
    closes = [100.0]
    for i in range(60):
        closes.append(closes[-1] * (1.001 if i % 2 == 0 else 0.999))
    vol = hourly_vol_from_closes(closes, 60)
    assert vol is not None
    assert 0.001 < vol < 0.05


def test_annual_vol_scales_from_hourly():
    assert annual_vol_from_hourly(0.0045) > 0.3
