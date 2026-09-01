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


def _exposure_dollars(pos: dict[str, Any]) -> float | None:
    dollars = parse_dollars(pos.get("market_exposure_dollars"))
    if dollars is not None:
        return abs(dollars)
    legacy = parse_dollars(pos.get("market_exposure"))
    if legacy is None:
        traded = parse_dollars(pos.get("total_traded_dollars"))
        return abs(traded) if traded is not None else None
    if abs(legacy) > 1:
        return abs(legacy) / 100.0
    return abs(legacy)


def map_kalshi_order(order: dict[str, Any]) -> dict[str, Any] | None:
    ticker = str(order.get("ticker") or "").strip()
    if not ticker:
        return None
    remaining = None
    for key in ("remaining_count_fp", "remaining_count", "initial_count_fp", "count"):
        if order.get(key) is not None:
            remaining = _count(order.get(key))
            break
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


def map_kalshi_position(pos: dict[str, Any]) -> dict[str, Any] | None:
    ticker = str(pos.get("ticker") or "").strip()
    if not ticker:
        return None
    qty = _count(pos.get("position_fp") if pos.get("position_fp") is not None else pos.get("position"))
    if qty is None or qty == 0:
        return None
    side = "yes" if qty > 0 else "no"
    count = abs(qty)
    cost = _exposure_dollars(pos)
    fill = round(cost / count, 4) if cost is not None and count else None
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
        "pnl": parse_dollars(pos.get("realized_pnl_dollars")),
    }
