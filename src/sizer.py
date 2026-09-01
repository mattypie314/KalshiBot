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
    max_risk_dollars: float = 3.00,
    preferred_risk_dollars: float = 2.00,
    last_loss_same_hour: bool = False,
    last_contracts: int | None = None,
) -> SizeDecision:
    if entry_price <= 0 or entry_price >= 1 or bankroll <= 0:
        return SizeDecision(0, 0.0, 0.0, True, "invalid price or bankroll")

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

    contracts = int(math.floor(risk_dollars / entry_price))
    if contracts < 1:
        if entry_price <= max_risk_dollars + 1e-12:
            contracts = 1
        else:
            return SizeDecision(0, 0.0, kelly_risk, True, "one contract exceeds max risk")

    if contracts * entry_price > max_risk_dollars + 1e-12:
        contracts = int(math.floor(max_risk_dollars / entry_price))
        if contracts < 1:
            return SizeDecision(0, 0.0, kelly_risk, True, "one contract exceeds max risk")

    if last_loss_same_hour and last_contracts is not None:
        if last_contracts < 1:
            return SizeDecision(0, 0.0, kelly_risk, True, "revenge: skip after a loss this hour")
        contracts = min(contracts, last_contracts)

    risk = contracts * entry_price
    if risk > max_risk_dollars + 1e-12:
        return SizeDecision(0, 0.0, kelly_risk, True, "sized risk exceeds hard cap")
    return SizeDecision(contracts, risk, kelly_risk, False)
