"""Hourly loop: live KXBTC15M tape, 6%+ net edge, 3–5% limit only.

Not the 1-hour KXBTC/KXBNB books. Maker (74–93¢ last 3 min) stays separate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from kalshibot.campaign.rules import join_price
from kalshibot.fees import TAKER_K, fee_points

HOURLY_SERIES = "KXBTC15M"
MIN_NET_EDGE = 0.06
SIZE_MIN = 0.03
SIZE_MAX = 0.05
SIZE_TYPICAL = 0.04
HOURLY_INTERVAL_SECONDS = 3600
NO_EDGE = "No actionable edge."


@dataclass(frozen=True)
class HourlyTape:
    passed: bool
    ticker: str
    minutes: float
    strike: float
    spot: float
    yes_mid: float
    fair: float
    net_edge: float
    side: str
    join_price: float
    skip: str | None = None


def hourly_stake(bankroll: float, room: float) -> float:
    if bankroll <= 0 or room <= 0:
        return 0.0
    frac = max(SIZE_MIN, min(SIZE_MAX, SIZE_TYPICAL))
    return min(frac * bankroll, room)


def pick_atm(items: list[dict]) -> dict | None:
    """The current 15m BTC contract: nearest strike to live spot."""
    live = [row for row in items if row.get("spot") and row.get("strike")]
    if not live:
        return None

    def distance(row: dict) -> float:
        spot = float(row["spot"])
        strike = float(row["strike"])
        if spot <= 0 or strike <= 0:
            return 1e9
        return abs(math.log(spot / strike))

    return min(live, key=distance)


def grade_tape(
    *,
    ticker: str,
    model_yes: float,
    yes_bid: float,
    yes_ask: float,
    secs_left: float,
    strike: float,
    spot: float,
) -> HourlyTape:
    mid = (yes_bid + yes_ask) / 2.0 if yes_bid > 0 and yes_ask >= yes_bid else 0.0
    if mid <= 0 or yes_ask < yes_bid:
        return HourlyTape(False, ticker, secs_left / 60.0, strike, spot, 0.0, model_yes, 0.0, "yes", 0.0, "unusable book")
    yes_edge = model_yes - mid
    if yes_edge >= 0:
        side = "yes"
        market = mid
        model = model_yes
    else:
        side = "no"
        market = 1.0 - mid
        model = 1.0 - model_yes
    net = (model - market) - fee_points(market, TAKER_K)
    join = join_price(side, yes_bid, yes_ask)
    minutes = secs_left / 60.0
    if net < MIN_NET_EDGE:
        return HourlyTape(
            False, ticker, minutes, strike, spot, mid, model_yes, net, side, join, f"net {100 * net:.1f}% < 6%"
        )
    return HourlyTape(True, ticker, minutes, strike, spot, mid, model_yes, net, side, join)


def format_hourly(tape: HourlyTape, *, size_line: str | None = None) -> str:
    if not tape.passed:
        return NO_EDGE
    lines = [
        f"Market: {tape.ticker}",
        f"Minutes left / strike / spot: {tape.minutes:.0f}m / {tape.strike:g} / {tape.spot:g}",
        f"Yes mid vs model fair: {tape.yes_mid:.2f} vs {tape.fair:.2f}",
        f"Edge after fees: {100 * tape.net_edge:+.1f}%",
        "Filter: Pass",
        f"Size if pass: {size_line or '3–5% of bankroll, limit only'}",
    ]
    return "\n".join(lines)
