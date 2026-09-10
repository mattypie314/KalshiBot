"""CLI for the 15m BTC/ETH edge-loop bot."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from src.cfindex import FIFTEEN_INDEX_BY_ASSET, fifteen_index_id_for
from src.clock import configure_logging, format_et, to_et
from src.exposure import select_ideas_per_asset
from src.executor import CRYPTO_SHARD, FIFTEEN_SERIES, execute_ideas, is_fifteen_rest
from src.exits import manage_open_positions
from src.fees import taker_fee_dollars
from src.filters import Idea
from src.evaluate import summarize_trades
from src.journal import (
    append_trade,
    fill_status_from_order,
    first_parsed_count,
    load_trades,
    new_trade_row,
    order_filled_contracts,
    resolve_pending,
    write_trades,
)
from src.fifteen.config import (
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_RATE_LIMITED,
    FifteenSettings,
    load_fifteen_settings,
)
from src.fifteen.edge import (
    enough_room,
    fifteen_stake,
    fifteen_stopped,
    fifteen_window_id,
    fifteen_working,
    in_fifteen_entry_window,
    in_fifteen_revenge,
    news_blackout,
    pass_fail,
    record_fifteen_result,
    seconds_until_entry_window,
)
from src.fifteen.pot import credit_pot, load_pot, save_pot, set_open_risk
from src.fifteen.regime import chop_veto_note, classify_regime
from src.kalshi_client import AuthConfigError, ForbiddenError, KalshiClient, RateLimitedError
from src.markets import HourlyMarket, MarketDiscovery
from src.model import fair_prob, hours_left, model_z
from src.paper import FILL_ASSUMED_MAKER, record_printed_ideas, try_settle_paper
from src.sizer import (
    economic_risk_dollars,
    labeled_limit_from_yes_book,
    maker_cost_per_contract,
    size_idea,
    yes_book_price,
)
from src.data_fetcher import signals_for_asset
from src.indicators import tape_from_15m_signals
from src.spot import SpotService

logger = logging.getLogger(__name__)

HALTED_MESSAGE = (
    "HALTED: live trading is off. HALTED=true refuses live even with --confirm LIVE. "
    "Set HALTED=false only when you mean to resume."
)


def _client(settings: FifteenSettings) -> KalshiClient:
    return KalshiClient(
        settings.trading_base_url,
        timeout=settings.request_timeout_seconds,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
        trading_base_url=settings.trading_base_url,
    )


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"tickets": [], "rests": []}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"tickets": [], "rests": []}
    return data if isinstance(data, dict) else {"tickets": [], "rests": []}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, default=str) + "\n")


def bankroll_from_balance(payload: object, fallback: float) -> float:
    if not isinstance(payload, dict):
        return fallback
    for key in ("total_value", "total_value_dollars", "balance_dollars", "balance", "cash"):
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if key in {"balance", "cash"} and value > 1000:
            value /= 100.0
        if value > 0:
            return value
    return fallback


def vol_fallback(settings: FifteenSettings, asset: str) -> float:
    if asset == "ETH":
        return settings.hourly_vol_fallback_eth
    return settings.hourly_vol_fallback_btc


def idea_from_pass(
    market: HourlyMarket,
    decision: Any,
    *,
    spot: float,
    vol: float,
    bankroll: float,
    room: float,
    settings: FifteenSettings,
    now: datetime,
) -> Idea | None:
    if not decision.passed:
        return None
    side = "Yes" if str(decision.side).lower() == "yes" else "No"
    join = float(decision.join_price)
    # join_price is on the Yes book. No joins the Yes ask (sell Yes). Size
    # from the dollars Kalshi locks — the No complement — not the cheap ask.
    limit = labeled_limit_from_yes_book(side, join)
    if not 0 < limit < 1:
        return None
    fair = float(decision.model_prob)
    # Size to preferred risk, never past pot room or hard caps.
    # fifteen_stake() is a 3–5% helper for tests — not the live ticket budget.
    stake_cap = min(settings.preferred_risk_dollars, settings.max_risk_dollars, room)
    if stake_cap <= 0 or room + 1e-9 < limit:
        return None
    sized = size_idea(
        bankroll=max(bankroll, settings.pot_start),
        entry_price=limit,
        p_hat=fair,
        kelly_mult=settings.kelly_mult,
        max_risk_pct=1.0,  # pot room already caps dollars
        max_risk_dollars=stake_cap,
        preferred_risk_dollars=stake_cap,
        cost_price=limit,
    )
    if sized.skip or sized.contracts < 1:
        return None
    secs = max(0.0, (market.close_time - now).total_seconds())
    hrs = hours_left(secs) or secs / 3600.0
    z = model_z(spot, market.threshold, vol, hrs)
    fee_total = taker_fee_dollars(sized.contracts, limit)
    fee_each = fee_total / sized.contracts
    gross = fair - limit
    return Idea(
        market=market,
        side=side,
        entry_price=limit,
        limit_price=limit,
        fair=fair,
        gross_edge=gross,
        net_edge=gross,
        fee_per_contract=fee_each,
        fee_total=fee_total,
        z=z,
        hours_left=hrs,
        contracts=sized.contracts,
        risk_dollars=sized.risk_dollars,
        max_loss=sized.risk_dollars,
        rationale=[
            decision.line,
            f"maker join Yes {join:.2f} as {side} (cost {limit:.2f})",
            f"pot room ${room:.2f}; risk ${sized.risk_dollars:.2f}",
        ],
        post_maker=True,
        strike_distance_pct=abs(market.threshold - spot) / spot if spot else 0.0,
        spot=spot,
        minutes_left=secs / 60.0,
        bucket="fifteen_edge",
    )


def collect_ideas(
    settings: FifteenSettings,
    *,
    client: KalshiClient,
    state: dict[str, Any],
    pot_room: float,
    bankroll: float,
    asset: str | None = None,
    now: datetime | None = None,
    apply_chop_veto: bool | None = None,
) -> tuple[list[Idea], list[str], Any]:
    now = to_et(now)
    notes: list[str] = []
    requested = [asset.upper()] if asset else list(settings.asset_list)
    working = [name for name in requested if fifteen_working(state, now, asset=name)]
    assets = [name for name in requested if name not in working]
    # Live and paper share this stack. FIFTEEN_CHOP_VETO=false is the only off switch.
    veto_chop = settings.chop_veto if apply_chop_veto is None else apply_chop_veto
    regimes: dict[str, Any] = {}

    if settings.news_pause:
        return [], ["NEWS_PAUSE — operator sit"], None
    news = news_blackout(now)
    if news:
        return [], [f"news blackout ({news})"], None
    if fifteen_stopped(state, now):
        return [], ["15m session stopped (3 losses)"], None
    if in_fifteen_revenge(state, now):
        return [], ["revenge window after a loser"], None
    if working:
        note = "already working a 15m ticket this window"
        if assets:
            note = f"{note} on {' and '.join(working)}"
        notes.append(note)
        if not assets:
            return [], notes, None
    if not in_fifteen_entry_window(now):
        notes.append(f"outside entry window (minute {now.minute % 15}; want 3-5)")

    spots_svc = SpotService(
        preferred=settings.spot_source,
        kalshi=client,
        index_id_fn=fifteen_index_id_for,
        vol_lookback_minutes=settings.vol_lookback_minutes,
        settlement_labels=dict(FIFTEEN_INDEX_BY_ASSET),
    )
    try:
        spots = spots_svc.snapshot(
            requested,
            fallbacks={
                "BTC": settings.hourly_vol_fallback_btc,
                "ETH": settings.hourly_vol_fallback_eth,
            },
        )

        markets = MarketDiscovery(client).discover_fifteen(
            assets,
            now=now,
            max_per_asset=settings.max_markets_per_asset,
            spots=spots.prices,
            require_exchange_index=CRYPTO_SHARD,
        )
        if not markets:
            notes.append("no live KXBTC15M/KXETH15M books")
            return [], notes, spots
        if not in_fifteen_entry_window(now):
            return [], notes, spots

        candidates: list[Idea] = []
        tape_cache: dict[str, Any] = {}
        for market in markets:
            spot = spots.prices.get(market.asset)
            vol = spots.hourly_vol.get(market.asset) or vol_fallback(settings, market.asset)
            if not spot:
                notes.append(f"{market.asset}: no spot")
                continue
            if settings.require_settlement_index and not spots.settlement_ok(market.asset):
                notes.append(f"{market.asset}: PROXY spot — sit")
                continue
            secs = (market.close_time - now).total_seconds()
            hrs = hours_left(secs)
            if hrs is None:
                continue
            if market.asset not in tape_cache:
                tape = None
                # Closed 15m CCXT+pandas-ta signals (RSI/MACD/BB/ADX).
                # 1m ADX/BB chop stays on regime.classify_regime — do not double-gate
                # the same bars with a second threshold set.
                try:
                    payload = signals_for_asset(market.asset)
                    tape = tape_from_15m_signals(payload)
                    if tape is not None:
                        logger.info(
                            "15m tape %s RSI=%.1f ADX=%.1f MACDh=%.4f BBw=%s",
                            market.asset,
                            tape.rsi or 0,
                            tape.adx or 0,
                            tape.macd_hist or 0,
                            f"{tape.bb_bandwidth:.4f}" if tape.bb_bandwidth is not None else "?",
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.info("15m CCXT tape failed for %s: %s", market.asset, exc)
                tape_cache[market.asset] = tape
            tape = tape_cache[market.asset]
            decision = pass_fail(
                model_yes=fair_prob(spot, market.threshold, vol, hrs),
                yes_bid=market.yes_bid,
                yes_ask=market.yes_ask,
                secs_left=secs,
                sigma=model_z(spot, market.threshold, vol, hrs),
                tape=tape,
            )
            if not decision.passed:
                notes.append(f"{market.ticker}: {decision.line}")
                continue
            if market.spread > settings.max_spread + 1e-12 and abs(decision.edge) <= market.spread:
                notes.append(f"{market.ticker}: spread wider than edge")
                continue
            if veto_chop:
                if market.asset not in regimes:
                    bars = (getattr(spots, "candles", None) or {}).get(market.asset)
                    regimes[market.asset] = classify_regime(bars)
                skipped = chop_veto_note(market.ticker, regimes[market.asset])
                if skipped:
                    notes.append(skipped)
                    continue
            idea = idea_from_pass(
                market,
                decision,
                spot=spot,
                vol=vol,
                bankroll=bankroll,
                room=pot_room,
                settings=settings,
                now=now,
            )
            if idea is None:
                notes.append(f"{market.ticker}: PASS but size/room failed")
                continue
            candidates.append(idea)

        candidates.sort(key=lambda i: abs(i.net_edge), reverse=True)
        chosen, extra = select_ideas_per_asset(
            candidates,
            max_per_asset=1,
            max_ideas=settings.max_ideas_per_run,
        )
        for idea in extra:
            notes.append(
                f"{idea.market.ticker}: held back (one per asset; "
                f"max {settings.max_ideas_per_run}/run)"
            )
        return chosen, notes, spots
    finally:
        spots_svc.close()



def append_scan_log(
    settings: FifteenSettings,
    *,
    mode: str,
    ideas: list[Idea],
    notes: list[str],
    spots: Any,
    window_id: str | None = None,
) -> None:
    path = Path(settings.scan_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": format_et(),
        "mode": mode,
        "window_id": window_id or fifteen_window_id(),
        "ideas": [
            {
                "ticker": i.market.ticker,
                "side": i.side,
                "limit": i.limit_price,
                "fair": i.fair,
                "contracts": i.contracts,
                "risk": i.risk_dollars,
            }
            for i in ideas
        ],
        "notes": notes[:20],
        "spots": getattr(spots, "prices", {}) if spots else {},
    }
    with path.open("a") as handle:
        handle.write(json.dumps(row, default=str) + "\n")


def idea_fingerprint(idea: Idea) -> tuple[str, str, float, int]:
    """Identity of a 15m Pass used to prove paper and live share a tick."""
    return (
        str(idea.market.ticker),
        str(idea.side),
        round(float(idea.limit_price), 4),
        int(idea.contracts),
    )


def paper_ideas_for_window(
    ideas: list[Idea],
    *,
    force_live: bool,
    place: bool,
    live_decided: bool,
) -> list[Idea]:
    """Which ideas to assume-fill on paper for this 15m window.

    The live tick is the source of truth: paper shadows that Pass/Sit list.
    A later scan must not journal a different decision. Classic dry boards
    (scan with no live decision yet) still paper Passes. ``once`` stays dry.
    """
    if force_live:
        return list(ideas)
    if live_decided:
        return []
    if place:
        return []
    return list(ideas)


def stamp_live_decision(
    state: dict[str, Any],
    *,
    window_id: str,
    ideas: list[Idea],
) -> dict[str, Any]:
    """Record this window's live Pass/Sit so a later scan cannot re-decide."""
    stamp = {
        "window_id": window_id,
        "tickers": [idea.market.ticker for idea in ideas],
        "n": len(ideas),
        "ts": format_et(),
    }
    state["live_decision"] = stamp
    return stamp


def adopt_live_decision(
    state: dict[str, Any],
    path: Path,
    window_id: str,
) -> dict[str, Any] | None:
    """Keep a parallel live oneshot's stamp; do not clobber it on save."""
    ours = state.get("live_decision")
    if isinstance(ours, dict) and str(ours.get("window_id") or "") == window_id:
        return ours
    if path.is_file():
        disk = load_state(path)
        disk_dec = disk.get("live_decision") if isinstance(disk, dict) else None
        if isinstance(disk_dec, dict) and str(disk_dec.get("window_id") or "") == window_id:
            state["live_decision"] = disk_dec
            return disk_dec
    return None


def persist_fifteen_state(
    path: Path,
    state: dict[str, Any],
    *,
    window_id: str,
) -> None:
    adopt_live_decision(state, path, window_id)
    save_state(path, state)


def live_decision_for_window(
    state: dict[str, Any],
    *,
    window_id: str,
    scan_log_path: Path | None = None,
    state_path: Path | None = None,
) -> dict[str, Any] | None:
    """Live Pass/Sit already recorded for this window, or None if scan-only."""
    if state_path is not None:
        adopted = adopt_live_decision(state, state_path, window_id)
        if adopted is not None:
            return adopted
    stamp = state.get("live_decision")
    if isinstance(stamp, dict) and str(stamp.get("window_id") or "") == window_id:
        return stamp
    if scan_log_path is None or not Path(scan_log_path).is_file():
        return None
    found: dict[str, Any] | None = None
    for row in load_trades(Path(scan_log_path)):
        if str(row.get("mode") or "") != "live":
            continue
        if str(row.get("window_id") or "") != window_id:
            continue
        tickers = [
            str(item.get("ticker") or "")
            for item in (row.get("ideas") or [])
            if isinstance(item, dict) and item.get("ticker")
        ]
        found = {
            "window_id": window_id,
            "tickers": tickers,
            "n": len(tickers),
            "source": "scan_log",
        }
    return found


def _is_live_entry(row: dict[str, Any]) -> bool:
    """True for a live 15m place row — not paper and not an exit event."""
    if str(row.get("kind") or "") == "paper":
        return False
    if str(row.get("action") or "") == "exit":
        return False
    return True


def _already_journaled(trades: list[dict[str, Any]], *, order_id: str, ticker: str) -> bool:
    """True when this live place is already a journal Pass.

    A new Kalshi `order_id` is a new place — never skip it just because the
    ticker already has a pending row (prior window leftover or a rest that
    was replaced).
    """
    want_order = str(order_id or "")
    want_ticker = str(ticker or "").upper()
    for row in trades:
        if not _is_live_entry(row):
            continue
        got_order = str(row.get("order_id") or "")
        if want_order and got_order == want_order:
            return True
        if want_order:
            continue
        if want_ticker and str(row.get("ticker") or "").upper() == want_ticker:
            if row.get("exit_reason"):
                continue
            if str(row.get("result") or "pending") == "pending":
                return True
    return False


def _flatten_order(order: dict[str, Any]) -> dict[str, Any]:
    nested = order.get("order")
    if not isinstance(nested, dict):
        return order
    merged = dict(nested)
    for key, value in order.items():
        if key == "order":
            continue
        if value not in (None, "") or merged.get(key) in (None, ""):
            merged[key] = value
    return merged


def _order_ticker(order: dict[str, Any]) -> str:
    return str(order.get("ticker") or order.get("market_ticker") or "").strip()


def _asset_from_ticker(ticker: str) -> str:
    code = str(ticker or "").upper().split("-", 1)[0]
    if "ETH" in code:
        return "ETH"
    if "BTC" in code:
        return "BTC"
    return ""


def _side_from_order(order: dict[str, Any]) -> str:
    for key in ("outcome_side", "side"):
        text = str(order.get(key) or "").strip().lower()
        if text in {"yes", "y", "bid"}:
            return "Yes"
        if text in {"no", "n", "ask"}:
            return "No"
    return ""


def _economic_place_risk(
    *,
    side: str,
    contracts: int,
    labeled_limit: float,
    yes_book: float,
) -> float:
    """Journal dollars actually at risk, not cheap-side limit × contracts."""
    if contracts < 1:
        return 0.0
    if yes_book and 0 < yes_book < 1:
        cost = maker_cost_per_contract(side, yes_book=yes_book)
    else:
        cost = maker_cost_per_contract(side, labeled_limit=labeled_limit)
    return economic_risk_dollars(contracts, cost)


def _price_from_order(order: dict[str, Any]) -> float:
    for key in (
        "yes_price_dollars",
        "average_fill_price",
        "price",
        "yes_price",
        "kalshi_price",
        "limit_price",
    ):
        raw = order.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if 0 < value < 1:
            return value
    return 0.0


def _payload_for_order(order: dict[str, Any], payloads: list[dict[str, Any]]) -> dict[str, Any]:
    cid = str(order.get("client_order_id") or "")
    if cid:
        for payload in payloads:
            if str(payload.get("client_order_id") or "") == cid:
                return payload
    ticker = _order_ticker(order).upper()
    if ticker:
        for payload in payloads:
            if str(payload.get("ticker") or "").upper() == ticker:
                return payload
    return {}


def _match_idea_for_place(
    *,
    ticker: str,
    ideas: list[Idea],
    used: set[int],
) -> Idea | None:
    want = str(ticker or "").upper()
    if want:
        for idea in ideas:
            if id(idea) in used:
                continue
            if idea.market.ticker.upper() == want:
                return idea
        return None
    leftover = [idea for idea in ideas if id(idea) not in used]
    if len(leftover) == 1:
        return leftover[0]
    return None


def _mark_ticket_resolved(state: dict[str, Any], ticker: str, *, result: str, pnl: object) -> None:
    for key in ("tickets", "rests"):
        for row in state.get(key) or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("ticker") or "") != ticker:
                continue
            if str(row.get("status") or "") != "open":
                continue
            row["status"] = "unfilled" if result == "unfilled" else "settled"
            row["result"] = result
            row["pnl"] = pnl


def _open_journal_risk(trades: list[dict[str, Any]]) -> float:
    total = 0.0
    for row in trades:
        if not _is_live_entry(row):
            continue
        if str(row.get("result") or "pending") in {"win", "loss", "unfilled"}:
            continue
        if row.get("exit_reason"):
            continue
        try:
            total += float(row.get("risk_dollars") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _safe_fills(client: KalshiClient) -> tuple[list[dict[str, Any]], bool]:
    if not client.can_trade:
        return [], False
    try:
        return list(client.get_fills(limit=50) or []), True
    except Exception as exc:  # noqa: BLE001
        logger.info("15m fills unavailable: %s", exc)
        return [], False


def refresh_live_journal(
    settings: FifteenSettings,
    *,
    client: KalshiClient,
    state: dict[str, Any],
    pot: Any,
) -> list[dict[str, Any]]:
    """Resolve live 15m journal fills/settlements. Never writes paper assumed fills."""
    from src.main import market_result_is_loss

    journal_path = Path(settings.trade_log_path)
    trades = load_trades(journal_path)
    prior = {id(row): str(row.get("result") or "pending") for row in trades}
    fills, fills_available = _safe_fills(client)
    getter = getattr(client, "get_market", None)
    if getter is not None:
        trades = resolve_pending(
            trades,
            getter,
            market_result_is_loss,
            fills=fills,
            fills_available=fills_available,
        )
        write_trades(journal_path, trades)
    for row in trades:
        if not _is_live_entry(row):
            continue
        result = str(row.get("result") or "pending")
        if result not in {"win", "loss", "unfilled"}:
            continue
        if prior.get(id(row)) in {"win", "loss", "unfilled"}:
            continue
        ticker = str(row.get("ticker") or "")
        pnl = row.get("pnl")
        _mark_ticket_resolved(state, ticker, result=result, pnl=pnl)
        if result in {"win", "loss"}:
            try:
                dollars = float(pnl or 0)
            except (TypeError, ValueError):
                dollars = 0.0
            msg = credit_pot(pot, dollars, note=f"{ticker} {result}")
            stop = record_fifteen_result(state, dollars)
            print(
                f"LIVE settled {ticker} {result} pnl={dollars:+.2f} "
                f"(pot ${pot.balance:.2f})"
            )
            if msg:
                print(msg)
            if stop:
                print(stop)
        else:
            print(f"LIVE unfilled {ticker} (not scored)")
    set_open_risk(pot, _open_journal_risk(trades))
    return trades


def journal_live_places(
    settings: FifteenSettings,
    *,
    ideas: list[Idea],
    result: dict[str, Any],
    spots: Any,
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Append one live journal row per successful 15m place. Paper stays separate.

    Always writes a Pass for every placed order. Kalshi V2 place responses often
    omit `ticker`; match via `market_ticker`, `client_order_id`, or synthesize.
    """
    journal_path = Path(settings.trade_log_path)
    trades = load_trades(journal_path)
    order_payloads = [row for row in (result.get("orders") or []) if isinstance(row, dict)]
    prices = getattr(spots, "prices", {}) or {}
    vols = getattr(spots, "hourly_vol", {}) or {}
    source = getattr(spots, "source", "") or ""
    sources = getattr(spots, "sources", {}) or {}
    written: list[dict[str, Any]] = []
    used_ideas: set[int] = set()
    for raw in result.get("placed") or []:
        if not isinstance(raw, dict):
            continue
        order = _flatten_order(raw)
        payload = _payload_for_order(order, order_payloads)
        ticker = _order_ticker(order) or str(payload.get("ticker") or "")
        order_id = str(order.get("order_id") or "")
        client_order_id = str(
            order.get("client_order_id") or payload.get("client_order_id") or ""
        )
        idea = _match_idea_for_place(ticker=ticker, ideas=ideas, used=used_ideas)
        matched_ticker = bool(
            idea is not None and ticker and idea.market.ticker.upper() == ticker.upper()
        )
        if idea is None:
            logger.warning(
                "15m live place: idea match failed ticker=%s order_id=%s "
                "client_order_id=%s; synthesizing journal row from order",
                ticker or "?",
                order_id or "?",
                client_order_id or "?",
            )
            print(
                f"LIVE journal: no idea matched ticker={ticker or '?'} "
                f"order_id={order_id or '?'} — writing synthesized Pass row",
                flush=True,
            )
        elif not matched_ticker:
            logger.warning(
                "15m live place: ticker mismatch order=%s idea=%s order_id=%s; "
                "journaling against best-effort idea",
                ticker or "?",
                idea.market.ticker,
                order_id or "?",
            )
            ticker = idea.market.ticker
        else:
            ticker = idea.market.ticker
        if idea is not None:
            used_ideas.add(id(idea))
        if _already_journaled(trades, order_id=order_id, ticker=ticker):
            continue
        if idea is not None:
            asset = idea.market.asset
            side = idea.side
            strike = idea.market.threshold
            spot = idea.spot or prices.get(asset) or 0.0
            minutes_left = idea.minutes_left
            fair = idea.fair
            kalshi_price = idea.entry_price
            limit_price = idea.limit_price
            contracts = idea.contracts
            merged = dict(payload)
            merged.update({k: v for k, v in order.items() if v not in (None, "")})
            risk_dollars = _economic_place_risk(
                side=side,
                contracts=contracts,
                labeled_limit=limit_price,
                yes_book=_price_from_order(merged) or yes_book_price(side, limit_price),
            )
            hourly_vol = vols.get(asset) or 0.0
            spot_source = sources.get(asset) or source
        else:
            merged = dict(payload)
            merged.update({k: v for k, v in order.items() if v not in (None, "")})
            asset = _asset_from_ticker(ticker)
            side = _side_from_order(merged) or "Yes"
            strike = 0.0
            spot = prices.get(asset) or 0.0
            minutes_left = 0.0
            fair = 0.0
            kalshi_price = _price_from_order(merged)
            if side == "No" and str(merged.get("side") or "").lower() == "ask" and kalshi_price:
                limit_price = round(1.0 - kalshi_price, 4)
            else:
                limit_price = kalshi_price
            count = order_filled_contracts(merged)
            if count <= 0:
                count = first_parsed_count(
                    merged,
                    ("initial_count_fp", "count_fp", "count", "remaining_count_fp", "remaining_count"),
                )
            contracts = int(count) if count >= 1 else 0
            risk_dollars = _economic_place_risk(
                side=side,
                contracts=contracts,
                labeled_limit=limit_price,
                yes_book=kalshi_price,
            )
            hourly_vol = vols.get(asset) or 0.0
            spot_source = sources.get(asset) or source
        row = new_trade_row(
            ticker=ticker,
            asset=asset,
            side=side,
            strike=strike,
            spot=spot,
            minutes_left=minutes_left,
            fair=fair,
            kalshi_price=kalshi_price,
            limit_price=limit_price,
            contracts=contracts,
            risk_dollars=risk_dollars,
            hourly_vol=hourly_vol,
            source=spot_source,
            order_id=order_id,
            client_order_id=client_order_id,
            fill_status=fill_status_from_order(order),
            filled_contracts=order_filled_contracts(order),
        )
        row["window_id"] = fifteen_window_id()
        append_trade(journal_path, row)
        trades.append(row)
        written.append(row)
        for ticket in state.get("tickets") or []:
            if not isinstance(ticket, dict):
                continue
            if str(ticket.get("ticker") or "").upper() != str(ticker or "").upper():
                continue
            if str(ticket.get("status") or "") != "open":
                continue
            if not ticket.get("order_id"):
                ticket["order_id"] = order_id
                ticket["client_order_id"] = client_order_id
                ticket["fill_status"] = row["fill_status"]
                ticket["risk"] = risk_dollars
    return written


def _record_window_paper(
    settings: FifteenSettings,
    ideas: list[Idea],
    spots: Any,
    *,
    window_id: str,
    shadow: str,
) -> list[dict[str, Any]]:
    if not ideas:
        return []
    return record_printed_ideas(
        Path(settings.paper_log_path),
        ideas,
        sources=(spots.sources if spots else {}),
        default_source=(spots.source if spots else ""),
        fill_model=FILL_ASSUMED_MAKER,
        hourly_vol=(spots.hourly_vol if spots else None),
        extra={"window_id": window_id, "shadow": shadow},
    )


def run_scan(
    settings: FifteenSettings,
    *,
    asset: str | None,
    place: bool,
    force_live: bool,
    armed: bool = False,
) -> int:
    Path(settings.artifacts_dir).mkdir(parents=True, exist_ok=True)
    # Early oneshots (stale :01 timer, manual kick at :00–:02) wait for minutes
    # 3–5. Past the window → collect_ideas sits without sleeping into the next block.
    wait_s = seconds_until_entry_window()
    if wait_s and wait_s > 0:
        print(
            f"waiting {wait_s:.0f}s for 15m entry window (minutes 3–5)…",
            flush=True,
        )
        time.sleep(wait_s)

    state_path = Path(settings.state_path)
    state = load_state(state_path)
    wid = fifteen_window_id()
    pot = load_pot(settings.pot_path)
    pot.start = settings.pot_start
    pot.double_at = settings.pot_double

    if force_live and settings.halted:
        print(HALTED_MESSAGE)
        return EXIT_CONFIG

    client = _client(settings)
    bankroll = settings.bankroll
    try:
        if client.can_trade:
            bankroll = bankroll_from_balance(
                client.get_balance(), max(pot.room, settings.bankroll)
            )
    except Exception as exc:  # noqa: BLE001
        logger.info("balance probe failed: %s", exc)

    try:
        try_settle_paper(settings, client)
    except Exception as exc:  # noqa: BLE001
        logger.info("paper settle skipped: %s", exc)

    journal_path = Path(settings.trade_log_path)
    trades = refresh_live_journal(settings, client=client, state=state, pot=pot)
    fills, fills_available = _safe_fills(client)
    if place or force_live:
        exit_live = bool(force_live and armed and not settings.halted and client.can_trade)
        manage_open_positions(
            client,
            state=state,
            settings=settings,
            trades=trades,
            fills=fills,
            fills_available=fills_available,
            live=exit_live,
            journal_path=journal_path,
            series=FIFTEEN_SERIES,
            exchange_index=CRYPTO_SHARD,
        )
        persist_fifteen_state(state_path, state, window_id=wid)
        save_pot(pot, settings.pot_path)

    if force_live and pot.stopped:
        print(f"15m pot stopped at ${pot.balance:.2f}. Refusing new live entries.")
        save_pot(pot, settings.pot_path)
        persist_fifteen_state(state_path, state, window_id=wid)
        return EXIT_OK

    try:
        ideas, notes, spots = collect_ideas(
            settings,
            client=client,
            state=state,
            pot_room=pot.room,
            bankroll=bankroll,
            asset=asset,
            apply_chop_veto=bool(settings.chop_veto),
        )
    except RateLimitedError as exc:
        print(f"rate limited: {exc}", file=sys.stderr)
        return EXIT_RATE_LIMITED
    except ForbiddenError as exc:
        print(f"forbidden: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except AuthConfigError as exc:
        print(f"auth failed: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    mode = "live" if force_live else ("once" if place else "scan")
    print(f"=== 15m BTC/ETH edge loop ({mode}) @ {format_et()} ===")
    print(
        f"pot ${pot.balance:.2f} (room ${pot.room:.2f}) | bankroll ${bankroll:.2f} | "
        f"window {wid} | halted={settings.halted}"
    )
    if pot.ask_to_continue:
        print(f"POT DOUBLE: ${pot.balance:.2f} >= ${pot.double_at:.2f} — ask Matt.")
    if spots is not None:
        for name, price in spots.prices.items():
            src = spots.sources.get(name, spots.source)
            tag = "settlement" if spots.settlement_ok(name) else "PROXY"
            print(f"  {name} {price:.2f} ({src}, {tag})")

    # Live computes the window once. Stamp immediately so a later/parallel
    # scan cannot paper a different Pass/Sit.
    if force_live:
        stamp_live_decision(state, window_id=wid, ideas=ideas)
        persist_fifteen_state(state_path, state, window_id=wid)

    decided = live_decision_for_window(
        state,
        window_id=wid,
        scan_log_path=Path(settings.scan_log_path),
        state_path=state_path,
    )
    to_paper = paper_ideas_for_window(
        ideas,
        force_live=force_live,
        place=place,
        live_decided=decided is not None,
    )

    if not ideas:
        print("NO_ACTIONABLE_EDGE")
        for note in notes[:12]:
            print(f"  sit: {note}")
    else:
        for idea in ideas:
            print(
                f"PASS {idea.market.ticker} {idea.side} @ {idea.limit_price:.2f} "
                f"x {idea.contracts} (fair {idea.fair:.2f}, edge {idea.net_edge:+.2f}, "
                f"risk ${idea.risk_dollars:.2f})"
            )
            for line in idea.rationale:
                print(f"  · {line}")

    if to_paper:
        written = _record_window_paper(
            settings,
            to_paper,
            spots,
            window_id=wid,
            shadow="live" if force_live else "scan",
        )
        for row in written:
            print(f"PAPER: logged {row.get('ticker')} (not live PnL)")
    elif decided is not None and not force_live and not place:
        print(
            f"PAPER: shadowing live decision for window {wid} "
            "(not a later scan)"
        )

    if not ideas or (not place and not force_live):
        persist_fifteen_state(state_path, state, window_id=wid)
        save_pot(pot, settings.pot_path)
        append_scan_log(
            settings, mode=mode, ideas=ideas, notes=notes, spots=spots, window_id=wid
        )
        return EXIT_OK

    go_live = bool(force_live and armed and not settings.halted and not pot.stopped)
    if force_live and not go_live:
        print("Live not armed — dry-run payloads only.")

    result = execute_ideas(
        ideas,
        client=client,
        artifacts_dir=settings.artifacts_dir,
        live=go_live,
        confirm_live=go_live,
        cancel_stale=True,
        rest_filter=is_fifteen_rest,
        exchange_index=CRYPTO_SHARD,
    )
    if go_live and result.get("placed"):
        # Journal first (handles V2 responses that omit ticker), then mirror
        # those rows into state tickets so fifteen_working stays accurate.
        journaled = journal_live_places(
            settings, ideas=ideas, result=result, spots=spots, state=state
        )
        open_tickers = {
            str(ticket.get("ticker") or "").upper()
            for ticket in state.get("tickets") or []
            if str(ticket.get("window_id") or "") == wid
            and str(ticket.get("status") or "") == "open"
        }
        ideas_by_ticker = {idea.market.ticker.upper(): idea for idea in ideas}
        for row in journaled:
            ticker = str(row.get("ticker") or "").upper()
            if not ticker or ticker in open_tickers:
                continue
            idea = ideas_by_ticker.get(ticker)
            state.setdefault("tickets", []).append(
                {
                    "status": "open",
                    "loop": "fifteen",
                    "window_id": wid,
                    "ticker": row.get("ticker") or (idea.market.ticker if idea else ""),
                    "asset": row.get("asset") or (idea.market.asset if idea else ""),
                    "side": row.get("side") or (idea.side if idea else ""),
                    "contracts": row.get("contracts")
                    if row.get("contracts") is not None
                    else (idea.contracts if idea else 0),
                    "limit": row.get("limit_price")
                    if row.get("limit_price") is not None
                    else (idea.limit_price if idea else 0),
                    "risk": row.get("risk_dollars")
                    if row.get("risk_dollars") is not None
                    else (idea.risk_dollars if idea else 0),
                    "order_id": row.get("order_id") or "",
                    "client_order_id": row.get("client_order_id") or "",
                    "fill_status": row.get("fill_status") or "",
                }
            )
            open_tickers.add(ticker)
        set_open_risk(pot, _open_journal_risk(load_trades(journal_path)))
        print(f"LIVE: placed {len(result['placed'])} 15m maker limit(s).")
        for row in journaled:
            print(
                f"LIVE journal {row.get('ticker')} {row.get('side')} "
                f"@{row.get('limit_price')} order_id={row.get('order_id') or '?'} "
                f"fill_status={row.get('fill_status')}"
            )
    elif place or force_live:
        print("DRY-RUN: order payloads written (not live).")

    persist_fifteen_state(state_path, state, window_id=wid)
    save_pot(pot, settings.pot_path)
    append_scan_log(
        settings, mode=mode, ideas=ideas, notes=notes, spots=spots, window_id=wid
    )
    return EXIT_OK


def run_auth(settings: FifteenSettings) -> int:
    try:
        client = _client(settings)
        payload = client.get_balance()
    except Exception as exc:  # noqa: BLE001
        print(f"AUTH FAIL: {exc}")
        return EXIT_CONFIG
    cash = (
        payload.get("balance_dollars") or payload.get("balance")
        if isinstance(payload, dict)
        else payload
    )
    host = "demo" if settings.use_demo else "prod"
    print(f"AUTH OK ({host}). Balance field: {cash}")
    return EXIT_OK


def run_eval(settings: FifteenSettings) -> int:
    try:
        try_settle_paper(settings)
    except Exception as exc:  # noqa: BLE001
        logger.info("paper settle: %s", exc)
    try:
        client = _client(settings)
        state = load_state(Path(settings.state_path))
        pot = load_pot(settings.pot_path)
        refresh_live_journal(settings, client=client, state=state, pot=pot)
        save_state(Path(settings.state_path), state)
        save_pot(pot, settings.pot_path)
    except Exception as exc:  # noqa: BLE001
        logger.info("live journal settle skipped: %s", exc)
        pot = load_pot(settings.pot_path)

    paper_path = Path(settings.paper_log_path)
    live_path = Path(settings.trade_log_path)
    print(f"=== 15m eval (paper={paper_path}) ===")
    if paper_path.is_file():
        rows = [json.loads(line) for line in paper_path.read_text().splitlines() if line.strip()]
        print(f"paper tickets: {len(rows)}")
        for row in rows[-10:]:
            print(
                f"  {row.get('ticker')} {row.get('side')} "
                f"result={row.get('result')} pnl={row.get('pnl')}"
            )
    else:
        print("no paper log yet")

    live_rows = [
        row
        for row in load_trades(live_path)
        if _is_live_entry(row)
    ]
    live = summarize_trades(live_rows)
    print(f"=== 15m livescore ({live_path}) ===")
    print(
        f"live rows: {live['n_rows']} | filled+settled {live['n_filled_settled']} "
        f"({live['n_wins']} win / {live['n_losses']} loss) | "
        f"unfilled {live['n_unfilled']} | pending {live['n_pending']}"
    )
    print(f"live filled PnL: ${live['pnl']:.2f}")
    if live_rows:
        for row in live_rows[-10:]:
            print(
                f"  {row.get('ticker')} {row.get('side')} "
                f"@{row.get('limit_price')} fill={row.get('fill_status')} "
                f"result={row.get('result')} pnl={row.get('pnl')} "
                f"order_id={row.get('order_id') or ''}"
            )
    else:
        print("no live 15m journal yet")
    print(f"pot ${pot.balance:.2f} realized ${pot.realized_pnl:.2f} stopped={pot.stopped}")
    return EXIT_OK


def live_is_armed(
    settings: FifteenSettings,
    *,
    confirm: str = "",
    isatty: bool | None = None,
    prompt: Any = None,
) -> bool:
    if settings.halted:
        return False
    if settings.live_enabled:
        return True
    if isatty is None:
        isatty = sys.stdin.isatty()
    if not isatty:
        return False
    if str(confirm or "").strip().upper() == "LIVE":
        return True
    where = " on DEMO" if settings.use_demo else " on PROD"
    reply = (prompt or input)(f"Type LIVE to place 15m maker limits{where}: ")
    return str(reply or "").strip().upper() == "LIVE"


def apply_host_flags(settings: FifteenSettings, args: argparse.Namespace) -> None:
    if getattr(args, "prod", False):
        settings.use_demo = False
    if getattr(args, "demo", False):
        settings.use_demo = True


def add_host_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prod", action="store_true")
    parser.add_argument("--demo", action="store_true")


def normalize_argv(argv: list[str] | None) -> list[str]:
    raw = list(argv) if argv is not None else sys.argv[1:]
    aliases = {
        "s": "scan",
        "1": "scan",
        "o": "once",
        "2": "once",
        "a": "auth",
        "3": "auth",
        "l": "live",
        "4": "live",
        "v": "eval",
        "6": "eval",
        "p": "paper",
        "7": "paper",
        "livescore": "livescore",
        "score": "score",
        "calibrate": "calibrate",
        "c": "calibrate",
    }
    if not raw:
        return ["scan"]
    if raw[0] in aliases:
        raw[0] = aliases[raw[0]]
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Kalshi 15m BTC/ETH edge-loop bot (KXBTC15M / KXETH15M)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Scan; paper shadows the live window decision")
    scan.add_argument("--asset", choices=["BTC", "ETH", "btc", "eth"], default=None)
    add_host_flags(scan)

    once = sub.add_parser("once", help="Scan + dry-run payloads")
    add_host_flags(once)

    auth = sub.add_parser("auth", help="Test API key + PEM")
    add_host_flags(auth)

    live = sub.add_parser("live", help="Place maker limits after LIVE confirm")
    live.add_argument("--confirm", default="", metavar="LIVE")
    add_host_flags(live)

    sub.add_parser("eval", help="Paper log + live journal + pot summary (bookkeeping dump)")
    sub.add_parser("paper", help="Same as eval")
    sub.add_parser("score", help="Termius 15m PAPER board (not live)")
    sub.add_parser("livescore", help="Termius 15m LIVE board (not paper)")
    cal = sub.add_parser(
        "calibrate",
        help="Model Yes calibration from scan strikes vs official settlement (no orders)",
    )
    cal.add_argument("--artifacts", default=None)
    cal.add_argument("--scan-log", default=None)
    cal.add_argument("--settlements", action="append", default=None)
    cal.add_argument("--out", default=None)
    cal.add_argument("--all-scans", action="store_true")
    cal.add_argument("--include-proxy", action="store_true")
    cal.add_argument("--fetch-prints", action="store_true")

    args = parser.parse_args(normalize_argv(argv))
    configure_logging()
    try:
        settings = load_fifteen_settings()
    except Exception as exc:  # noqa: BLE001
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    apply_host_flags(settings, args)

    if args.command == "auth":
        return run_auth(settings)
    if args.command == "scan":
        return run_scan(settings, asset=args.asset, place=False, force_live=False)
    if args.command == "once":
        return run_scan(settings, asset=None, place=True, force_live=False)
    if args.command == "live":
        if settings.halted:
            print(HALTED_MESSAGE)
            return EXIT_CONFIG
        if not live_is_armed(settings, confirm=getattr(args, "confirm", "")):
            print("Live aborted (not confirmed).")
            return EXIT_OK
        return run_scan(settings, asset=None, place=True, force_live=True, armed=True)
    if args.command in {"eval", "paper"}:
        return run_eval(settings)
    if args.command in {"score", "livescore"}:
        from src.scoreboard import run_board

        return run_board(args.command, fifteen_root=Path.cwd())
    if args.command == "calibrate":
        from src.calibrate import run_calibrate_cli

        extra: list[str] = ["--fifteen"]
        if args.artifacts:
            extra.extend(["--artifacts", args.artifacts])
        if args.scan_log:
            extra.extend(["--scan-log", args.scan_log])
        for path in args.settlements or []:
            extra.extend(["--settlements", path])
        if args.out:
            extra.extend(["--out", args.out])
        if args.all_scans:
            extra.append("--all-scans")
        if args.include_proxy:
            extra.append("--include-proxy")
        if args.fetch_prints:
            extra.append("--fetch-prints")
        return run_calibrate_cli(settings, fifteen=True, argv=extra)
    return EXIT_CONFIG


def cli() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
