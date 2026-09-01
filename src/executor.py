"""Limit-order executor. Default path is dry-run — never calls create_order."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.filters import Idea

logger = logging.getLogger(__name__)


def _order_payload(idea: Idea, run_id: str) -> dict[str, Any]:
    price_cents = int(round(idea.limit_price * 100))
    side = idea.side.lower()
    body: dict[str, Any] = {
        "ticker": idea.market.ticker,
        "client_order_id": f"hourly-{run_id}-{idea.market.ticker}-{side}"[:64],
        "side": side,
        "action": "buy",
        "count": idea.contracts,
        "type": "limit",
        "time_in_force": "good_till_canceled",
        "post_only": bool(idea.post_maker),
    }
    if side == "yes":
        body["yes_price"] = price_cents
    else:
        body["no_price"] = price_cents
    if idea.market.exchange_index is not None:
        body["exchange_index"] = idea.market.exchange_index
    return body


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
        "ts": datetime.now(timezone.utc).isoformat(),
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
            cid = str(row.get("client_order_id") or "")
            if not cid.startswith("hourly-"):
                continue
            order_id = str(row.get("order_id") or "")
            ticker = str(row.get("ticker") or "")
            still_wanted = any(p.get("ticker") == ticker for p in orders)
            if still_wanted:
                continue
            try:
                client.cancel_order(order_id, ticker=ticker or None)
                result["canceled"].append({"order_id": order_id, "ticker": ticker})
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"cancel {order_id}: {exc}")

    create = getattr(client, "create_order", None) or getattr(client, "create_order_v2", None)
    if create is None:
        raise RuntimeError("client has no create_order")

    for payload in orders:
        try:
            placed = create(payload)
            result["placed"].append(placed)
            logger.info("placed order %s", placed.get("order_id") or placed)
            try:
                open_orders = client.get_orders(status="resting")
                logger.info("open orders after place: %s", [o.get("order_id") for o in open_orders])
            except Exception as exc:  # noqa: BLE001
                logger.warning("fetch open orders failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("create_order failed: %s", exc)
            result["errors"].append(str(exc))

    (dest / "last_run.json").write_text(json.dumps(result, indent=2, default=str))
    return result
