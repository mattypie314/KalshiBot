"""Append-only trade log. Close-strike / buy-No kill switch lives here."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from src.clock import format_et

CLOSE_BUCKET_PCT = 0.01
KILL_MIN_TRADES = 3


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
) -> dict[str, Any]:
    distance = strike_distance_pct(spot, strike)
    return {
        "ts": format_et(),
        "ticker": ticker,
        "asset": asset,
        "side": side,
        "strike": strike,
        "spot": spot,
        "distance_pct": round(distance, 6),
        "minutes_left": round(minutes_left, 2),
        "fair": round(fair, 4),
        "kalshi_price": round(kalshi_price, 4),
        "limit_price": round(limit_price, 4),
        "contracts": contracts,
        "risk_dollars": round(risk_dollars, 4),
        "hourly_vol": hourly_vol,
        "spot_source": source,
        "bucket": trade_bucket(side, distance),
        "result": "pending",
        "pnl": None,
    }


def resolve_pending(
    rows: list[dict[str, Any]],
    get_market: Callable[[str], dict[str, Any] | None],
    result_is_loss: Callable[[dict[str, Any], str], bool | None],
) -> list[dict[str, Any]]:
    for row in rows:
        if row.get("result") in {"win", "loss"}:
            continue
        ticker = str(row.get("ticker") or "")
        if not ticker:
            continue
        try:
            market = get_market(ticker)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(market, dict):
            continue
        lost = result_is_loss(market, str(row.get("side") or ""))
        if lost is None:
            continue
        row["result"] = "loss" if lost else "win"
        row["pnl"] = estimate_pnl(
            won=not lost,
            contracts=int(row.get("contracts") or 0),
            entry_price=float(row.get("kalshi_price") or row.get("limit_price") or 0),
            risk_dollars=float(row.get("risk_dollars") or 0),
        )
        row["resolved_ts"] = format_et()
    return rows


def bucket_underwater(
    rows: list[dict[str, Any]],
    bucket: str,
    *,
    min_n: int = KILL_MIN_TRADES,
) -> bool:
    settled = [
        row
        for row in rows
        if row.get("bucket") == bucket and row.get("result") in {"win", "loss"}
    ]
    if len(settled) < min_n:
        return False
    return sum(float(row.get("pnl") or 0) for row in settled) < 0
