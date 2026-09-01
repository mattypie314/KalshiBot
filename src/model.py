"""Zero-drift lognormal short-horizon fair probability."""

from __future__ import annotations

import math


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def hours_left(seconds: float) -> float | None:
    if seconds <= 0:
        return None
    return seconds / 3600.0


def model_z(spot: float, threshold: float, hourly_vol: float, hours_left: float) -> float:
    """z = ln(threshold / spot) / (hourly_vol * sqrt(hours_left))."""
    if spot <= 0 or threshold <= 0 or hourly_vol <= 0 or hours_left <= 0:
        return 0.0
    sigma = hourly_vol * math.sqrt(hours_left)
    if sigma <= 0:
        return 0.0
    return math.log(threshold / spot) / sigma


def fair_prob(spot: float, threshold: float, hourly_vol: float, hours_left: float) -> float:
    """Fair Yes: price finishes **above** the threshold. 1 - Φ(z)."""
    if hours_left <= 0:
        return 1.0 if spot > threshold else 0.0
    z = model_z(spot, threshold, hourly_vol, hours_left)
    return 1.0 - norm_cdf(z)


def fair_no(spot: float, threshold: float, hourly_vol: float, hours_left: float) -> float:
    return 1.0 - fair_prob(spot, threshold, hourly_vol, hours_left)


def required_move_pct(spot: float, threshold: float) -> float:
    if spot <= 0:
        return 0.0
    return abs(threshold - spot) / spot
