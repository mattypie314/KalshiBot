"""Hard flatten rules shared by hourly and 15m bots.

Priority: cash_out_99 → cash_out_95_time (bid ≥ 95¢ and ≤ 10m left) → +2¢ TP.
Live oneshots place the exit; they do not wait for an operator.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from src.clock import format_et, parse_ts, to_et
from src.executor import (
    FIFTEEN_SERIES,
    HOURLY_SERIES,
    TICK,
    is_post_only_cross,
    series_code,
)
from src.journal import (
    FILLED_STATUSES,
    TERMINAL_RESULTS,
    apply_exit_fields,
    ticker_in_fills,
)
from src.kalshi_client import unwrap_order
from src.markets import _quote, parse_close_time

logger = logging.getLogger(__name__)

DEFAULT_CASH_OUT_BID = 0.99
DEFAULT_EARLY_CASH_OUT_BID = 0.95
DEFAULT_EARLY_CASH_OUT_MINUTES = 10.0
DEFAULT_TAKE_PROFIT_CENTS = 0.02
CASH_OUT_LABEL = "cash_out_99"
EARLY_CASH_OUT_LABEL = "cash_out_95_time"
MANUAL_FLATTEN_LABEL = "manual_flatten"
MANUAL_CASH_OUT_LABEL = MANUAL_FLATTEN_LABEL
TAKE_PROFIT_LABEL = "take_profit"


@dataclass(frozen=True)
class Holding:
    ticker: str
    side: str
    contracts: int
    fill_price: float | None = None
    asset: str = ""
    exchange_index: int = -1
    source: str = ""


@dataclass
class ExitSignal:
    holding: Holding
    reason: str
    exit_price: float
    yes_book_price: float
    book_side: str
    post_only: bool
    yes_bid: float
    yes_ask: float
    no_bid: float
    payload: dict[str, Any] = field(default_factory=dict)
    minutes_left: float | None = None


def _side(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"yes", "y", "bid"}:
        return "Yes"
    if text in {"no", "n", "ask"}:
        return "No"
    return ""


def held_side_bid(
    side: str,
    *,
    yes_bid: float,
    yes_ask: float = 0.0,
    no_bid: float | None = None,
) -> float:
    """Live bid on the side we hold (Yes bid, or No bid ≈ 1 − Yes ask)."""
    if _side(side) == "Yes":
        return float(yes_bid or 0.0)
    if no_bid is not None and float(no_bid) > 0:
        return float(no_bid)
    if yes_ask and yes_ask > 0:
        return max(0.0, 1.0 - float(yes_ask))
    return 0.0


def should_cash_out_99(
    side: str,
    *,
    yes_bid: float,
    yes_ask: float = 0.0,
    no_bid: float | None = None,
    threshold: float = DEFAULT_CASH_OUT_BID,
) -> bool:
    """True when the held side's live bid is at/through the cash-out tick (default 99¢)."""
    level = float(threshold)
    if _side(side) == "Yes":
        return float(yes_bid or 0.0) + 1e-12 >= level
    if no_bid is not None and float(no_bid) + 1e-12 >= level:
        return True
    if yes_ask is not None and float(yes_ask) <= (1.0 - level) + 1e-12:
        return True
    return False


def should_cash_out_early(
    side: str,
    *,
    yes_bid: float,
    yes_ask: float = 0.0,
    no_bid: float | None = None,
    minutes_left: float | None = None,
    bid_threshold: float = DEFAULT_EARLY_CASH_OUT_BID,
    minutes_threshold: float = DEFAULT_EARLY_CASH_OUT_MINUTES,
) -> bool:
    """True when the held-side bid is ≥ 95¢ and minutes to settlement are ≤ 10.

    Missing minutes_left does not fire — time is part of the rule.
    """
    if minutes_left is None:
        return False
    try:
        remaining = float(minutes_left)
    except (TypeError, ValueError):
        return False
    if remaining > float(minutes_threshold) + 1e-12:
        return False
    return should_cash_out_99(
        side,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        threshold=bid_threshold,
    )


def minutes_until_settlement(close_time: object, now: datetime | None = None) -> float | None:
    """Minutes remaining until market close / settlement. None if close time is unknown."""
    close = parse_close_time(close_time)
    if close is None:
        close = parse_ts(close_time)
    if close is None:
        return None
    return max(0.0, (to_et(close) - to_et(now)).total_seconds() / 60.0)


def minutes_left_from_market(market: dict[str, Any] | None, now: datetime | None = None) -> float | None:
    """Minutes left from a Kalshi market payload's close / expiration time."""
    if not isinstance(market, dict):
        return None
    inner = market["market"] if isinstance(market.get("market"), dict) else market
    raw = (
        inner.get("close_time")
        or inner.get("expiration_time")
        or inner.get("expected_expiration_time")
        or inner.get("latest_expiration_time")
    )
    return minutes_until_settlement(raw, now)


def should_take_profit(
    side: str,
    *,
    fill_price: float | None,
    yes_bid: float,
    yes_ask: float = 0.0,
    no_bid: float | None = None,
    cash_out_bid: float = DEFAULT_CASH_OUT_BID,
    take_profit_cents: float = DEFAULT_TAKE_PROFIT_CENTS,
) -> bool:
    """True on cash_out_99, or when the held-side bid is fill + 2¢."""
    if should_cash_out_99(
        side,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        threshold=cash_out_bid,
    ):
        return True
    if fill_price is None or float(fill_price) <= 0:
        return False
    bid = held_side_bid(side, yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid)
    return bid + 1e-12 >= float(fill_price) + float(take_profit_cents)


def exit_reason(
    side: str,
    *,
    fill_price: float | None,
    yes_bid: float,
    yes_ask: float = 0.0,
    no_bid: float | None = None,
    cash_out_bid: float = DEFAULT_CASH_OUT_BID,
    take_profit_cents: float = DEFAULT_TAKE_PROFIT_CENTS,
    minutes_left: float | None = None,
    early_cash_out_bid: float = DEFAULT_EARLY_CASH_OUT_BID,
    early_cash_out_minutes: float = DEFAULT_EARLY_CASH_OUT_MINUTES,
) -> str | None:
    """cash_out_99, then cash_out_95_time, then take_profit."""
    if should_cash_out_99(
        side,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        threshold=cash_out_bid,
    ):
        return CASH_OUT_LABEL
    if should_cash_out_early(
        side,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        minutes_left=minutes_left,
        bid_threshold=early_cash_out_bid,
        minutes_threshold=early_cash_out_minutes,
    ):
        return EARLY_CASH_OUT_LABEL
    if should_take_profit(
        side,
        fill_price=fill_price,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        cash_out_bid=cash_out_bid,
        take_profit_cents=take_profit_cents,
    ):
        return TAKE_PROFIT_LABEL
    return None


def flatten_yes_book_price(side: str, held_exit_price: float) -> float:
    """Yes-book limit that sells the held side at held_exit_price."""
    price = max(TICK, min(1.0 - TICK, round(float(held_exit_price), 2)))
    if _side(side) == "Yes":
        return price
    return round(max(TICK, min(1.0 - TICK, 1.0 - price)), 2)


def flatten_book_side(side: str) -> str:
    return "ask" if _side(side) == "Yes" else "bid"


def post_only_would_take_exit(
    side: str,
    *,
    yes_bid: float,
    yes_ask: float,
    yes_book_price: float,
) -> bool:
    """True if a post-only flatten at yes_book_price would take the book."""
    if _side(side) == "Yes":
        return float(yes_bid or 0.0) + 1e-12 >= float(yes_book_price)
    return float(yes_ask or 0.0) <= float(yes_book_price) + 1e-12


def flatten_payload(
    ticker: str,
    side: str,
    contracts: int,
    *,
    yes_book_price: float,
    post_only: bool,
    exchange_index: int = -1,
) -> dict[str, Any]:
    """Limit flatten. Same Yes-book bid/ask shape as entries; never a market sweep."""
    return {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": flatten_book_side(side),
        "count": f"{int(contracts):.2f}",
        "price": f"{float(yes_book_price):.4f}",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": bool(post_only),
        "exchange_index": exchange_index,
        "label": CASH_OUT_LABEL,
    }


def ticker_in_bot_series(ticker: str, series: Iterable[str]) -> bool:
    want = {str(item).upper() for item in series}
    return series_code(ticker) in want


def parse_signed_contracts(row: dict[str, Any]) -> float:
    for key in ("position_fp", "position"):
        raw = row.get(key)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


def parse_fill_price(row: dict[str, Any], side: str) -> float | None:
    """Held-side entry from a fill or journal row."""
    if _side(side) == "Yes":
        keys = (
            "yes_price_dollars",
            "yes_price",
            "price_dollars",
            "price",
            "kalshi_price",
            "limit_price",
            "limit",
            "fill_price",
        )
    else:
        keys = (
            "no_price_dollars",
            "no_price",
            "kalshi_price",
            "limit_price",
            "limit",
            "fill_price",
        )
    for key in keys:
        raw = row.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if 0 < value < 1:
            return value
    if _side(side) == "No":
        for key in ("yes_price_dollars", "yes_price"):
            raw = row.get(key)
            if raw in (None, ""):
                continue
            try:
                yes = float(raw)
            except (TypeError, ValueError):
                continue
            if 0 < yes < 1:
                return round(1.0 - yes, 4)
    return None


def _hint_fill(row: dict[str, Any], side: str) -> float | None:
    return parse_fill_price(row, side)


def fill_ticker(row: dict[str, Any]) -> str:
    return str(row.get("ticker") or row.get("market_ticker") or "")


def fill_order_id(row: dict[str, Any]) -> str:
    return str(row.get("order_id") or "")


def known_bot_order_ids(row: dict[str, Any]) -> set[str]:
    """Entry + bot-placed exit ids on a journal row. App fills will not match these."""
    found: set[str] = set()
    for key in ("order_id", "client_order_id", "exit_order_id", "exit_client_order_id"):
        value = str(row.get(key) or "").strip()
        if value:
            found.add(value)
    return found


def fill_outcome_side(row: dict[str, Any]) -> str:
    """Yes/No this fill is positioned for (Kalshi outcome_side, or action+side)."""
    outcome = _side(row.get("outcome_side"))
    if outcome:
        return outcome
    book = str(row.get("book_side") or "").strip().lower()
    if book == "bid":
        return "Yes"
    if book == "ask":
        return "No"
    action = str(row.get("action") or "").strip().lower()
    side = _side(row.get("side"))
    if action == "sell" and side == "Yes":
        return "No"
    if action == "sell" and side == "No":
        return "Yes"
    if action == "buy" and side:
        return side
    return side


def fill_is_close(row: dict[str, Any], held_side: str) -> bool:
    """True when this fill reduces/closes the side we were long."""
    held = _side(held_side)
    if not held:
        return False
    action = str(row.get("action") or row.get("order_action") or "").strip().lower()
    fill_side = _side(row.get("side"))
    if action in {"sell", "close", "flatten", "exit"}:
        return (not fill_side) or fill_side == held
    if action in {"buy", "open"}:
        return bool(fill_side) and fill_side != held
    outcome = fill_outcome_side(row)
    return bool(outcome) and outcome != held


def matching_close_fills(
    fills: Iterable[dict[str, Any]] | None,
    *,
    ticker: str,
    side: str,
    exclude_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    want = str(ticker or "").upper()
    skip = {str(item).strip() for item in exclude_ids if str(item).strip()}
    found: list[dict[str, Any]] = []
    for row in fills or []:
        if not isinstance(row, dict):
            continue
        if fill_ticker(row).upper() != want:
            continue
        order_id = fill_order_id(row)
        if order_id and order_id in skip:
            continue
        if fill_is_close(row, side):
            found.append(row)
    return found


def ticker_has_exit(trades: Iterable[dict[str, Any]] | None, ticker: str) -> bool:
    want = str(ticker or "")
    if not want:
        return False
    for row in trades or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("ticker") or "") != want:
            continue
        if row.get("exit_reason"):
            return True
    return False


def load_position_qty_by_ticker(
    client: Any,
    series: Iterable[str],
) -> tuple[dict[str, float] | None, bool]:
    """Map ticker → signed contracts. available=False if the positions API did not answer."""
    getter = getattr(client, "get_positions", None)
    if getter is None:
        return None, False
    try:
        rows = getter(count_filter="position") or []
    except Exception as exc:  # noqa: BLE001
        logger.info("positions unavailable: %s", exc)
        return None, False
    if not isinstance(rows, list):
        return None, False
    found: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or row.get("market_ticker") or "")
        if not ticker or not ticker_in_bot_series(ticker, series):
            continue
        found[ticker] = parse_signed_contracts(row)
    return found, True


def detect_manual_flattens(
    trades: Iterable[dict[str, Any]] | None,
    *,
    fills: Iterable[dict[str, Any]] | None,
    fills_available: bool,
    positions: dict[str, float] | None,
    positions_available: bool,
    series: Iterable[str],
) -> list[dict[str, Any]]:
    """Filled bot entries that are now flat via an in-app sell, not our exit order id."""
    if not fills_available or not positions_available or positions is None:
        return []
    found: list[dict[str, Any]] = []
    for row in trades or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("action") or "") == "exit":
            continue
        if row.get("result") in TERMINAL_RESULTS or row.get("exit_reason"):
            continue
        if str(row.get("fill_status") or "").lower() not in FILLED_STATUSES:
            continue
        ticker = str(row.get("ticker") or "")
        if not ticker or not ticker_in_bot_series(ticker, series):
            continue
        side = _side(row.get("side"))
        if not side:
            continue
        qty = float(positions.get(ticker) or 0.0)
        if abs(qty) >= 1 - 1e-9:
            continue
        closes = matching_close_fills(
            fills,
            ticker=ticker,
            side=side,
            exclude_ids=known_bot_order_ids(row),
        )
        if not closes:
            continue
        close = closes[0]
        price = parse_fill_price(close, side) or 0.0
        found.append(
            {
                "trade": row,
                "ticker": ticker,
                "side": side,
                "exit_price": price,
                "order_id": fill_order_id(close),
                "fill": close,
            }
        )
    return found


def holdings_from_positions(
    rows: Iterable[dict[str, Any]],
    series: Iterable[str],
) -> dict[str, Holding]:
    found: dict[str, Holding] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or row.get("market_ticker") or "")
        if not ticker or not ticker_in_bot_series(ticker, series):
            continue
        qty = parse_signed_contracts(row)
        if abs(qty) < 1 - 1e-9:
            continue
        side = "Yes" if qty > 0 else "No"
        try:
            shard = int(row.get("exchange_index")) if row.get("exchange_index") not in (None, "") else -1
        except (TypeError, ValueError):
            shard = -1
        found[ticker] = Holding(
            ticker=ticker,
            side=side,
            contracts=max(1, int(abs(qty))),
            fill_price=_hint_fill(row, side),
            exchange_index=shard,
            source="position",
        )
    return found


def holdings_from_hints(
    *,
    state: dict[str, Any],
    trades: Iterable[dict[str, Any]] | None,
    fills: Iterable[dict[str, Any]] | None,
    fills_available: bool,
    series: Iterable[str],
) -> dict[str, Holding]:
    """Inventory from last ticket / journal / fills when the positions API is quiet."""
    found: dict[str, Holding] = {}

    def _add(ticker: str, side: str, contracts: object, fill: float | None, source: str) -> None:
        if not ticker or not ticker_in_bot_series(ticker, series):
            return
        ours = _side(side)
        if not ours:
            return
        try:
            count = int(float(contracts or 0))
        except (TypeError, ValueError):
            count = 0
        if count < 1:
            count = 1
        found[ticker] = Holding(
            ticker=ticker,
            side=ours,
            contracts=count,
            fill_price=fill,
            source=source,
        )

    last = str(state.get("last_ticker") or "")
    if last and not ticker_has_exit(trades, last):
        _add(
            last,
            str(state.get("last_side") or ""),
            state.get("last_contracts"),
            parse_fill_price(state, str(state.get("last_side") or "")),
            "state",
        )
    for ticket in state.get("tickets") or []:
        if not isinstance(ticket, dict) or str(ticket.get("status") or "") != "open":
            continue
        _add(
            str(ticket.get("ticker") or ""),
            str(ticket.get("side") or ""),
            ticket.get("contracts") or ticket.get("filled_contracts"),
            parse_fill_price(ticket, str(ticket.get("side") or "")),
            "ticket",
        )

    for row in trades or []:
        if not isinstance(row, dict):
            continue
        if row.get("result") in TERMINAL_RESULTS or row.get("exit_reason"):
            continue
        status = str(row.get("fill_status") or "").lower()
        if status not in FILLED_STATUSES:
            continue
        qty = row.get("filled_contracts") or row.get("contracts")
        _add(
            str(row.get("ticker") or ""),
            str(row.get("side") or ""),
            qty,
            parse_fill_price(row, str(row.get("side") or "")),
            "journal",
        )

    if not found:
        return {}
    if not fills_available:
        # Filled journal rows only. An open rest / last_ticker is not inventory.
        return {ticker: holding for ticker, holding in found.items() if holding.source == "journal"}
    confirmed: dict[str, Holding] = {}
    fill_list = list(fills or [])
    for ticker, holding in found.items():
        if holding.source == "journal" or ticker_in_fills(fill_list, ticker):
            confirmed[ticker] = holding
    return confirmed


def collect_holdings(
    client: Any,
    *,
    state: dict[str, Any],
    trades: Iterable[dict[str, Any]] | None = None,
    fills: Iterable[dict[str, Any]] | None = None,
    fills_available: bool = False,
    series: Iterable[str] = HOURLY_SERIES,
) -> list[Holding]:
    found: dict[str, Holding] = {}
    getter = getattr(client, "get_positions", None)
    if getter is not None:
        try:
            rows = getter(count_filter="position") or []
        except Exception as exc:  # noqa: BLE001
            logger.info("positions unavailable: %s", exc)
            rows = []
        if isinstance(rows, list):
            found.update(holdings_from_positions(rows, series))
    hints = holdings_from_hints(
        state=state,
        trades=trades,
        fills=fills,
        fills_available=fills_available,
        series=series,
    )
    for ticker, holding in hints.items():
        current = found.get(ticker)
        if current is None:
            found[ticker] = holding
            continue
        if current.fill_price is None and holding.fill_price is not None:
            found[ticker] = Holding(
                ticker=current.ticker,
                side=current.side,
                contracts=current.contracts,
                fill_price=holding.fill_price,
                asset=current.asset or holding.asset,
                exchange_index=current.exchange_index,
                source=current.source,
            )
    return list(found.values())


def signal_for_holding(
    holding: Holding,
    *,
    yes_bid: float,
    yes_ask: float,
    no_bid: float,
    cash_out_bid: float = DEFAULT_CASH_OUT_BID,
    take_profit_cents: float = DEFAULT_TAKE_PROFIT_CENTS,
    exchange_index: int = -1,
    minutes_left: float | None = None,
    early_cash_out_bid: float = DEFAULT_EARLY_CASH_OUT_BID,
    early_cash_out_minutes: float = DEFAULT_EARLY_CASH_OUT_MINUTES,
) -> ExitSignal | None:
    reason = exit_reason(
        holding.side,
        fill_price=holding.fill_price,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        cash_out_bid=cash_out_bid,
        take_profit_cents=take_profit_cents,
        minutes_left=minutes_left,
        early_cash_out_bid=early_cash_out_bid,
        early_cash_out_minutes=early_cash_out_minutes,
    )
    if not reason:
        return None
    held_exit = cash_out_bid if reason == CASH_OUT_LABEL else held_side_bid(
        holding.side, yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid
    )
    if held_exit <= 0:
        held_exit = cash_out_bid
    yes_price = flatten_yes_book_price(holding.side, held_exit)
    take = post_only_would_take_exit(
        holding.side, yes_bid=yes_bid, yes_ask=yes_ask, yes_book_price=yes_price
    )
    shard = holding.exchange_index if holding.exchange_index >= 0 else exchange_index
    payload = flatten_payload(
        holding.ticker,
        holding.side,
        holding.contracts,
        yes_book_price=yes_price,
        post_only=not take,
        exchange_index=shard,
    )
    payload["label"] = reason
    return ExitSignal(
        holding=holding,
        reason=reason,
        exit_price=held_exit,
        yes_book_price=yes_price,
        book_side=payload["side"],
        post_only=not take,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        payload=payload,
        minutes_left=minutes_left,
    )


def place_flatten(create: Any, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """POST the flatten limit. If post-only would take, hit the same 99¢/bid limit."""
    attempt = dict(payload)
    try:
        return unwrap_order(create(attempt)), attempt
    except Exception as exc:  # noqa: BLE001
        if not attempt.get("post_only") or not is_post_only_cross(exc):
            raise
        retry = dict(attempt)
        retry["post_only"] = False
        retry["client_order_id"] = str(uuid.uuid4())
        print(
            f"LIVE {retry.get('label') or CASH_OUT_LABEL} post-only would take; "
            f"hitting the bid at {retry.get('price')} (limit, not a market sweep)",
            flush=True,
        )
        return unwrap_order(create(retry)), retry


def _quote_market(
    client: Any,
    ticker: str,
    now: datetime | None = None,
) -> tuple[float, float, float, float, float | None] | None:
    getter = getattr(client, "get_market", None)
    if getter is None:
        return None
    try:
        raw = getter(ticker)
    except Exception as exc:  # noqa: BLE001
        logger.info("exit quote failed for %s: %s", ticker, exc)
        return None
    if not isinstance(raw, dict):
        return None
    yes_bid, yes_ask, no_bid, no_ask = _quote(raw)
    return yes_bid, yes_ask, no_bid, no_ask, minutes_left_from_market(raw, now)


def _exit_event(
    signal: ExitSignal,
    *,
    order: dict[str, Any] | None,
    live: bool,
) -> dict[str, Any]:
    return apply_exit_fields(
        {
            "ts": format_et(),
            "ticker": signal.holding.ticker,
            "side": signal.holding.side,
            "contracts": signal.holding.contracts,
            "fill_price": signal.holding.fill_price,
            "action": "exit",
            "mode": "live" if live else "dry_run",
            "yes_bid": signal.yes_bid,
            "yes_ask": signal.yes_ask,
            "no_bid": signal.no_bid,
            "order_id": str((order or {}).get("order_id") or ""),
            "client_order_id": str((signal.payload or {}).get("client_order_id") or ""),
            "minutes_left": signal.minutes_left,
        },
        reason=signal.reason,
        exit_price=signal.exit_price,
        order_id=str((order or {}).get("order_id") or ""),
    )


def _label_open_trade(trades: list[dict[str, Any]] | None, signal: ExitSignal, order: dict[str, Any] | None) -> None:
    for row in trades or []:
        if str(row.get("ticker") or "") != signal.holding.ticker:
            continue
        if row.get("result") in TERMINAL_RESULTS:
            continue
        apply_exit_fields(
            row,
            reason=signal.reason,
            exit_price=signal.exit_price,
            order_id=str((order or {}).get("order_id") or ""),
        )
        break


def mark_tickets_flat(state: dict[str, Any], ticker: str, reason: str) -> None:
    for key in ("tickets", "rests"):
        for row in state.get(key) or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("ticker") or "") != ticker:
                continue
            if str(row.get("status") or "") != "open":
                continue
            row["status"] = "flat"
            row["exit_reason"] = reason


def manage_open_positions(
    client: Any,
    *,
    state: dict[str, Any],
    settings: Any | None = None,
    trades: list[dict[str, Any]] | None = None,
    fills: list[dict[str, Any]] | None = None,
    fills_available: bool = False,
    live: bool = False,
    journal_path: str | Path | None = None,
    series: Iterable[str] = HOURLY_SERIES,
    exchange_index: int = -1,
    cash_out_bid: float | None = None,
    take_profit_cents: float | None = None,
    early_cash_out_bid: float | None = None,
    early_cash_out_minutes: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reconcile in-app flattens, then flatten when 99¢ / early 95¢ / +2¢ TP fires.

    Live oneshots POST bot exits. Manual in-app sells are journaled, not re-placed.
    """
    threshold = (
        cash_out_bid
        if cash_out_bid is not None
        else float(getattr(settings, "cash_out_bid", DEFAULT_CASH_OUT_BID))
    )
    tp = (
        take_profit_cents
        if take_profit_cents is not None
        else float(getattr(settings, "take_profit_cents", DEFAULT_TAKE_PROFIT_CENTS))
    )
    early_bid = (
        early_cash_out_bid
        if early_cash_out_bid is not None
        else float(getattr(settings, "early_cash_out_bid", DEFAULT_EARLY_CASH_OUT_BID))
    )
    early_minutes = (
        early_cash_out_minutes
        if early_cash_out_minutes is not None
        else float(getattr(settings, "early_cash_out_minutes", DEFAULT_EARLY_CASH_OUT_MINUTES))
    )
    dest = Path(journal_path) if journal_path else None
    result: dict[str, Any] = {
        "signals": [],
        "placed": [],
        "errors": [],
        "dry_run": [],
        "journal": [],
        "manual": [],
    }
    positions, positions_ok = load_position_qty_by_ticker(client, series)
    for hit in detect_manual_flattens(
        trades,
        fills=fills,
        fills_available=fills_available,
        positions=positions,
        positions_available=positions_ok,
        series=series,
    ):
        apply_exit_fields(
            hit["trade"],
            reason=MANUAL_FLATTEN_LABEL,
            exit_price=hit["exit_price"],
            order_id=hit["order_id"],
        )
        mark_tickets_flat(state, hit["ticker"], MANUAL_FLATTEN_LABEL)
        if str(state.get("last_ticker") or "") == hit["ticker"]:
            state["last_exit_reason"] = MANUAL_FLATTEN_LABEL
        event = apply_exit_fields(
            {
                "ts": format_et(),
                "ticker": hit["ticker"],
                "side": hit["side"],
                "contracts": hit["trade"].get("contracts") or hit["trade"].get("filled_contracts"),
                "fill_price": hit["trade"].get("kalshi_price") or hit["trade"].get("fill_price"),
                "action": "exit",
                "mode": "live" if live else "dry_run",
                "order_id": hit["order_id"],
                "source": "manual",
            },
            reason=MANUAL_FLATTEN_LABEL,
            exit_price=hit["exit_price"],
            order_id=hit["order_id"],
        )
        result["manual"].append(
            {
                "ticker": hit["ticker"],
                "side": hit["side"],
                "reason": MANUAL_FLATTEN_LABEL,
                "exit_price": hit["exit_price"],
                "order_id": hit["order_id"],
            }
        )
        result["signals"].append(
            {
                "ticker": hit["ticker"],
                "side": hit["side"],
                "reason": MANUAL_FLATTEN_LABEL,
                "exit_price": hit["exit_price"],
                "source": "manual",
            }
        )
        result["journal"].append(event)
        print(
            f"{MANUAL_FLATTEN_LABEL.upper()} {hit['ticker']} {hit['side']} "
            f"@ {float(hit['exit_price'] or 0):.2f} (in-app flatten, not a bot exit)",
            flush=True,
        )
    holdings = collect_holdings(
        client,
        state=state,
        trades=trades,
        fills=fills,
        fills_available=fills_available,
        series=series,
    )
    create = getattr(client, "create_order", None) or getattr(client, "create_order_v2", None)
    for holding in holdings:
        quote = _quote_market(client, holding.ticker, now)
        if quote is None:
            continue
        yes_bid, yes_ask, no_bid, _no_ask, minutes_left = quote
        signal = signal_for_holding(
            holding,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            cash_out_bid=threshold,
            take_profit_cents=tp,
            exchange_index=exchange_index,
            minutes_left=minutes_left,
            early_cash_out_bid=early_bid,
            early_cash_out_minutes=early_minutes,
        )
        if signal is None:
            continue
        result["signals"].append(
            {
                "ticker": holding.ticker,
                "side": holding.side,
                "reason": signal.reason,
                "exit_price": signal.exit_price,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "no_bid": no_bid,
                "minutes_left": minutes_left,
            }
        )
        print(
            f"{signal.reason.upper()} {holding.ticker} {holding.side} "
            f"x {holding.contracts} @ {signal.exit_price:.2f} "
            f"(book {yes_bid:.2f}/{yes_ask:.2f}; "
            f"{'hit bid' if not signal.post_only else 'post-only rest'}"
            f"{f'; {minutes_left:.1f}m left' if minutes_left is not None else ''})",
            flush=True,
        )
        if not live:
            print(json_payload(signal.payload), flush=True)
            result["dry_run"].append(signal.payload)
            _label_open_trade(trades, signal, None)
            result["journal"].append(_exit_event(signal, order=None, live=False))
            continue
        if create is None:
            result["errors"].append(f"{holding.ticker}: client has no create_order")
            continue
        try:
            placed, working = place_flatten(create, signal.payload)
            signal.payload = working
            result["placed"].append(placed)
            print(
                f"LIVE {signal.reason} {working.get('ticker')} {working.get('side')} "
                f"{working.get('price')} x {working.get('count')} "
                f"order_id={placed.get('order_id') or '?'}",
                flush=True,
            )
            mark_tickets_flat(state, holding.ticker, signal.reason)
            if str(state.get("last_ticker") or "") == holding.ticker:
                state["last_exit_reason"] = signal.reason
            _label_open_trade(trades, signal, placed)
            result["journal"].append(_exit_event(signal, order=placed, live=True))
        except Exception as exc:  # noqa: BLE001
            msg = f"{holding.ticker}: {exc}"
            logger.error("flatten failed: %s payload=%s", exc, signal.payload)
            result["errors"].append(msg)
            print(f"LIVE {signal.reason} failed: {msg}", flush=True)
    if result["journal"] and trades is not None:
        trades.extend(result["journal"])
    if dest is not None and (trades is not None):
        from src.journal import write_trades

        write_trades(dest, trades)
    elif dest is not None and result["journal"]:
        from src.journal import write_trades

        write_trades(dest, result["journal"])
    return result


def json_payload(payload: dict[str, Any]) -> str:
    import json

    return json.dumps({"dry_run_exit": payload}, indent=2)
