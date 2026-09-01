"""Kalshi taker fee estimate. Maker treated as 0 unless series docs say otherwise."""

from __future__ import annotations

import math


TAKER_K = 0.07


def fee_per_contract_raw(price: float, k: float = TAKER_K) -> float:
    """Unrounded taker fee per contract. At 50¢ this is 0.0175."""
    if price <= 0 or price >= 1:
        return 0.0
    return k * price * (1.0 - price)


def taker_fee_per_contract(price: float, k: float = TAKER_K) -> float:
    return fee_per_contract_raw(price, k)


def taker_fee_cents_ceil(contracts: float, price: float, k: float = TAKER_K) -> int:
    if contracts <= 0 or price <= 0 or price >= 1:
        return 0
    raw = k * contracts * price * (1.0 - price)
    return int(math.ceil(raw * 100.0 - 1e-12))


def taker_fee_dollars(contracts: float, price: float, k: float = TAKER_K) -> float:
    return taker_fee_cents_ceil(contracts, price, k) / 100.0


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
