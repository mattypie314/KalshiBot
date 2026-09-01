"""Kalshi taker fee estimate. Maker treated as 0 unless series docs say otherwise."""

from __future__ import annotations

from kalshibot.fees import TAKER_K, fee_points, quadratic_fee

# Same quadratic as the campaign desk — one formula, two CLIs.
fee_per_contract_raw = fee_points
taker_fee_per_contract = fee_points


def taker_fee_dollars(contracts: float, price: float, k: float = TAKER_K) -> float:
    return quadratic_fee(contracts, price, k)


def taker_fee_cents_ceil(contracts: float, price: float, k: float = TAKER_K) -> int:
    return int(round(taker_fee_dollars(contracts, price, k) * 100))


def ev_per_contract(p_hat: float, price: float, fee_per_contract: float) -> tuple[float, float]:
    """gross = p*(1-price) - (1-p)*price; net = gross - fee."""
    if price < 0 or price > 1:
        return 0.0, 0.0
    gross = p_hat * (1.0 - price) - (1.0 - p_hat) * price
    return gross, gross - fee_per_contract


def fee_on_size(contracts: int, price: float, *, maker: bool) -> tuple[float, float]:
    """Return (fee_total, fee_per_contract) for the recommended size."""
    if maker or contracts <= 0:
        return 0.0, 0.0
    total = taker_fee_dollars(contracts, price)
    return total, total / contracts
