"""Fractional Kelly sizer never breaches the $2 hard cap on a $40 book."""

from src.sizer import (
    SizeDecision,
    economic_risk_dollars,
    labeled_limit_from_yes_book,
    maker_cost_per_contract,
    size_idea,
    yes_book_price,
)


BANKROLL = 40.00
MAX_RISK = 2.00


def _size(**kwargs) -> SizeDecision:
    defaults = dict(
        bankroll=BANKROLL,
        entry_price=0.50,
        p_hat=0.62,
        kelly_mult=0.25,
        max_risk_pct=0.05,
        max_risk_dollars=MAX_RISK,
        preferred_risk_dollars=1.75,
        last_loss_same_hour=False,
        last_contracts=None,
    )
    defaults.update(kwargs)
    return size_idea(**defaults)


def test_sizer_never_exceeds_two_dollars_on_40_bankroll():
    for price in (0.15, 0.40, 0.50, 0.60, 0.80, 0.94):
        for p_hat in (0.55, 0.70, 0.90, 0.99):
            decision = _size(entry_price=price, p_hat=p_hat)
            if decision.skip:
                continue
            assert decision.risk_dollars <= MAX_RISK + 1e-9
            assert decision.contracts * price <= MAX_RISK + 1e-9


def test_sizer_caps_at_preferred_when_kelly_is_large():
    decision = _size(entry_price=0.50, p_hat=0.90)
    assert not decision.skip
    assert decision.risk_dollars <= 1.75 + 1e-9
    assert decision.contracts == 3  # floor(1.75 / 0.50)


def test_sizer_skips_when_one_contract_exceeds_cap():
    decision = _size(entry_price=3.50, p_hat=0.90, max_risk_dollars=3.00)
    # entry_price is a probability; use a synthetic high dollar risk via price~1
    # A $4 contract cannot exist (max $1). Simulate via tiny bankroll + high floor:
    decision = size_idea(
        bankroll=40.00,
        entry_price=0.99,
        p_hat=0.999,
        kelly_mult=0.25,
        max_risk_pct=0.05,
        max_risk_dollars=0.50,  # one 99¢ contract already over this toy cap
        preferred_risk_dollars=0.50,
    )
    assert decision.skip
    assert decision.contracts == 0


def test_sizer_does_not_increase_size_after_loss_same_hour():
    first = _size(entry_price=0.50, p_hat=0.80)
    assert first.contracts >= 2
    revenge = _size(
        entry_price=0.50,
        p_hat=0.80,
        last_loss_same_hour=True,
        last_contracts=1,
    )
    assert not revenge.skip
    assert revenge.contracts == 1


def test_no_via_sell_yes_sizes_from_complement_not_cheap_limit():
    """2026-09-07 ETH No: 4 × 0.24 looked like $0.96; sell-Yes @ 0.22 locked $3.12."""
    yes_ask = 0.24
    cost = maker_cost_per_contract("No", yes_book=yes_ask)
    assert cost == 0.76
    assert labeled_limit_from_yes_book("No", yes_ask) == 0.76
    assert yes_book_price("No", 0.76) == 0.24

    max_risk = 1.50
    # Old math: floor(1.50 / 0.24) = 6 contracts × $0.76 = $4.56 over the cap.
    leaked = int(max_risk / yes_ask) * cost
    assert leaked > max_risk

    decision = _size(
        entry_price=cost,
        p_hat=0.90,
        max_risk_dollars=max_risk,
        preferred_risk_dollars=max_risk,
        cost_price=cost,
    )
    assert not decision.skip
    assert decision.contracts * cost <= max_risk + 1e-9
    assert decision.risk_dollars == economic_risk_dollars(decision.contracts, cost)
    assert decision.risk_dollars <= max_risk + 1e-9
    assert decision.contracts == 1  # floor(1.50 / 0.76)


def test_yes_via_buy_yes_still_sizes_on_limit():
    decision = _size(
        entry_price=0.54,
        p_hat=0.70,
        max_risk_dollars=1.50,
        preferred_risk_dollars=1.50,
        cost_price=maker_cost_per_contract("Yes", labeled_limit=0.54),
    )
    assert not decision.skip
    assert decision.contracts == 2  # floor(1.50 / 0.54)
    assert decision.risk_dollars == economic_risk_dollars(2, 0.54)
    assert yes_book_price("Yes", 0.54) == 0.54
