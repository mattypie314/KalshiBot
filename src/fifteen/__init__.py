"""15-minute BTC/ETH Kalshi edge-loop bot (sibling to the hourly scanner)."""

from src.fifteen.edge import (
    CPI_DATES,
    FOMC_DATES,
    FifteenDecision,
    enough_room,
    fifteen_session_date,
    fifteen_stake,
    fifteen_stopped,
    fifteen_window_id,
    fifteen_window_start,
    fifteen_working,
    half_sigma_move,
    in_fifteen_entry_window,
    in_fifteen_revenge,
    in_fifteen_settlement,
    labeled_join_price,
    net_edge_vs_join,
    news_blackout,
    next_et_midnight,
    pass_fail,
    record_fifteen_result,
    revenge_until_after_loss,
    seconds_until_entry_window,
    strike_decided,
)

__all__ = [
    "CPI_DATES",
    "FOMC_DATES",
    "FifteenDecision",
    "cli",
    "enough_room",
    "fifteen_session_date",
    "fifteen_stake",
    "fifteen_stopped",
    "fifteen_window_id",
    "fifteen_window_start",
    "fifteen_working",
    "half_sigma_move",
    "in_fifteen_entry_window",
    "in_fifteen_revenge",
    "in_fifteen_settlement",
    "labeled_join_price",
    "main",
    "net_edge_vs_join",
    "news_blackout",
    "next_et_midnight",
    "pass_fail",
    "record_fifteen_result",
    "revenge_until_after_loss",
    "seconds_until_entry_window",
    "strike_decided",
]


def __getattr__(name: str):
    if name in {"main", "cli"}:
        from src.fifteen.main import cli, main

        return main if name == "main" else cli
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
