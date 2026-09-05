"""Paper (counterfactual) journal for dry hourly scans.

Answers: if we had taken the printed dry-scan idea, would it have been profitable?
This is not live profitability. Assumed maker fills are labeled as such.
Never writes to artifacts/trade_log.jsonl.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.cfindex import (
    average_settlement_window,
    fifteen_index_id_for,
    history_query_timestamp,
    index_id_for,
    official_index_label,
    official_yes,
    parse_cf_history_ticks,
)
from src.clock import format_et, parse_ts, to_et
from src.filters import Idea
from src.journal import (
    append_trade,
    estimate_pnl,
    load_trades,
    strike_distance_pct,
    trade_bucket,
    write_trades,
)
from src.spot import is_settlement_index

logger = logging.getLogger(__name__)

LIVE_TRADE_LOG_NAME = "trade_log.jsonl"
DEFAULT_PAPER_LOG_NAME = "paper_log.jsonl"

FILL_ASSUMED_MAKER = "assumed-maker-fill"
FILL_UNFILLED = "unfilled"
FILL_SIT_UNSCORED = "sit/unscored"

RESULT_PENDING = "pending"
RESULT_WIN = "win"
RESULT_LOSS = "loss"
RESULT_SIT = "sit"
RESULT_UNFILLED = "unfilled"
RESULT_UNSCORED = "unscored"

TERMINAL_RESULTS = frozenset(
    {RESULT_WIN, RESULT_LOSS, RESULT_SIT, RESULT_UNFILLED, RESULT_UNSCORED}
)
SCORED_RESULTS = frozenset({RESULT_WIN, RESULT_LOSS})
SIT_UNSCORED_RESULTS = frozenset({RESULT_SIT, RESULT_UNSCORED})


def assert_paper_path(path: Path) -> Path:
    """Refuse to mix paper rows into the live fill journal."""
    if path.name == LIVE_TRADE_LOG_NAME:
        raise ValueError("paper journal must not write to artifacts/trade_log.jsonl")
    return path


def spot_source_label(asset: str, source: str) -> str:
    if is_settlement_index(source):
        return official_index_label(asset, source)
    return "PROXY"


def is_scoreable_source(label: str, raw_source: str = "") -> bool:
    if str(label or "").strip().upper() in {"BRTI", "ERTI", "ETHUSD_RTI"}:
        return True
    return is_settlement_index(raw_source)


def paper_won(*, side: str, settlement_print: float, strike: float) -> bool:
    yes = official_yes(settlement_print=settlement_print, strike=strike)
    if str(side or "").strip().lower() == "yes":
        return yes
    return not yes


def paper_pnl(*, won: bool, contracts: int, fill_price: float, risk_dollars: float) -> float:
    """PnL at the paper fill price (maker limit), not Coinbase and not a live fill."""
    return estimate_pnl(
        won=won,
        contracts=contracts,
        entry_price=fill_price,
        risk_dollars=risk_dollars,
    )


def new_paper_row(
    *,
    ticker: str,
    asset: str,
    side: str,
    strike: float,
    spot: float,
    spot_source: str,
    minutes_left: float,
    fair: float,
    kalshi_price: float,
    limit_price: float,
    contracts: int,
    risk_dollars: float,
    net_edge: float,
    close_time: datetime | str | None,
    fill_model: str = FILL_ASSUMED_MAKER,
    hourly_vol: float = 0.0,
) -> dict[str, Any]:
    raw = str(spot_source or "").strip()
    if raw.upper() in {"BRTI", "ERTI", "ETHUSD_RTI"}:
        label = raw.upper()
    elif raw.upper() == "PROXY":
        label = "PROXY"
    elif is_settlement_index(raw):
        label = official_index_label(asset, raw)
    else:
        label = "PROXY"

    scoreable = is_scoreable_source(label, spot_source)
    if not scoreable:
        model = FILL_SIT_UNSCORED
        result = RESULT_SIT
        fill_status = FILL_SIT_UNSCORED
        filled = 0.0
    elif fill_model == FILL_UNFILLED:
        model = FILL_UNFILLED
        result = RESULT_UNFILLED
        fill_status = FILL_UNFILLED
        filled = 0.0
    else:
        model = FILL_ASSUMED_MAKER
        result = RESULT_PENDING
        fill_status = FILL_ASSUMED_MAKER
        filled = float(max(int(contracts), 0))

    close = parse_ts(close_time) if close_time and not isinstance(close_time, datetime) else close_time
    close_et = to_et(close) if isinstance(close, datetime) else None
    distance = strike_distance_pct(spot, strike)
    return {
        "ts": format_et(),
        "ts_iso": to_et().isoformat(),
        "kind": "paper",
        "ticker": ticker,
        "asset": asset,
        "side": side,
        "strike": strike,
        "spot": spot,
        "spot_source": label,
        "raw_spot_source": spot_source,
        "distance_pct": round(distance, 6),
        "minutes_left": round(minutes_left, 2),
        "fair": round(fair, 4),
        "model_pct": round(fair, 4),
        "kalshi_price": round(kalshi_price, 4),
        "limit_price": round(limit_price, 4),
        "fill_price": round(limit_price, 4),
        "contracts": contracts,
        "size": contracts,
        "risk_dollars": round(risk_dollars, 4),
        "net_edge": round(net_edge, 4),
        "hourly_vol": hourly_vol,
        "bucket": trade_bucket(side, distance),
        "close_time": close_et.isoformat() if close_et else None,
        "fill_model": model,
        "fill_status": fill_status,
        "filled_contracts": filled,
        "settlement_print": None,
        "settlement_index": label if label in {"BRTI", "ERTI", "ETHUSD_RTI"} else index_id_for(asset),
        "settlement_result": None,
        "result": result,
        "pnl": None if result == RESULT_PENDING else 0.0,
        "note": (
            "sit/unscored: PROXY or missing BRTI/ERTI — not a paper fill"
            if model == FILL_SIT_UNSCORED
            else "assumed-maker-fill at the printed limit (if we got that quote); not a real fill"
            if model == FILL_ASSUMED_MAKER
            else "stricter paper mode: left unfilled, not scored"
        ),
    }


def paper_row_from_idea(
    idea: Idea,
    *,
    spot_source: str,
    fill_model: str = FILL_ASSUMED_MAKER,
    hourly_vol: float = 0.0,
) -> dict[str, Any]:
    return new_paper_row(
        ticker=idea.market.ticker,
        asset=idea.market.asset,
        side=idea.side,
        strike=idea.market.threshold,
        spot=idea.spot,
        spot_source=spot_source,
        minutes_left=idea.minutes_left,
        fair=idea.fair,
        kalshi_price=idea.entry_price,
        limit_price=idea.limit_price,
        contracts=idea.contracts,
        risk_dollars=idea.risk_dollars,
        net_edge=idea.net_edge,
        close_time=idea.market.close_time,
        fill_model=fill_model,
        hourly_vol=hourly_vol,
    )


def load_paper(path: Path) -> list[dict[str, Any]]:
    return load_trades(assert_paper_path(path))


def write_paper(path: Path, rows: list[dict[str, Any]]) -> None:
    write_trades(assert_paper_path(path), rows)


def already_logged(rows: list[dict[str, Any]], ticker: str) -> bool:
    want = str(ticker or "").upper()
    return any(str(row.get("ticker") or "").upper() == want for row in rows)


def append_paper_ticket(path: Path, row: dict[str, Any]) -> dict[str, Any]:
    dest = assert_paper_path(path)
    existing = load_paper(dest)
    ticker = str(row.get("ticker") or "")
    if ticker and already_logged(existing, ticker):
        return row
    append_trade(dest, row)
    return row


def record_printed_ideas(
    path: Path,
    ideas: list[Idea],
    *,
    sources: dict[str, str],
    default_source: str = "",
    fill_model: str = FILL_ASSUMED_MAKER,
    hourly_vol: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Append one paper ticket per printed dry-scan idea (deduped by ticker)."""
    dest = assert_paper_path(path)
    written: list[dict[str, Any]] = []
    vols = hourly_vol or {}
    existing = load_paper(dest)
    for idea in ideas:
        ticker = idea.market.ticker
        if already_logged(existing, ticker) or already_logged(written, ticker):
            continue
        source = sources.get(idea.market.asset, default_source)
        row = paper_row_from_idea(
            idea,
            spot_source=source,
            fill_model=fill_model,
            hourly_vol=vols.get(idea.market.asset) or 0.0,
        )
        append_trade(dest, row)
        written.append(row)
        existing.append(row)
    return written


def hour_has_closed(close_time: object, now: datetime | None = None) -> bool:
    close = parse_ts(close_time) if not isinstance(close_time, datetime) else to_et(close_time)
    if close is None:
        return False
    return to_et(now) >= close


def fetch_official_print(
    client: Any,
    asset: str,
    close_time: datetime | str,
) -> float | None:
    """Official 60s BRTI/ERTI (hourly) or ETHUSD_RTI (15m) average. Never Coinbase last tick."""
    ids: list[str] = []
    for candidate in (index_id_for(asset), fifteen_index_id_for(asset)):
        if candidate and candidate not in ids:
            ids.append(candidate)
    getter = getattr(client, "get_cf_history", None)
    if not ids or getter is None:
        return None
    if hasattr(client, "can_trade") and not client.can_trade:
        return None
    close = parse_ts(close_time) if not isinstance(close_time, datetime) else close_time
    if close is None:
        return None
    for index_id in ids:
        if not index_id:
            continue
        for timespan in ("MINUTE", "HOUR"):
            stamp = history_query_timestamp(close, timespan=timespan)
            try:
                blob = getter(index_id, timestamp=stamp, timespan=timespan)
            except Exception as exc:  # noqa: BLE001
                logger.info("CF history %s %s failed: %s", index_id, timespan, exc)
                continue
            ticks = parse_cf_history_ticks(blob)
            average = average_settlement_window(ticks, close)
            if average:
                return average
    return None


def settle_paper_row(
    row: dict[str, Any],
    *,
    settlement_print: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mark win/loss from the official 60s average at the paper fill price."""
    if row.get("result") in TERMINAL_RESULTS:
        return row
    if str(row.get("fill_model") or "") == FILL_SIT_UNSCORED:
        return row
    if str(row.get("fill_model") or "") == FILL_UNFILLED:
        return row
    strike = float(row.get("strike") or 0)
    won = paper_won(
        side=str(row.get("side") or ""),
        settlement_print=float(settlement_print),
        strike=strike,
    )
    fill_price = float(row.get("fill_price") or row.get("limit_price") or 0)
    row["settlement_print"] = round(float(settlement_print), 4)
    row["settlement_result"] = "yes" if official_yes(settlement_print=float(settlement_print), strike=strike) else "no"
    row["result"] = RESULT_WIN if won else RESULT_LOSS
    row["pnl"] = paper_pnl(
        won=won,
        contracts=int(row.get("contracts") or row.get("size") or 0),
        fill_price=fill_price,
        risk_dollars=float(row.get("risk_dollars") or 0),
    )
    row["resolved_ts"] = format_et(now)
    row["resolved_ts_iso"] = to_et(now).isoformat()
    return row


def resolve_paper(
    rows: list[dict[str, Any]],
    get_print: Callable[[str, datetime | str], float | None],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Settle assumed-maker paper tickets after the hour. Unsettled stay pending."""
    for row in rows:
        if row.get("result") in TERMINAL_RESULTS:
            continue
        if str(row.get("fill_model") or "") in {FILL_SIT_UNSCORED, FILL_UNFILLED}:
            continue
        if not is_scoreable_source(str(row.get("spot_source") or ""), str(row.get("raw_spot_source") or "")):
            row["fill_model"] = FILL_SIT_UNSCORED
            row["result"] = RESULT_SIT
            row["fill_status"] = FILL_SIT_UNSCORED
            row["pnl"] = 0.0
            row["note"] = "sit/unscored: PROXY or missing BRTI/ERTI — not a paper fill"
            continue
        close = row.get("close_time")
        if close and not hour_has_closed(close, now):
            continue
        if not close:
            # No close time: cannot know the hour is over.
            continue
        asset = str(row.get("asset") or "")
        try:
            print_value = get_print(asset, close)
        except Exception as exc:  # noqa: BLE001
            logger.info("paper settlement print unavailable for %s: %s", row.get("ticker"), exc)
            continue
        if print_value is None or print_value <= 0:
            continue
        settle_paper_row(row, settlement_print=print_value, now=now)
    return rows


def settle_paper_file(
    path: Path,
    get_print: Callable[[str, datetime | str], float | None],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    dest = assert_paper_path(path)
    rows = resolve_paper(load_paper(dest), get_print, now=now)
    write_paper(dest, rows)
    return rows


def summarize_paper(rows: list[dict[str, Any]]) -> dict[str, Any]:
    assumed = [
        row
        for row in rows
        if row.get("result") in SCORED_RESULTS
        and str(row.get("fill_model") or "") == FILL_ASSUMED_MAKER
    ]
    wins = [row for row in assumed if row.get("result") == RESULT_WIN]
    losses = [row for row in assumed if row.get("result") == RESULT_LOSS]
    pending = [
        row
        for row in rows
        if row.get("result") == RESULT_PENDING
        or (
            row.get("result") not in TERMINAL_RESULTS
            and str(row.get("fill_model") or "") == FILL_ASSUMED_MAKER
        )
    ]
    sit = [
        row
        for row in rows
        if row.get("result") in SIT_UNSCORED_RESULTS
        or str(row.get("fill_model") or "") == FILL_SIT_UNSCORED
    ]
    unfilled = [
        row
        for row in rows
        if row.get("result") == RESULT_UNFILLED or str(row.get("fill_model") or "") == FILL_UNFILLED
    ]
    # A sit row should not also count as pending.
    pending = [row for row in pending if row not in sit and row not in unfilled and row not in assumed]
    pnl = sum(float(row.get("pnl") or 0) for row in assumed)
    return {
        "n_tickets": len(rows),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "n_assumed_filled_settled": len(assumed),
        "assumed_fill_pnl": round(pnl, 4),
        "n_pending": len(pending),
        "n_sit_unscored": len(sit),
        "n_unfilled": len(unfilled),
        "live": False,
    }


def format_paper_section(paper: dict[str, Any]) -> list[str]:
    return [
        "## Paper journal (`artifacts/paper_log.jsonl`)",
        "- Not live profitability. This tape is a counterfactual: dry-scan ideas only.",
        "- Default fill model is **assumed-maker-fill** at the printed maker limit",
        "  (the \"if we got that quote\" case). That is not a real Kalshi fill.",
        "- PROXY / missing BRTI/ERTI rows are sit/unscored — they are not paper fills.",
        "- Settlement is the official CF Benchmarks 60-second average (BRTI / ERTI),",
        "  not Coinbase last tick. Live fills stay in `artifacts/trade_log.jsonl`.",
        f"- Tickets: {paper['n_tickets']}",
        f"- Assumed-fill settled: {paper['n_assumed_filled_settled']} "
        f"({paper['n_wins']} win / {paper['n_losses']} loss)",
        f"- Assumed-fill PnL: ${paper['assumed_fill_pnl']:.2f}",
        f"- Pending (hour not settled or official print missing): {paper['n_pending']}",
        f"- Sit / unscored (PROXY or missing settlement index): {paper['n_sit_unscored']}",
        f"- Unfilled (stricter paper mode): {paper['n_unfilled']}",
        "- This is not live profitability and must not retune the 6% / close-strike / size rules.",
    ]


def try_settle_paper(settings: Any, client: Any | None = None) -> list[dict[str, Any]]:
    """Best-effort official-index settle. Failures leave tickets pending."""
    path = Path(getattr(settings, "paper_log_path", "") or DEFAULT_PAPER_LOG_NAME)
    if not path.is_file():
        return []
    owns = client is None
    if owns:
        from src.kalshi_client import KalshiClient

        client = KalshiClient(
            settings.kalshi_base_url,
            timeout=getattr(settings, "request_timeout_seconds", 20.0),
            api_key_id=getattr(settings, "kalshi_api_key_id", ""),
            private_key_path=getattr(settings, "kalshi_private_key_path", ""),
            trading_base_url=settings.trading_base_url,
        )
    try:
        return settle_paper_file(
            path,
            lambda asset, close: fetch_official_print(client, asset, close),
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("paper settle skipped: %s", exc)
        return load_paper(path) if path.is_file() else []
    finally:
        if owns and client is not None:
            closer = getattr(client, "close", None)
            if closer:
                closer()


def describe_paper_append(row: dict[str, Any]) -> str:
    ticker = row.get("ticker") or "?"
    side = row.get("side") or "?"
    model = row.get("fill_model") or ""
    if model == FILL_SIT_UNSCORED:
        return (
            f"PAPER: {ticker} {side} sit/unscored "
            f"({row.get('spot_source') or 'PROXY'} / missing settlement index — not a fill)"
        )
    if model == FILL_UNFILLED:
        return f"PAPER: {ticker} {side} left unfilled (stricter paper mode; not scored)"
    return (
        f"PAPER: logged {ticker} {side} @ {float(row.get('limit_price') or 0):.2f} "
        f"assumed-maker-fill (not a real fill; settle after the hour vs BRTI/ERTI)"
    )
