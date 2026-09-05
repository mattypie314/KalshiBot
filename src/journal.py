"""Append-only trade log. Close-strike / buy-No kill switch lives here."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.clock import format_et, same_et_day, to_et

CLOSE_BUCKET_PCT = 0.01
KILL_MIN_TRADES = 3
FILLED_STATUSES = frozenset({"filled", "partial"})
TERMINAL_RESULTS = frozenset({"win", "loss", "unfilled"})
TURBO_LABEL = "Turbo / FORCE_NEAR_RULE"


CASH_OUT_LABEL = "cash_out_99"


def apply_exit_fields(
    row: dict[str, Any],
    *,
    reason: str,
    exit_price: float,
    order_id: str = "",
) -> dict[str, Any]:
    """Label a flatten (cash_out_99 / take_profit) on a journal row."""
    row["exit_reason"] = reason
    row["exit_label"] = reason
    row["exit_price"] = round(float(exit_price), 4)
    if reason:
        row["label"] = reason
    if order_id:
        row["exit_order_id"] = order_id
    row["exit_ts"] = format_et()
    return row


def forced_ticket_fields(*, forced: bool = False, force_near_rule: bool = False) -> dict[str, Any]:
    """Label a Turbo Mode ticket in journal / last_run / trade_log / paper."""
    on = bool(forced or force_near_rule)
    return {
        "forced": on,
        "turbo": on,
        "force_near_rule": on,
        "label": TURBO_LABEL if on else "",
    }


def strike_distance_pct(spot: float, threshold: float) -> float:
    if spot <= 0:
        return 0.0
    return abs(threshold - spot) / spot


def trade_bucket(side: str, distance_pct: float, close_pct: float = CLOSE_BUCKET_PCT) -> str:
    near = distance_pct < close_pct
    if str(side).lower() == "no":
        return "close_no" if near else "far_no"
    return "close_yes" if near else "far_yes"


def estimate_pnl(*, won: bool, contracts: int, entry_price: float, risk_dollars: float) -> float:
    if won:
        return round(max(contracts, 0) * max(0.0, 1.0 - entry_price), 4)
    return round(-abs(risk_dollars), 4)


def load_trades(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def write_trades(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, default=str) + "\n" for row in rows))


def append_trade(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(row, default=str) + "\n")


def new_trade_row(
    *,
    ticker: str,
    asset: str,
    side: str,
    strike: float,
    spot: float,
    minutes_left: float,
    fair: float,
    kalshi_price: float,
    limit_price: float,
    contracts: int,
    risk_dollars: float,
    hourly_vol: float,
    source: str,
    order_id: str = "",
    client_order_id: str = "",
    fill_status: str = "resting",
    filled_contracts: float = 0.0,
    forced: bool = False,
    force_near_rule: bool = False,
) -> dict[str, Any]:
    distance = strike_distance_pct(spot, strike)
    row = {
        "ts": format_et(),
        "ts_iso": to_et().isoformat(),
        "ticker": ticker,
        "asset": asset,
        "side": side,
        "strike": strike,
        "spot": spot,
        "distance_pct": round(distance, 6),
        "minutes_left": round(minutes_left, 2),
        "fair": round(fair, 4),
        "model_pct": round(fair, 4),
        "kalshi_price": round(kalshi_price, 4),
        "limit_price": round(limit_price, 4),
        "contracts": contracts,
        "risk_dollars": round(risk_dollars, 4),
        "hourly_vol": hourly_vol,
        "spot_source": source,
        "bucket": trade_bucket(side, distance),
        "order_id": order_id,
        "client_order_id": client_order_id,
        "fill_status": fill_status,
        "filled_contracts": filled_contracts,
        "settlement_result": None,
        "result": "pending",
        "pnl": None,
    }
    row.update(forced_ticket_fields(forced=forced, force_near_rule=force_near_rule))
    return row


def resolve_pending(
    rows: list[dict[str, Any]],
    get_market: Callable[[str], dict[str, Any] | None],
    result_is_loss: Callable[[dict[str, Any], str], bool | None],
    *,
    fills: list[dict[str, Any]] | None = None,
    fills_available: bool = False,
) -> list[dict[str, Any]]:
    """Settle journal rows. Unfilled rests are not wins or losses.

    If fills cannot be loaded, leave unknown rows pending rather than inventing PnL.
    """
    for row in rows:
        if row.get("result") in TERMINAL_RESULTS:
            continue
        ticker = str(row.get("ticker") or "")
        if not ticker:
            continue
        if fills_available and ticker_in_fills(fills, ticker):
            row["fill_status"] = "filled"
        try:
            market = get_market(ticker)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(market, dict):
            continue
        lost = result_is_loss(market, str(row.get("side") or ""))
        if lost is None:
            continue
        settlement = str(market.get("result") or "").strip().lower()
        if settlement in {"yes", "no"}:
            row["settlement_result"] = settlement
        status = str(row.get("fill_status") or "").lower()
        filled = status in FILLED_STATUSES or (fills_available and ticker_in_fills(fills, ticker))
        if filled:
            row["fill_status"] = status if status in FILLED_STATUSES else "filled"
            row["result"] = "loss" if lost else "win"
            row["pnl"] = estimate_pnl(
                won=not lost,
                contracts=int(row.get("contracts") or 0),
                entry_price=float(row.get("kalshi_price") or row.get("limit_price") or 0),
                risk_dollars=float(row.get("risk_dollars") or 0),
            )
            row["resolved_ts"] = format_et()
            row["resolved_ts_iso"] = to_et().isoformat()
            continue
        if fills_available:
            # Book settled and fills were checked — this rest never filled.
            row["fill_status"] = status or "unfilled"
            row["result"] = "unfilled"
            row["pnl"] = 0.0
            row["resolved_ts"] = format_et()
            row["resolved_ts_iso"] = to_et().isoformat()
            # Unknown fill and no fills API: leave pending. Do not invent PnL.
    return rows


def parse_count(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def fill_status_from_order(order: dict[str, Any] | None) -> str:
    """filled / partial / resting / canceled from a Kalshi order payload."""
    if not isinstance(order, dict):
        return "resting"
    fill_count = parse_count(order.get("fill_count") or order.get("filled_count"))
    remaining = parse_count(order.get("remaining_count"))
    status = str(order.get("status") or "").lower()
    if fill_count > 0 and remaining <= 1e-9:
        return "filled"
    if fill_count > 0:
        return "partial"
    if status in {"canceled", "cancelled", "expired"}:
        return "canceled"
    return "resting"


def ticker_in_fills(fills: list[dict[str, Any]] | None, ticker: str) -> bool:
    want = str(ticker or "").upper()
    if not want or not fills:
        return False
    for fill in fills:
        got = str(fill.get("ticker") or fill.get("market_ticker") or "").upper()
        if got == want:
            return True
    return False


def counts_as_filled(row: dict[str, Any]) -> bool:
    status = str(row.get("fill_status") or "").lower()
    if status in FILLED_STATUSES:
        return True
    # Legacy rows resolved before fill tracking: they already have win/loss.
    if not status and row.get("result") in {"win", "loss"}:
        return True
    return False


def daily_loss_reason(
    rows: list[dict[str, Any]],
    now: datetime | None = None,
    *,
    max_dollars: float = 4.00,
    max_losses: int = 2,
) -> str | None:
    """Sit reason if today's filled, settled losses hit the daily cap."""
    losses = [
        row
        for row in rows
        if row.get("result") == "loss"
        and counts_as_filled(row)
        and same_et_day(
            row.get("resolved_ts_iso") or row.get("resolved_ts") or row.get("ts_iso") or row.get("ts"),
            now,
        )
    ]
    if not losses:
        return None
    pnl = sum(float(row.get("pnl") or 0) for row in losses)
    if max_losses > 0 and len(losses) >= max_losses:
        return f"daily loss limit: {len(losses)} filled losses today (cap {max_losses})"
    if max_dollars > 0 and pnl <= -abs(max_dollars):
        return f"daily loss limit: ${-pnl:.2f} filled loss today (cap ${max_dollars:.2f})"
    return None


def bucket_underwater(
    rows: list[dict[str, Any]],
    bucket: str,
    *,
    min_n: int = KILL_MIN_TRADES,
) -> bool:
    settled = [
        row
        for row in rows
        if row.get("bucket") == bucket
        and row.get("result") in {"win", "loss"}
        and counts_as_filled(row)
    ]
    if len(settled) < min_n:
        return False
    return sum(float(row.get("pnl") or 0) for row in settled) < 0
