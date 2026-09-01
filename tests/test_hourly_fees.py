"""Taker fee estimate for the hourly bot (spec: ~1.75¢ at 50¢)."""

from math import isclose

from src.fees import (
    ev_per_contract,
    fee_per_contract_raw,
    taker_fee_cents_ceil,
    taker_fee_dollars,
    taker_fee_per_contract,
)


def test_fee_at_fifty_cents_is_about_0_0175_per_contract():
    assert isclose(fee_per_contract_raw(0.50), 0.0175, abs_tol=1e-12)
    assert isclose(taker_fee_per_contract(0.50), 0.0175, abs_tol=1e-12)


def test_taker_fee_rounds_up_to_the_next_cent():
    # 0.07 * 1 * 0.50 * 0.50 = 0.0175 → 2¢
    assert taker_fee_cents_ceil(1, 0.50) == 2
    assert isclose(taker_fee_dollars(1, 0.50), 0.02)


def test_net_ev_uses_fee_on_recommended_size_not_toy_one_lot():
    # Four contracts at 50¢: raw 0.07 → ceil to 7¢ total, 1.75¢/contract after ceil.
    p_hat = 0.60
    price = 0.50
    contracts = 4
    fee_total = taker_fee_dollars(contracts, price)
    fee_each = fee_total / contracts
    gross, net = ev_per_contract(p_hat, price, fee_each)
    assert isclose(gross, 0.60 * 0.50 - 0.40 * 0.50)
    assert net < gross
    assert fee_total >= 0.07


def test_fee_zero_at_boundaries():
    assert fee_per_contract_raw(0.0) == 0.0
    assert fee_per_contract_raw(1.0) == 0.0
    assert taker_fee_dollars(1, 0.0) == 0.0
    assert taker_fee_dollars(0, 0.50) == 0.0
