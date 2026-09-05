"""15m exit filters. Live oneshots flatten when these fire — not operator-only.

cash_out_99 (held-side bid ≥ 99¢) runs ahead of the +2¢ take-profit.
"""

from __future__ import annotations

from src.exits import (
    CASH_OUT_LABEL,
    DEFAULT_CASH_OUT_BID,
    DEFAULT_TAKE_PROFIT_CENTS,
    TAKE_PROFIT_LABEL,
    exit_reason,
    held_side_bid,
    should_cash_out_99,
    should_take_profit,
)

__all__ = [
    "CASH_OUT_LABEL",
    "DEFAULT_CASH_OUT_BID",
    "DEFAULT_TAKE_PROFIT_CENTS",
    "TAKE_PROFIT_LABEL",
    "exit_reason",
    "held_side_bid",
    "should_cash_out_99",
    "should_take_profit",
]
