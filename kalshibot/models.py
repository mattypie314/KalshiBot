from __future__ import annotations

import math
import re
from dataclasses import dataclass

from kalshibot.money import clamp_prob, parse_dollars


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def digital_call_prob(spot: float, strike: float, years: float, vol: float) -> float:
    """Risk-neutral P(S_T >= K) with zero rates (cash-or-nothing digital)."""
    if spot <= 0 or strike <= 0 or vol <= 0:
        return 0.5
    if years <= 1e-8:
        return 0.99 if spot >= strike else 0.01
    sqrt_t = math.sqrt(years)
    d2 = (math.log(spot / strike) - 0.5 * vol * vol * years) / (vol * sqrt_t)
    return clamp_prob(norm_cdf(d2))


HOURS_PER_YEAR = 365.25 * 24


def annual_vol_from_hourly(hour_vol: float) -> float:
    return hour_vol * math.sqrt(HOURS_PER_YEAR)


def hours_to_years(hours: float) -> float:
    return max(hours, 1e-8) / HOURS_PER_YEAR


def distance_in_sigma(spot: float, strike: float, hour_vol: float, hours_left: float) -> float:
    """How many typical remaining moves (σ) spot is from the strike."""
    move = hour_vol * math.sqrt(max(hours_left, 1e-8))
    if spot <= 0 or strike <= 0 or move <= 0:
        return 0.0
    return math.log(spot / strike) / move


QUIET_HOUR_VOL = {
    "BTC": 0.0045,
    "ETH": 0.0060,
    "SOL": 0.0080,
}


def digital_put_prob(spot: float, strike: float, years: float, vol: float) -> float:
    return clamp_prob(1.0 - digital_call_prob(spot, strike, years, vol))


@dataclass(frozen=True)
class StrikeSpec:
    kind: str  # greater, greater_or_equal, less, less_or_equal, range, unknown
    floor: float | None = None
    cap: float | None = None


_ABOVE_RE = re.compile(
    r"(?:above|over|at\s+least|>=|≥)\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
_BELOW_RE = re.compile(
    r"(?:below|under|at\s+most|<=|≤)\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
_OR_ABOVE_RE = re.compile(
    r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s+or\s+above",
    re.IGNORECASE,
)
_TARGET_RE = re.compile(
    r"target(?:\s+price)?\s*:\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
_TICKER_STRIKE_RE = re.compile(r"-T([0-9]+(?:\.[0-9]+)?)$")
_RANGE_RE = re.compile(
    r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:to|–|-)\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


def _num(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.replace(",", "").replace("$", "").strip()
    return parse_dollars(value)


def parse_strike(market: dict) -> StrikeSpec:
    custom = market.get("custom_strike") or {}
    strike_type = str(market.get("strike_type") or custom.get("strike_type") or "").lower()
    floor = _num(custom.get("floor_strike"))
    cap = _num(custom.get("cap_strike"))

    subtitle = str(market.get("yes_sub_title") or "")
    title = str(market.get("title") or "")
    blob = f"{subtitle} {title}"
    ticker = str(market.get("ticker") or "")

    range_match = _RANGE_RE.search(subtitle) or _RANGE_RE.search(blob)
    if range_match and (floor is None or cap is None):
        low, high = _num(range_match.group(1)), _num(range_match.group(2))
        if low is not None and high is not None and high > low:
            floor = floor or low
            cap = cap or high
            strike_type = "range"

    if floor is None:
        for pattern in (_OR_ABOVE_RE, _ABOVE_RE, _TARGET_RE):
            match = pattern.search(blob)
            if match:
                floor = _num(match.group(1))
                if strike_type not in {"less", "less_or_equal"}:
                    strike_type = strike_type or "greater"
                break
    if cap is None:
        match = _BELOW_RE.search(blob)
        if match:
            cap = _num(match.group(1))
            strike_type = strike_type or "less"

    if floor is None:
        match = _TICKER_STRIKE_RE.search(ticker)
        if match:
            floor = _num(match.group(1))

    if strike_type in {"greater", "greater_or_equal", "at_least"}:
        return StrikeSpec("greater", floor=floor, cap=cap)
    if strike_type in {"less", "less_or_equal", "at_most"}:
        return StrikeSpec("less", floor=floor, cap=cap)
    if strike_type in {"between", "range"} or (floor is not None and cap is not None):
        return StrikeSpec("range", floor=floor, cap=cap)
    if floor is not None:
        return StrikeSpec("greater", floor=floor, cap=cap)
    if cap is not None:
        return StrikeSpec("less", floor=floor, cap=cap)
    return StrikeSpec("unknown", floor=floor, cap=cap)


def price_threshold_prob(
    spec: StrikeSpec,
    spot: float,
    years: float,
    vol: float,
) -> float | None:
    if spec.kind in {"greater", "greater_or_equal"} and spec.floor:
        return digital_call_prob(spot, spec.floor, years, vol)
    if spec.kind in {"less", "less_or_equal"} and spec.cap:
        return digital_put_prob(spot, spec.cap, years, vol)
    if spec.kind == "range" and spec.floor and spec.cap:
        return clamp_prob(
            digital_call_prob(spot, spec.floor, years, vol)
            - digital_call_prob(spot, spec.cap, years, vol)
        )
    if spec.floor:
        return digital_call_prob(spot, spec.floor, years, vol)
    return None


def devig_probs(mids: list[float]) -> list[float]:
    """Multiplicative (proportional) vigorish removal for mutually exclusive outcomes."""
    cleaned = [max(1e-6, min(0.999, m)) for m in mids]
    total = sum(cleaned)
    if total <= 0:
        n = len(cleaned) or 1
        return [1.0 / n] * len(cleaned)
    return [c / total for c in cleaned]


def confidence_from_spread(spread: float | None, volume_24h: float, model_used: bool) -> float:
    if spread is None:
        spread = 0.2
    spread_score = max(0.0, 1.0 - spread / 0.2)
    volume_score = min(1.0, math.log10(1.0 + volume_24h) / 5.0)
    model_bonus = 0.25 if model_used else 0.0
    return round(min(0.99, 0.2 + 0.45 * spread_score + 0.3 * volume_score + model_bonus), 3)
