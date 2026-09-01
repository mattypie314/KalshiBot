from kalshibot.auth import sign_path_from_url
from kalshibot.fees import TAKER_K, fee_points, quadratic_fee
from src.fees import fee_per_contract_raw, taker_fee_dollars


def test_hourly_fees_are_the_campaign_formula():
    assert fee_per_contract_raw(0.50) == fee_points(0.50)
    assert taker_fee_dollars(1, 0.50) == quadratic_fee(1, 0.50, TAKER_K)


def test_sign_path_matches_trade_api_contract():
    assert (
        sign_path_from_url("https://demo-api.kalshi.co/trade-api/v2", "/portfolio/balance?x=1")
        == "/trade-api/v2/portfolio/balance"
    )
