"""15m exit filters. Live oneshots flatten when these fire — not operator-only.

cash_out_99, then cash_out_95_time (bid ≥ 95¢ and ≤ 10m left), then +2¢ TP.
"""

from __future__ import annotations

from src.exits import (
    CASH_OUT_LABEL,
    DEFAULT_CASH_OUT_BID,
    DEFAULT_EARLY_CASH_OUT_BID,
    DEFAULT_EARLY_CASH_OUT_MINUTES,
    DEFAULT_TAKE_PROFIT_CENTS,
    EARLY_CASH_OUT_LABEL,
    TAKE_PROFIT_LABEL,
    exit_reason,
    held_side_bid,
    should_cash_out_99,
    should_cash_out_early,
    should_take_profit,
)

__all__ = [
    "CASH_OUT_LABEL",
    "DEFAULT_CASH_OUT_BID",
    "DEFAULT_EARLY_CASH_OUT_BID",
    "DEFAULT_EARLY_CASH_OUT_MINUTES",
    "DEFAULT_TAKE_PROFIT_CENTS",
    "EARLY_CASH_OUT_LABEL",
    "TAKE_PROFIT_LABEL",
    "exit_reason",
    "held_side_bid",
    "should_cash_out_99",
    "should_cash_out_early",
    "should_take_profit",
]
