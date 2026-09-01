"""Fractional Kelly sizer never breaches the $3 hard cap on a $40 book."""

from src.sizer import SizeDecision, size_idea


BANKROLL = 40.00
MAX_RISK = 3.00


def _size(**kwargs) -> SizeDecision:
    defaults = dict(
        bankroll=BANKROLL,
        entry_price=0.50,
        p_hat=0.62,
        kelly_mult=0.25,
        max_risk_pct=0.05,
        max_risk_dollars=MAX_RISK,
        preferred_risk_dollars=2.00,
        last_loss_same_hour=False,
        last_contracts=None,
    )
    defaults.update(kwargs)
    return size_idea(**defaults)


def test_sizer_never_exceeds_three_dollars_on_40_bankroll():
    for price in (0.15, 0.40, 0.50, 0.60, 0.80, 0.94):
        for p_hat in (0.55, 0.70, 0.90, 0.99):
            decision = _size(entry_price=price, p_hat=p_hat)
            if decision.skip:
                continue
            assert decision.risk_dollars <= MAX_RISK + 1e-9
            assert decision.contracts * price <= MAX_RISK + 1e-9


def test_sizer_caps_at_preferred_two_dollars_when_kelly_is_large():
    decision = _size(entry_price=0.50, p_hat=0.90)
    assert not decision.skip
    assert decision.risk_dollars <= 2.00 + 1e-9
    assert decision.contracts == 4  # floor(2.00 / 0.50)


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
