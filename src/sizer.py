"""Fractional Kelly (0.25x) with dollar and percent hard caps."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SizeDecision:
    contracts: int
    risk_dollars: float
    kelly_risk: float
    skip: bool
    reason: str = ""


def clamp_price(price: float) -> float:
    return max(0.0, min(1.0, float(price)))


def yes_book_price(side: str, labeled_limit: float) -> float:
    """Yes-book price the executor posts for a labeled-side limit.

    Buy-Yes posts ``labeled_limit``. Sell-Yes (No idea) posts ``1 - labeled_limit``.
    """
    px = clamp_price(labeled_limit)
    if str(side or "").strip().lower() == "yes":
        return round(px, 4)
    return round(1.0 - px, 4)


def maker_cost_per_contract(
    side: str,
    *,
    labeled_limit: float | None = None,
    yes_book: float | None = None,
) -> float:
    """Dollars Kalshi locks on one maker contract.

    Buy-Yes at Y costs Y. Sell-Yes at Y costs ``1 - Y`` (the No complement).
    A labeled No limit is already that complement, so ``labeled_limit`` is the
    cost for both Yes and No once the idea stores the side's own price.
    """
    if yes_book is not None:
        yes_px = clamp_price(yes_book)
        if str(side or "").strip().lower() == "yes":
            return round(yes_px, 4)
        return round(1.0 - yes_px, 4)
    if labeled_limit is None:
        return 0.0
    return round(clamp_price(labeled_limit), 4)


def labeled_limit_from_yes_book(side: str, yes_book: float) -> float:
    """Turn a Yes-book join price into the labeled-side limit the executor expects.

    15m ``join_price`` is already on the Yes book (Yes joins the Yes bid; No
    joins the Yes ask). Storing that ask as a No limit would size as if each
    contract cost 24¢ while Kalshi locks 76¢ to sell Yes at 24¢.
    """
    return maker_cost_per_contract(side, yes_book=yes_book)


def economic_risk_dollars(contracts: int | float, cost_per: float) -> float:
    return round(abs(float(contracts)) * float(cost_per), 4)


def kelly_bankroll_fraction(p_hat: float, price: float) -> float:
    """Full Kelly fraction of bankroll to spend on a binary buy-at-`price`."""
    if price <= 0 or price >= 1 or p_hat <= price:
        return 0.0
    return (p_hat - price) / (1.0 - price)


def size_idea(
    *,
    bankroll: float,
    entry_price: float,
    p_hat: float,
    kelly_mult: float = 0.25,
    max_risk_pct: float = 0.05,
    max_risk_dollars: float = 2.00,
    preferred_risk_dollars: float = 1.75,
    last_loss_same_hour: bool = False,
    last_contracts: int | None = None,
    cost_price: float | None = None,
) -> SizeDecision:
    """Size contracts from true dollar risk (``cost_price``), never past the caps.

    ``entry_price`` is the labeled side's implied probability for Kelly.
    ``cost_price`` is dollars Kalshi actually locks per contract (maker fill
    cost). Defaults to ``entry_price``, which is correct once a No idea stores
    the No / sell-Yes complement rather than the cheap Yes-book join.
    """
    if entry_price <= 0 or entry_price >= 1 or bankroll <= 0:
        return SizeDecision(0, 0.0, 0.0, True, "invalid price or bankroll")
    cost = float(entry_price if cost_price is None else cost_price)
    if cost <= 0 or cost >= 1:
        return SizeDecision(0, 0.0, 0.0, True, "invalid cost price")

    full = kelly_bankroll_fraction(p_hat, entry_price)
    kelly_risk = max(0.0, kelly_mult * full * bankroll)
    risk_dollars = min(
        kelly_risk,
        max_risk_pct * bankroll,
        max_risk_dollars,
        preferred_risk_dollars,
    )
    if risk_dollars <= 0:
        return SizeDecision(0, 0.0, kelly_risk, True, "kelly/risk cap is zero")

    contracts = int(math.floor(risk_dollars / cost))
    if contracts < 1:
        if cost <= max_risk_dollars + 1e-12:
            contracts = 1
        else:
            return SizeDecision(0, 0.0, kelly_risk, True, "one contract exceeds max risk")

    if contracts * cost > max_risk_dollars + 1e-12:
        contracts = int(math.floor(max_risk_dollars / cost))
        if contracts < 1:
            return SizeDecision(0, 0.0, kelly_risk, True, "one contract exceeds max risk")

    if last_loss_same_hour and last_contracts is not None:
        if last_contracts < 1:
            return SizeDecision(0, 0.0, kelly_risk, True, "revenge: skip after a loss this hour")
        contracts = min(contracts, last_contracts)

    risk = contracts * cost
    if risk > max_risk_dollars + 1e-12:
        return SizeDecision(0, 0.0, kelly_risk, True, "sized risk exceeds hard cap")
    return SizeDecision(contracts, risk, kelly_risk, False)
