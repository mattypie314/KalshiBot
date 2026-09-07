"""Append-only trade log. Close-strike / buy-No kill switch lives here."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.clock import fifteen_window_start, format_et, parse_ts, same_et_day, to_et

CLOSE_BUCKET_PCT = 0.01
KILL_MIN_TRADES = 3
FILLED_STATUSES = frozenset({"filled", "partial"})
TERMINAL_RESULTS = frozenset({"win", "loss", "unfilled"})
TURBO_LABEL = "Turbo / FORCE_NEAR_RULE"
FILL_BACKFILL_SOURCE = "kalshi-fill-backfill"
KIND_BACKFILL = "backfill"
KIND_PAPER = "paper"
LATE_PLACE_SECONDS = 4 * 60
# Kalshi fills often put size in `count_fp` ("2.00") and leave `count` empty / "0.00".
FILL_SIZE_KEYS = (
    "count_fp",
    "filled_count_fp",
    "fill_count_fp",
    "count",
    "filled_count",
    "fill_count",
)
ORDER_FILL_COUNT_KEYS = (
    "fill_count_fp",
    "filled_count_fp",
    "fill_count",
    "filled_count",
)
ORDER_REMAINING_KEYS = ("remaining_count_fp", "remaining_count")


CASH_OUT_LABEL = "cash_out_99"
EARLY_CASH_OUT_LABEL = "cash_out_95_time"
MANUAL_FLATTEN_LABEL = "manual_flatten"
MANUAL_CASH_OUT_LABEL = MANUAL_FLATTEN_LABEL


def apply_exit_fields(
    row: dict[str, Any],
    *,
    reason: str,
    exit_price: float,
    order_id: str = "",
) -> dict[str, Any]:
    """Label a flatten (cash_out_99 / cash_out_95_time / take_profit / manual_flatten)."""
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


def _truthy_flag(value: object) -> bool:
    if value is True:
        return True
    if value is False or value is None or value == "":
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def is_journal_backfill(row: dict[str, Any] | None) -> bool:
    """Fill-recon row, not a live/entry/paper Pass. Exclude from entry timing."""
    if not isinstance(row, dict):
        return False
    if _truthy_flag(row.get("backfill")):
        return True
    if str(row.get("spot_source") or "").strip().lower() == FILL_BACKFILL_SOURCE:
        return True
    return str(row.get("kind") or "").strip().lower() == KIND_BACKFILL


def counts_for_scoreboard(row: dict[str, Any] | None) -> bool:
    """True for a live/paper ticket that belongs on W/L, day PnL, streak, PLAY, pot equity.

    `kind=backfill` recon rows stay in the journal but must not score like a 15m Pass.
    """
    if not isinstance(row, dict):
        return False
    return not is_journal_backfill(row)


def scoreboard_rows(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [row for row in (rows or []) if counts_for_scoreboard(row)]


def filled_settled_score_rows(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Win/loss fills that count on the board. Backfills are excluded."""
    return [
        row
        for row in scoreboard_rows(rows)
        if row.get("result") in {"win", "loss"} and counts_as_filled(row)
    ]


def win_loss_streak(rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Current consecutive W/L streak on scoreboard rows (backfills skipped)."""
    settled = filled_settled_score_rows(rows)
    if not settled:
        return {"result": None, "n": 0}
    last = str(settled[-1].get("result") or "")
    n = 0
    for row in reversed(settled):
        if str(row.get("result") or "") != last:
            break
        n += 1
    return {"result": last, "n": n}


def day_filled_pnl(rows: list[dict[str, Any]] | None, now: datetime | None = None) -> float:
    """Today's filled PnL for the board. Backfills are excluded."""
    total = 0.0
    for row in filled_settled_score_rows(rows):
        stamp = row.get("resolved_ts_iso") or row.get("resolved_ts") or row.get("ts_iso") or row.get("ts")
        if not same_et_day(stamp, now):
            continue
        try:
            total += float(row.get("pnl") or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 4)


def play_pot_equity(
    rows: list[dict[str, Any]] | None,
    *,
    start: float = 5.0,
) -> float:
    """Pot reconstructed from live-play PnL only. Backfill recon does not move it."""
    pnl = sum(float(row.get("pnl") or 0) for row in filled_settled_score_rows(rows))
    return round(float(start) + pnl, 4)


def counts_for_entry_timing(row: dict[str, Any] | None) -> bool:
    """True for a real place/entry used in late-place / seconds-into-window stats."""
    if not isinstance(row, dict):
        return False
    if is_journal_backfill(row):
        return False
    if str(row.get("kind") or "").strip().lower() == KIND_PAPER:
        return False
    return str(row.get("action") or "").strip().lower() != "exit"


def entry_timing_rows(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [row for row in (rows or []) if counts_for_entry_timing(row)]


def _fill_side_label(value: object) -> str:
    text = str(value or "").strip()
    if text.lower() in {"yes", "y", "bid"}:
        return "Yes"
    if text.lower() in {"no", "n", "ask"}:
        return "No"
    return text


def _fill_price_from_payload(fill: dict[str, Any], side: str) -> float:
    if side == "Yes":
        keys = ("yes_price_dollars", "yes_price", "price_dollars", "price", "kalshi_price")
    else:
        keys = ("no_price_dollars", "no_price", "kalshi_price", "price_dollars", "price")
    for key in keys:
        raw = fill.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if 0 < value < 1:
            return value
    if side == "No":
        for key in ("yes_price_dollars", "yes_price"):
            raw = fill.get(key)
            if raw in (None, ""):
                continue
            try:
                yes = float(raw)
            except (TypeError, ValueError):
                continue
            if 0 < yes < 1:
                return round(1.0 - yes, 4)
    return 0.0


def fill_exchange_ts(fill: dict[str, Any] | None) -> datetime | None:
    """Kalshi/exchange stamp on a fill. Never the journal write clock."""
    if not isinstance(fill, dict):
        return None
    for key in ("created_time", "created_ts", "fill_ts", "timestamp", "ts"):
        parsed = parse_ts(fill.get(key))
        if parsed is not None:
            return parsed
    return None


def timing_timestamp(row: dict[str, Any] | None) -> datetime | None:
    """Clock used for seconds-into-window. Backfills use fill_ts, never write-time."""
    if not isinstance(row, dict):
        return None
    if is_journal_backfill(row):
        return parse_ts(row.get("fill_ts_iso") or row.get("fill_ts"))
    return parse_ts(row.get("ts_iso") or row.get("ts"))


def seconds_into_window(row: dict[str, Any] | None) -> float | None:
    stamp = timing_timestamp(row)
    if stamp is None:
        return None
    return max(0.0, (stamp - fifteen_window_start(stamp)).total_seconds())


def is_late_place(row: dict[str, Any] | None) -> bool:
    secs = seconds_into_window(row)
    return secs is not None and secs > LATE_PLACE_SECONDS


def summarize_entry_timing(rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Late-place / seconds-into-window stats. Backfills are excluded from the rate."""
    all_rows = [row for row in (rows or []) if isinstance(row, dict)]
    backfills = [row for row in all_rows if is_journal_backfill(row)]
    timed = entry_timing_rows(all_rows)
    late = [row for row in timed if is_late_place(row)]
    seconds = [secs for secs in (seconds_into_window(row) for row in timed) if secs is not None]
    return {
        "n": len(timed),
        "n_late": len(late),
        "n_backfills": len(backfills),
        "late_place_rate": (len(late) / len(timed)) if timed else 0.0,
        "seconds": seconds,
    }


def late_place_rate(rows: list[dict[str, Any]] | None) -> float:
    return float(summarize_entry_timing(rows)["late_place_rate"])


def journal_match_ids(row: dict[str, Any] | None) -> set[str]:
    if not isinstance(row, dict):
        return set()
    found: set[str] = set()
    for key in ("order_id", "client_order_id", "fill_id", "exit_order_id"):
        value = str(row.get(key) or "").strip()
        if value:
            found.add(value)
    return found


def fill_already_journaled(
    trades: list[dict[str, Any]] | None,
    fill: dict[str, Any] | None,
) -> bool:
    """True when this Kalshi fill is already on a journal row (place or prior backfill)."""
    if not isinstance(fill, dict):
        return False
    ids = journal_match_ids(fill)
    ticker = str(fill.get("ticker") or fill.get("market_ticker") or "").upper()
    for row in trades or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("kind") or "").strip().lower() == KIND_PAPER:
            continue
        if str(row.get("action") or "").strip().lower() == "exit":
            continue
        if ids and ids & journal_match_ids(row):
            return True
        if not ids and ticker and str(row.get("ticker") or "").upper() == ticker:
            return True
    return False


def asset_from_ticker(ticker: str) -> str:
    code = str(ticker or "").upper().split("-", 1)[0]
    if "ETH" in code:
        return "ETH"
    if "BTC" in code:
        return "BTC"
    return ""


def new_backfill_row(
    *,
    ticker: str = "",
    fill: dict[str, Any] | None = None,
    asset: str = "",
    side: str = "",
    order_id: str = "",
    fill_id: str = "",
    fill_ts: object = None,
    contracts: float | None = None,
    kalshi_price: float | None = None,
    action: str = "",
) -> dict[str, Any]:
    """Journal a Kalshi fill that was never a live Pass. Do not invent spot/strike/vol.

    `ts` / `ts_iso` are write-time bookkeeping. Entry-timing readers must use `fill_ts`
    or skip the row (`kind=backfill`).
    """
    payload = fill if isinstance(fill, dict) else {}
    ticker = str(ticker or payload.get("ticker") or payload.get("market_ticker") or "")
    side_label = _fill_side_label(side or payload.get("outcome_side") or payload.get("side"))
    when = parse_ts(fill_ts) or fill_exchange_ts(payload)
    count = float(contracts) if contracts is not None else fill_size_from_payload(payload)
    price = (
        float(kalshi_price)
        if kalshi_price is not None
        else _fill_price_from_payload(payload, side_label)
    )
    contracts_i = int(count) if count >= 1 else (1 if 0 < count < 1 else 0)
    risk = round(abs(count) * price, 4) if count and price else 0.0
    row = {
        "ts": format_et(),
        "ts_iso": to_et().isoformat(),
        "kind": KIND_BACKFILL,
        "backfill": True,
        "spot_source": FILL_BACKFILL_SOURCE,
        "ticker": ticker,
        "asset": asset or asset_from_ticker(ticker),
        "side": side_label,
        "strike": 0.0,
        "spot": 0.0,
        "distance_pct": 0.0,
        "minutes_left": 0.0,
        "fair": 0.0,
        "model_pct": 0.0,
        "kalshi_price": round(price, 4),
        "limit_price": round(price, 4),
        "contracts": contracts_i,
        "risk_dollars": risk,
        "hourly_vol": 0.0,
        "bucket": "backfill",
        "order_id": str(order_id or payload.get("order_id") or ""),
        "client_order_id": str(payload.get("client_order_id") or ""),
        "fill_id": str(fill_id or payload.get("fill_id") or payload.get("id") or ""),
        "fill_status": "filled",
        "filled_contracts": count,
        "settlement_result": None,
        "result": "pending",
        "pnl": None,
        "action": str(action or payload.get("action") or payload.get("order_action") or ""),
    }
    if when is not None:
        row["fill_ts"] = format_et(when)
        row["fill_ts_iso"] = when.isoformat()
    return row


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


def first_present_count(payload: dict[str, Any] | None, keys: tuple[str, ...]) -> float:
    """First field that is present, including zero ('0.00' remaining means filled)."""
    if not isinstance(payload, dict):
        return 0.0
    for key in keys:
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        return parse_count(raw)
    return 0.0


def first_parsed_count(payload: dict[str, Any] | None, keys: tuple[str, ...]) -> float:
    """First field that parses to a positive size. Skips missing / empty / 0 / '0.00'."""
    if not isinstance(payload, dict):
        return 0.0
    seen = 0.0
    found = False
    for key in keys:
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        value = parse_count(raw)
        found = True
        if value > 0:
            return value
        seen = value
    return seen if found else 0.0


def fill_size_from_payload(payload: dict[str, Any] | None) -> float:
    """Kalshi fill size. Prefer `count_fp` / `*_fp` strings over integer `count`."""
    return first_parsed_count(payload, FILL_SIZE_KEYS)


def fill_status_from_order(order: dict[str, Any] | None) -> str:
    """filled / partial / resting / canceled from a Kalshi order payload."""
    if not isinstance(order, dict):
        return "resting"
    fill_count = first_parsed_count(order, ORDER_FILL_COUNT_KEYS)
    remaining = first_present_count(order, ORDER_REMAINING_KEYS)
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
