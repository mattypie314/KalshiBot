"""Hourly lognormal model: zero-drift short-horizon fair probs."""

from math import isclose

from src.model import fair_no, fair_prob, hours_left, model_z


def test_fair_prob_atm_is_fifty_percent_any_horizon():
    for hours in (0.05, 0.25, 1.0, 2.0):
        yes = fair_prob(spot=100.0, threshold=100.0, hourly_vol=0.004, hours_left=hours)
        assert isclose(yes, 0.5, abs_tol=1e-9)


def test_fair_prob_spot_well_above_threshold_short_time_yes_high():
    yes = fair_prob(spot=110.0, threshold=100.0, hourly_vol=0.004, hours_left=0.1)
    assert yes > 0.95
    assert fair_no(spot=110.0, threshold=100.0, hourly_vol=0.004, hours_left=0.1) < 0.05


def test_fair_prob_spot_well_below_threshold_yes_low():
    yes = fair_prob(spot=90.0, threshold=100.0, hourly_vol=0.004, hours_left=0.1)
    assert yes < 0.05


def test_fair_no_complements_fair_yes():
    yes = fair_prob(spot=101.0, threshold=100.0, hourly_vol=0.005, hours_left=0.5)
    no = fair_no(spot=101.0, threshold=100.0, hourly_vol=0.005, hours_left=0.5)
    assert isclose(yes + no, 1.0, abs_tol=1e-12)


def test_hours_left_skips_non_positive():
    assert hours_left(0.0) is None
    assert hours_left(-5.0) is None
    assert isclose(hours_left(1800.0), 0.5)


def test_z_score_zero_when_spot_equals_threshold():
    z = model_z(spot=100.0, threshold=100.0, hourly_vol=0.004, hours_left=1.0)
    assert isclose(z, 0.0, abs_tol=1e-12)
