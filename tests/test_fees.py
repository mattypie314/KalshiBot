from kalshibot.fees import fee_points, maker_fee, taker_fee


def test_taker_fee_rounds_up_to_a_cent():
    # 0.07 * 1 * 0.50 * 0.50 = 0.0175 → 2¢
    assert taker_fee(1, 0.50) == 0.02


def test_maker_fee_is_a_quarter_of_taker():
    assert maker_fee(1, 0.50) == 0.01


def test_fee_points_match_quadratic_before_rounding():
    assert abs(fee_points(0.50) - 0.0175) < 1e-9
    assert fee_points(0.0) == 0.0
    assert fee_points(1.0) == 0.0
