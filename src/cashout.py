"""Early cash-out when a held side already trades at 99¢.

Waiting for settlement after the book has already priced a near-certain win
risks a last-second flip. Both the hourly and 15m bots call this before new
entries: if the held-side live bid is ≥ 99¢ and we have a confirmed fill,
flatten immediately (take the bid / lift the ask — not post-only).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from src.journal import parse_count, ticker_in_fills
from src.kalshi_client import unwrap_order
from src.markets import _quote

logger = logging.getLogger(__name__)

CASHOUT_BID = 0.99
YES_EXIT_PRICE = 0.99
NO_EXIT_YES_PRICE = 0.01  # buy Yes @ 1¢ to flatten a No


def held_side_bid(side: str, yes_bid: float, no_bid: float) -> float:
    if str(side or "").strip().lower() == "yes":
        return float(yes_bid or 0.0)
    return float(no_bid or 0.0)


def should_cashout(
    side: str,
    yes_bid: float,
    no_bid: float,
    *,
    threshold: float = CASHOUT_BID,
) -> bool:
    return held_side_bid(side, yes_bid, no_bid) + 1e-12 >= float(threshold)


def exit_order_payload(
    ticker: str,
    side: str,
    contracts: int,
    *,
    exchange_index: int = -1,
    yes_price: float | None = None,
) -> dict[str, Any]:
    """Flatten a filled Yes/No position into a ~99¢ held-side bid.

    Yes → ask Yes @ 0.99 (sell into the 99¢ bid).
    No → bid Yes @ 0.01 (buy Yes to unwind No when No bid is 99¢).

    Not post-only: at 99¢ a maker rest would often cross or sit forever; the
    point of this path is to lock the win now.
    """
    count = max(1, int(contracts))
    if str(side or "").strip().lower() == "yes":
        book_side = "ask"
        price = float(YES_EXIT_PRICE if yes_price is None else yes_price)
    else:
        book_side = "bid"
        price = float(NO_EXIT_YES_PRICE if yes_price is None else yes_price)
    price = max(0.01, min(0.99, round(price, 4)))
    return {
        "ticker": str(ticker),
        "client_order_id": str(uuid.uuid4()),
        "side": book_side,
        "count": f"{count:.2f}",
        "price": f"{price:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "exchange_index": exchange_index,
    }


def filled_contracts_for(
    fills: list[dict[str, Any]] | None,
    ticker: str,
    *,
    side: str = "",
) -> float:
    """Contracts seen in fills for ticker. Side is accepted for call-site clarity only."""
    del side  # Kalshi fill side labels vary; size comes from state when ambiguous.
    want = str(ticker or "").upper()
    if not want or not fills:
        return 0.0
    total = 0.0
    for fill in fills:
        got = str(fill.get("ticker") or fill.get("market_ticker") or "").upper()
        if got != want:
            continue
        total += parse_count(
            fill.get("count")
            or fill.get("count_fp")
            or fill.get("contracts")
            or fill.get("filled_count")
        )
    return total


def quote_for_ticker(client: Any, ticker: str) -> tuple[float, float, float, float] | None:
    getter = getattr(client, "get_market", None)
    if getter is None or not ticker:
        return None
    try:
        raw = getter(ticker)
    except Exception as exc:  # noqa: BLE001
        logger.info("cashout quote failed for %s: %s", ticker, exc)
        return None
    if not isinstance(raw, dict):
        return None
    return _quote(raw)


def _safe_fills(client: Any) -> tuple[list[dict[str, Any]], bool]:
    getter = getattr(client, "get_fills", None)
    if getter is None:
        return [], False
    try:
        return list(getter(limit=100) or []), True
    except Exception as exc:  # noqa: BLE001
        logger.info("cashout fills unavailable: %s", exc)
        return [], False


def _cancel_resting_for_ticker(client: Any, ticker: str, *, rest_filter: Any | None = None) -> list[str]:
    canceled: list[str] = []
    getter = getattr(client, "get_orders", None)
    cancel = getattr(client, "cancel_order", None)
    if getter is None or cancel is None:
        return canceled
    try:
        resting = getter(status="resting") or []
    except Exception as exc:  # noqa: BLE001
        logger.info("cashout resting list failed: %s", exc)
        return canceled
    want = str(ticker or "").upper()
    for row in resting:
        if not isinstance(row, dict):
            continue
        row = unwrap_order(row)
        got = str(row.get("ticker") or row.get("market_ticker") or "").upper()
        if got != want:
            continue
        if rest_filter is not None and not rest_filter(row):
            continue
        order_id = str(row.get("order_id") or "")
        if not order_id:
            continue
        try:
            cancel(order_id, ticker=ticker or None)
            canceled.append(order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cashout cancel %s failed: %s", order_id, exc)
    return canceled


def manage_open_cashouts(
    client: Any,
    positions: list[dict[str, Any]],
    *,
    live: bool = False,
    exchange_index: int = -1,
    rest_filter: Any | None = None,
    threshold: float = CASHOUT_BID,
) -> list[dict[str, Any]]:
    """Check open tickets; cash out any whose held-side bid is already ≥ 99¢.

    Each position dict needs at least: ticker, side, contracts.
    Returns one result row per position that was inspected for cash-out.
    """
    if not positions:
        return []
    fills, fills_ok = _safe_fills(client)
    results: list[dict[str, Any]] = []

    for pos in positions:
        ticker = str(pos.get("ticker") or "")
        side = str(pos.get("side") or "")
        planned = max(1, int(parse_count(pos.get("contracts")) or 1))
        row: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": "skip",
            "reason": "",
            "payload": None,
            "placed": None,
            "canceled": [],
        }
        if not ticker or side.lower() not in {"yes", "no"}:
            row["reason"] = "missing ticker/side"
            results.append(row)
            continue

        quotes = quote_for_ticker(client, ticker)
        if quotes is None:
            row["reason"] = "no quote"
            results.append(row)
            continue
        yes_bid, yes_ask, no_bid, no_ask = quotes
        bid = held_side_bid(side, yes_bid, no_bid)
        row["held_bid"] = bid
        row["yes_bid"] = yes_bid
        row["no_bid"] = no_bid
        if not should_cashout(side, yes_bid, no_bid, threshold=threshold):
            row["reason"] = f"held bid {bid:.2f} < {threshold:.2f}"
            results.append(row)
            continue

        if fills_ok and not ticker_in_fills(fills, ticker):
            row["reason"] = "no fill yet — not cashing out a resting entry"
            results.append(row)
            continue
        if not fills_ok:
            row["reason"] = "fills unavailable — refuse cash-out without fill proof"
            results.append(row)
            continue

        filled = filled_contracts_for(fills, ticker, side=side)
        contracts = max(1, int(filled) if filled > 0 else planned)
        payload = exit_order_payload(
            ticker,
            side,
            contracts,
            exchange_index=exchange_index,
            yes_price=YES_EXIT_PRICE if side.lower() == "yes" else NO_EXIT_YES_PRICE,
        )
        row["payload"] = payload
        row["contracts"] = contracts
        row["action"] = "would_cashout"
        row["reason"] = f"held bid {bid:.2f} ≥ {threshold:.2f} — cash out early"

        print(
            f"CASHOUT {ticker} {side}: held bid {bid:.2f} "
            f"(book Yes {yes_bid:.2f}/{yes_ask:.2f} No {no_bid:.2f}/{no_ask:.2f}) "
            f"→ {'LIVE' if live else 'dry'} exit x{contracts}",
            flush=True,
        )

        if not live:
            results.append(row)
            continue

        create = getattr(client, "create_order", None) or getattr(client, "create_order_v2", None)
        if create is None:
            row["action"] = "error"
            row["reason"] = "client has no create_order"
            results.append(row)
            continue

        row["canceled"] = _cancel_resting_for_ticker(client, ticker, rest_filter=rest_filter)
        try:
            placed = unwrap_order(create(payload))
            row["placed"] = placed
            row["action"] = "cashed_out"
            print(
                f"LIVE cashout {ticker} {payload['side']} {payload['price']} "
                f"x {payload['count']} order_id={placed.get('order_id') or '?'}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            row["action"] = "error"
            row["reason"] = str(exc)
            logger.error("cashout create_order failed for %s: %s", ticker, exc)
            print(f"LIVE cashout failed {ticker}: {exc}", flush=True)

        results.append(row)
    return results
