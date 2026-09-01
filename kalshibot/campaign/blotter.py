from __future__ import annotations

from typing import Any

from kalshibot.money import parse_dollars


def quote_dollars(value: object) -> float | None:
    """Kalshi quotes as dollar strings ('0.89') or legacy integer cents (89)."""
    n = parse_dollars(value)
    if n is None:
        return None
    if n > 1.0:
        return n / 100.0
    return n


def _count(value: object) -> float | None:
    return parse_dollars(value)


def first_number(*values: object) -> float | None:
    for value in values:
        if value is None or value == "":
            continue
        n = _count(value)
        if n is not None:
            return n
    return None


def _exposure_dollars(pos: dict[str, Any]) -> float | None:
    dollars = first_number(
        pos.get("market_exposure_dollars"),
        pos.get("event_exposure_dollars"),
        pos.get("total_traded_dollars"),
        pos.get("total_cost_dollars"),
    )
    if dollars is not None:
        return abs(dollars)
    legacy = first_number(pos.get("market_exposure"), pos.get("total_traded"))
    if legacy is None:
        return None
    if abs(legacy) > 1:
        return abs(legacy) / 100.0
    return abs(legacy)


def map_kalshi_order(order: dict[str, Any]) -> dict[str, Any] | None:
    ticker = str(order.get("ticker") or order.get("market_ticker") or "").strip()
    if not ticker:
        return None
    remaining = first_number(
        order.get("remaining_count_fp"),
        order.get("remaining_count"),
        order.get("initial_count_fp"),
        order.get("count_fp"),
        order.get("count"),
    )
    if remaining is not None and remaining <= 0:
        return None
    side = str(order.get("outcome_side") or order.get("side") or "").lower()
    if side not in {"yes", "no"}:
        book = str(order.get("book_side") or "").lower()
        if book == "bid":
            side = "yes"
        elif book == "ask":
            side = "no"
        else:
            return None
    yes_px = quote_dollars(order.get("yes_price_dollars"))
    if yes_px is None:
        yes_px = quote_dollars(order.get("yes_price"))
    no_px = quote_dollars(order.get("no_price_dollars"))
    if no_px is None:
        no_px = quote_dollars(order.get("no_price"))
    if no_px is None and yes_px is not None:
        no_px = max(0.0, 1.0 - yes_px)
    if yes_px is None and no_px is not None:
        yes_px = max(0.0, 1.0 - no_px)
    price = yes_px if side == "yes" else no_px
    count = remaining if remaining is not None else 0.0
    cost = None if price is None else round(price * count, 4)
    return {
        "id": str(order.get("order_id") or ticker),
        "order_id": order.get("order_id"),
        "loop": "kalshi",
        "ticker": ticker,
        "title": ticker,
        "side": side,
        "price": price,
        "fill": price,
        "count": count,
        "cost": cost,
        "status": "open",
        "source": "kalshi",
        "paper": False,
    }


def _pnl_dollars(pos: dict[str, Any]) -> float | None:
    raw = pos.get("realized_pnl_dollars")
    if raw not in (None, ""):
        return parse_dollars(raw)
    cents = pos.get("realized_pnl")
    if isinstance(cents, bool) or not isinstance(cents, (int, float)):
        return None
    return float(cents) / 100.0


def map_kalshi_position(pos: dict[str, Any]) -> dict[str, Any] | None:
    ticker = str(pos.get("ticker") or pos.get("market_ticker") or pos.get("event_ticker") or "").strip()
    if not ticker:
        return None
    qty = first_number(pos.get("position_fp"), pos.get("position"), pos.get("position_count"))
    if qty is None or qty == 0:
        return None
    side = "yes" if qty > 0 else "no"
    count = abs(qty)
    cost = _exposure_dollars(pos)
    fill = round(cost / count, 4) if cost is not None and count else None
    pnl = _pnl_dollars(pos)
    return {
        "id": ticker,
        "loop": "kalshi",
        "ticker": ticker,
        "title": ticker,
        "side": side,
        "fill": fill,
        "price": fill,
        "count": count,
        "cost": round(cost, 4) if cost is not None else None,
        "status": "open",
        "source": "kalshi",
        "paper": False,
        "pnl": round(pnl, 4) if pnl is not None else None,
    }


def positions_from_fills(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Net recent Kalshi fills into open lots when /portfolio/positions is empty."""
    books: dict[str, dict[str, float]] = {}
    for fill in fills:
        ticker = str(fill.get("ticker") or fill.get("market_ticker") or "").strip()
        if not ticker:
            continue
        side = str(fill.get("outcome_side") or fill.get("side") or "").lower()
        count = first_number(fill.get("count_fp"), fill.get("count"))
        if count is None or count == 0 or side not in {"yes", "no"}:
            continue
        action = str(fill.get("action") or "").lower()
        if fill.get("outcome_side"):
            signed = count if side == "yes" else -count
        elif action == "sell":
            signed = -count if side == "yes" else count
        else:
            signed = count if side == "yes" else -count
        yes_px = quote_dollars(fill.get("yes_price_dollars"))
        if yes_px is None:
            yes_px = quote_dollars(fill.get("yes_price"))
        no_px = quote_dollars(fill.get("no_price_dollars"))
        if no_px is None:
            no_px = quote_dollars(fill.get("no_price"))
        px = yes_px if side == "yes" else no_px
        if px is None and yes_px is not None:
            px = max(0.0, 1.0 - yes_px) if side == "no" else yes_px
        cost = abs(count) * px if px is not None else 0.0
        row = books.setdefault(ticker, {"qty": 0.0, "cost": 0.0})
        row["qty"] += signed
        row["cost"] += cost if action != "sell" else -cost
    mapped: list[dict[str, Any]] = []
    for ticker, row in books.items():
        qty = row["qty"]
        if abs(qty) < 0.005:
            continue
        mapped.append(
            map_kalshi_position(
                {
                    "ticker": ticker,
                    "position_fp": f"{qty:.2f}",
                    "market_exposure_dollars": f"{abs(row['cost']):.4f}",
                }
            )
        )
    return [row for row in mapped if row]
