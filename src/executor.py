"""Limit-order executor. Default path is dry-run — never calls create_order."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from src.clock import format_et
from src.filters import Idea, maker_limit
from src.kalshi_client import unwrap_order
from src.markets import _quote

logger = logging.getLogger(__name__)

# Daily BTC/ETH threshold books this scanner trades. Not 15m campaign rests.
HOURLY_SERIES = frozenset({"KXBTCD", "KXETHD"})
TICK = 0.01
MAX_CROSS_RETRIES = 3


def series_code(ticker: str) -> str:
    return str(ticker or "").upper().split("-", 1)[0]


def is_hourly_rest(row: dict[str, Any]) -> bool:
    """True for this scanner's rests, including UUID client_order_ids.

    Kalshi V2 requires a UUID client_order_id, so we cannot tag orders with an
    `hourly-` prefix anymore. Identify them by series (or leftover hourly- ids).
    """
    ticker = str(row.get("ticker") or row.get("market_ticker") or "")
    series = str(row.get("series_ticker") or "")
    if series in HOURLY_SERIES or series_code(ticker) in HOURLY_SERIES:
        return True
    return str(row.get("client_order_id") or "").startswith("hourly-")


def _order_payload(idea: Idea, run_id: str) -> dict[str, Any]:
    """Kalshi V2 CreateOrder: count and price are strings. Side is bid/ask on the Yes book."""
    side = idea.side.lower()
    if side == "yes":
        book_side = "bid"
        yes_price = idea.limit_price
    else:
        book_side = "ask"
        yes_price = max(0.0, min(1.0, 1.0 - idea.limit_price))
    return {
        "ticker": idea.market.ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": book_side,
        "count": f"{int(idea.contracts):.2f}",
        "price": f"{yes_price:.4f}",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": bool(idea.post_maker),
        "exchange_index": -1,
    }


def is_post_only_cross(exc: BaseException) -> bool:
    text = str(exc).lower().replace("_", " ").replace("-", " ")
    return "post only cross" in text


def step_more_passive(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Move a post-only Yes-book order one tick away from the inside. Never lifts."""
    try:
        price = float(payload["price"])
    except (KeyError, TypeError, ValueError):
        return None
    side = str(payload.get("side") or "").lower()
    if side == "bid":
        nxt = round(price - TICK, 4)
        if nxt < TICK - 1e-9:
            return None
    elif side == "ask":
        nxt = round(price + TICK, 4)
        if nxt > 1.0 - TICK + 1e-9:
            return None
    else:
        return None
    out = dict(payload)
    out["price"] = f"{nxt:.4f}"
    out["client_order_id"] = str(uuid.uuid4())
    out["post_only"] = True
    return out


def refresh_maker_payload(idea: Idea, payload: dict[str, Any], client: Any) -> dict[str, Any]:
    """Reprice from a live GET /markets/{ticker} so a stale 2¢ spread does not cross."""
    getter = getattr(client, "get_market", None)
    if getter is None or not idea.post_maker:
        return payload
    try:
        raw = getter(idea.market.ticker)
    except Exception as exc:  # noqa: BLE001
        logger.warning("live quote refresh failed for %s: %s", idea.market.ticker, exc)
        return payload
    if not isinstance(raw, dict):
        return payload
    yes_bid, yes_ask, no_bid, no_ask = _quote(raw)
    side = idea.side
    if side.lower() == "yes":
        bid, ask = yes_bid, yes_ask
    else:
        bid, ask = no_bid, no_ask
    if bid <= 0 or ask <= 0:
        return payload
    limit = maker_limit(side, bid, ask)
    if limit is None:
        return payload
    refreshed = dict(payload)
    if side.lower() == "yes":
        yes_price = limit
    else:
        yes_price = max(0.0, min(1.0, 1.0 - limit))
    refreshed["price"] = f"{yes_price:.4f}"
    refreshed["post_only"] = True
    if refreshed["price"] != payload.get("price"):
        refreshed["client_order_id"] = str(uuid.uuid4())
        print(
            f"LIVE requote {idea.market.ticker}: {payload.get('side')} "
            f"{payload.get('price')} -> {refreshed['price']} "
            f"(book {bid:.2f}/{ask:.2f})",
            flush=True,
        )
    return refreshed


def place_post_only(create: Any, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """POST a post-only order. On post-only-cross, step one tick more passive and retry."""
    attempt = dict(payload)
    last_exc: Exception | None = None
    for _ in range(MAX_CROSS_RETRIES + 1):
        try:
            return unwrap_order(create(attempt)), attempt
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not is_post_only_cross(exc):
                raise
            nxt = step_more_passive(attempt)
            if nxt is None:
                raise
            print(
                f"LIVE post-only cross at {attempt['price']}; retry {nxt['price']} (still maker)",
                flush=True,
            )
            attempt = nxt
    assert last_exc is not None
    raise last_exc


def execute_ideas(
    ideas: list[Idea],
    *,
    client: Any,
    artifacts_dir: str | Path,
    live: bool = False,
    confirm_live: bool = False,
    run_id: str | None = None,
    cancel_stale: bool = True,
) -> dict[str, Any]:
    run_id = run_id or uuid.uuid4().hex[:12]
    dest = Path(artifacts_dir)
    dest.mkdir(parents=True, exist_ok=True)

    go_live = bool(live and confirm_live)
    orders = [_order_payload(idea, run_id) for idea in ideas]
    result: dict[str, Any] = {
        "run_id": run_id,
        "ts": format_et(),
        "mode": "live" if go_live else "dry_run",
        "orders": orders,
        "placed": [],
        "canceled": [],
        "errors": [],
    }

    if not go_live:
        path = dest / "last_run.json"
        path.write_text(json.dumps(result, indent=2, default=str))
        for payload in orders:
            logger.info("DRY-RUN order %s", json.dumps(payload, default=str))
            print(json.dumps({"dry_run_order": payload}, indent=2))
        return result

    if cancel_stale and hasattr(client, "get_orders"):
        try:
            resting = client.get_orders(status="resting")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not list resting orders: %s", exc)
            resting = []
        for row in resting:
            row = unwrap_order(row)
            if not is_hourly_rest(row):
                continue
            order_id = str(row.get("order_id") or "")
            ticker = str(row.get("ticker") or row.get("market_ticker") or "")
            still_wanted = any(p.get("ticker") == ticker for p in orders)
            if still_wanted or not order_id:
                continue
            try:
                client.cancel_order(order_id, ticker=ticker or None)
                result["canceled"].append({"order_id": order_id, "ticker": ticker})
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"cancel {order_id}: {exc}")

    create = getattr(client, "create_order", None) or getattr(client, "create_order_v2", None)
    if create is None:
        raise RuntimeError("client has no create_order")

    final_orders: list[dict[str, Any]] = []
    for idea, payload in zip(ideas, orders, strict=True):
        working = refresh_maker_payload(idea, payload, client) if idea.post_maker else payload
        try:
            placed, working = place_post_only(create, working)
            result["placed"].append(placed)
            print(
                f"LIVE placed {working.get('ticker')} {working.get('side')} "
                f"{working.get('price')} x {working.get('count')} "
                f"order_id={placed.get('order_id') or '?'}",
                flush=True,
            )
            logger.info("placed order %s", placed.get("order_id") or placed)
            try:
                open_orders = client.get_orders(status="resting")
                logger.info("open orders after place: %s", [o.get("order_id") for o in open_orders])
            except Exception as exc:  # noqa: BLE001
                logger.warning("fetch open orders failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("create_order failed: %s payload=%s", exc, json.dumps(working))
            msg = str(exc)
            result["errors"].append(msg)
            if is_post_only_cross(exc):
                print(
                    "LIVE skipped: post-only would take. Did not lift. "
                    "Re-run ./kb live --prod if the book is still interesting.",
                    flush=True,
                )
            else:
                print(f"LIVE order failed: {msg}", flush=True)
        final_orders.append(working)
    result["orders"] = final_orders

    (dest / "last_run.json").write_text(json.dumps(result, indent=2, default=str))
    return result
