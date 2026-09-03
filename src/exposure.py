"""Cap open hourly crypto tickets so the timer cannot stack one-direction fades."""

from __future__ import annotations

import logging
from typing import Any

from src.executor import is_hourly_rest
from src.filters import Idea

logger = logging.getLogger(__name__)


def ticket_asset(ticker: str) -> str:
    text = str(ticker or "").upper()
    if text.startswith("KXETH"):
        return "ETH"
    if text.startswith("KXBTC"):
        return "BTC"
    return ""


def side_from_order(row: dict[str, Any]) -> str:
    side = str(row.get("side") or "").strip().lower()
    if side in {"yes", "no"}:
        return side.title()
    if side == "bid":
        return "Yes"
    if side == "ask":
        return "No"
    return ""


def open_hourly_tickets(client: Any, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Unsettled last ticket plus resting hourly orders."""
    found: dict[str, dict[str, Any]] = {}
    last = str(state.get("last_ticker") or "")
    if last:
        found[last] = {
            "ticker": last,
            "side": str(state.get("last_side") or ""),
            "asset": ticket_asset(last),
            "source": "state",
        }
    getter = getattr(client, "get_orders", None)
    if getter is None:
        return list(found.values())
    try:
        resting = getter(status="resting") or []
    except Exception as exc:  # noqa: BLE001
        logger.info("open-ticket list failed: %s", exc)
        return list(found.values())
    for row in resting:
        if not isinstance(row, dict) or not is_hourly_rest(row):
            continue
        ticker = str(row.get("ticker") or row.get("market_ticker") or "")
        if not ticker:
            continue
        prev = found.get(ticker, {})
        found[ticker] = {
            "ticker": ticker,
            "side": side_from_order(row) or str(prev.get("side") or ""),
            "asset": ticket_asset(ticker),
            "source": "rest",
            "order_id": row.get("order_id"),
        }
    return list(found.values())


def blocks_new_idea(open_tickets: list[dict[str, Any]], idea: Idea) -> str | None:
    """None if this idea may be added. Else a sit reason.

    Max 1 open hourly ticket. A second is allowed only if it is a different
    coin and the opposite side — not another 'price stays put' fade.

    Same-direction BTC+ETH Nos (the 2026-09-02 stacked losing card) sit.
    """
    if not open_tickets:
        return None
    if len(open_tickets) >= 2:
        tickers = ", ".join(row["ticker"] for row in open_tickets)
        return f"already 2 open hourly tickets ({tickers})"
    existing = open_tickets[0]
    same_coin = str(existing.get("asset") or "") == idea.market.asset
    same_dir = str(existing.get("side") or "").lower() == idea.side.lower()
    label = f"{existing.get('side') or '?'} {existing.get('ticker')}"
    if same_coin:
        return f"already open {label} on {idea.market.asset}"
    if same_dir:
        return f"already open {label} (same direction — correlated sit-still)"
    return None
