"""Cap open crypto tickets at one per asset (BTC and ETH both OK when they pass)."""

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


def row_asset(row: dict[str, Any] | None) -> str:
    """Coin on a ticket/rest row — explicit asset, else ticker prefix."""
    if not isinstance(row, dict):
        return ""
    raw = str(row.get("asset") or "").strip().upper()
    if raw in {"BTC", "ETH"}:
        return raw
    return ticket_asset(str(row.get("ticker") or row.get("market_ticker") or ""))


def select_ideas_per_asset(
    ideas: list[Idea],
    *,
    max_per_asset: int = 1,
    max_ideas: int | None = None,
) -> tuple[list[Idea], list[Idea]]:
    """Keep the best already-ranked idea per coin.

    The real rule is 1 per asset (BTC and ETH both OK), not a global top-N
    that could pick two BTC strikes. ``max_ideas`` is only a total cap.
    """
    if max_per_asset < 1:
        return [], list(ideas)
    cap = len(ideas) if max_ideas is None else max(0, max_ideas)
    chosen: list[Idea] = []
    extra: list[Idea] = []
    counts: dict[str, int] = {}
    for idea in ideas:
        asset = str(idea.market.asset or "").upper()
        if counts.get(asset, 0) < max_per_asset and len(chosen) < cap:
            chosen.append(idea)
            counts[asset] = counts.get(asset, 0) + 1
        else:
            extra.append(idea)
    return chosen, extra


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

    Max 1 open hourly ticket per coin (never 2 BTC or 2 ETH). A second
    ticket is allowed on the other coin regardless of Yes/No side.
    Hard cap 2 open hourly tickets.
    """
    if not open_tickets:
        return None
    if len(open_tickets) >= 2:
        tickers = ", ".join(row["ticker"] for row in open_tickets)
        return f"already 2 open hourly tickets ({tickers})"
    existing = open_tickets[0]
    existing_asset = row_asset(existing)
    same_coin = existing_asset == idea.market.asset
    label = f"{existing.get('side') or '?'} {existing.get('ticker')}"
    if same_coin:
        return f"already open {label} on {idea.market.asset}"
    return None
