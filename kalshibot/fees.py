from __future__ import annotations

import math


TAKER_K = 0.07
MAKER_K = 0.0175


def quadratic_fee(count: float, price: float, k: float, multiplier: float = 1.0) -> float:
    """Kalshi quadratic fee, rounded up to the next cent."""
    if count <= 0 or price <= 0 or price >= 1:
        return 0.0
    raw = multiplier * k * count * price * (1.0 - price)
    return math.ceil(raw * 100.0) / 100.0


def taker_fee(count: float, price: float, multiplier: float = 1.0) -> float:
    return quadratic_fee(count, price, TAKER_K, multiplier)


def maker_fee(count: float, price: float, multiplier: float = 1.0) -> float:
    return quadratic_fee(count, price, MAKER_K, multiplier)


def fee_points(price: float, k: float = TAKER_K) -> float:
    """Fee in probability-points for one contract (no rounding)."""
    if price <= 0 or price >= 1:
        return 0.0
    return k * price * (1.0 - price)
