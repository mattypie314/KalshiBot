"""Dedicated 15-minute BTC/ETH Kalshi bot. Hourly lives in src.main."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from src.cfindex import fifteen_index_id_for, fifteen_official_index_label
from src.clock import (
    configure_logging,
    fifteen_window_key,
    format_et,
    to_et,
)
from src.config import EXIT_CONFIG, EXIT_OK, EXIT_RATE_LIMITED
from src.evaluate import summarize_scans, summarize_trades
from src.executor import CRYPTO_SHARD, execute_ideas, is_fifteen_rest
from src.fifteen_config import FifteenSettings, load_fifteen_settings
from src.fifteen_filters import (
    FifteenFilterConfig,
    classify_phase,
    evaluate_fifteen_market,
    should_stop_ticket,
    should_take_profit,
)
from src.fifteen_pot import apply_pnl, load_pot, pot_should_halt, remaining_room, save_pot
from src.filters import FilterResult, Idea
from src.journal import (
    append_trade,
    fill_status_from_order,
    load_trades,
    new_trade_row,
    parse_count,
    resolve_pending,
    write_trades,
)
from src.kalshi_client import AuthConfigError, ForbiddenError, KalshiClient, RateLimitedError
from src.main import (
    apply_host_flags,
    live_is_armed,
    market_result_is_loss,
    probe_balance,
    upsert_dotenv,
)
from src.markets import FIFTEEN_SERIES, MarketDiscovery
from src.paper import (
    describe_paper_append,
    format_paper_section,
    record_printed_ideas,
    settle_paper_file,
    summarize_paper,
)
from src.report import format_report
from src.spot import SpotService

logger = logging.getLogger(__name__)

HALTED_MESSAGE = (
    "HALTED: 15m live trading is off until further notice. "
    "--confirm LIVE cannot override this. "
    "On the Pi: sudo systemctl disable --now kalshi-15m.timer. "
    "Resume later with HALTED=false."
)

TICKET_KEYS = ("last_ticker", "last_side", "last_contracts", "last_fill_price", "last_risk")
KEEP_KEYS = TICKET_KEYS + ("recheck_spot", "recheck_used")


def parse_total_value(payload: Any) -> float:
    """Kalshi portfolio total_value (dollars). Falls back to balance fields."""
    if not isinstance(payload, dict):
        return 0.0
    for key in ("total_value", "portfolio_value", "balance_dollars", "balance"):
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 1000 and key == "balance":
            value = value / 100.0
        if value > 0:
            return value
    return 0.0


def parse_shard2_cash(payload: Any) -> float:
    """Cash available on crypto shard (exchange_index 2), else total_value."""
    if not isinstance(payload, dict):
        return 0.0
    for key in ("balances", "exchange_balances", "portfolios"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            idx = row.get("exchange_index")
            if idx in (2, "2"):
                cash = parse_total_value(row) or parse_total_value(
                    {"balance": row.get("balance") or row.get("cash") or row.get("available")}
                )
                if cash > 0:
                    return cash
    return parse_total_value(payload)


def _window_key(now: datetime | None = None) -> str:
    return fifteen_window_key(now)


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    current = _window_key(to_et())
    if data.get("window_key") == current:
        return data
    kept = {key: data[key] for key in KEEP_KEYS if key in data}
    kept["window_key"] = current
    return kept


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def fifteen_spot_service(settings: FifteenSettings, client: KalshiClient) -> SpotService:
    return SpotService(
        preferred=settings.spot_source,
        kalshi=client,
        index_id_fn=fifteen_index_id_for,
        vol_lookback_minutes=settings.fifteen_vol_lookback_minutes,
        settlement_labels={"BTC": "BRTI", "ETH": "ETHUSD_RTI"},
    )


def _filter_cfg(
    settings: FifteenSettings,
    *,
    pot: dict[str, Any],
    state: dict[str, Any],
    bankroll: float,
    shard2_cash: float,
    daily_losses: int,
    phase_name: str,
    half_sigma: bool,
) -> FifteenFilterConfig:
    return FifteenFilterConfig(
        mid_tolerance=settings.mid_tolerance,
        min_minutes_left=settings.min_minutes_left,
        edge_loop_min_into=settings.edge_loop_min_into,
        edge_loop_max_into=settings.edge_loop_max_into,
        last_minute_maker=settings.last_minute_maker,
        last_minute_minutes=settings.last_minute_minutes,
        last_minute_min_price=settings.last_minute_min_price,
        last_minute_max_price=settings.last_minute_max_price,
        last_minute_min_risk=settings.last_minute_min_risk,
        last_minute_max_risk=settings.last_minute_max_risk,
        stack_last_minute_with_edge=settings.stack_last_minute_with_edge,
        require_settlement_index=settings.require_settlement_index,
        require_maker=settings.require_maker,
        news_pause=settings.news_pause,
        vol_pause_mult=settings.vol_pause_mult,
        min_risk_dollars=settings.min_risk_dollars,
        max_risk_dollars=settings.max_risk_dollars,
        preferred_risk_dollars=settings.preferred_risk_dollars,
        max_risk_pct=settings.max_risk_pct,
        kelly_mult=settings.kelly_mult,
        bankroll=bankroll,
        pot_room=remaining_room(pot),
        shard2_cash=shard2_cash if shard2_cash > 0 else bankroll,
        revenge=bool(state.get("loss_this_window")),
        daily_losses=daily_losses,
        max_daily_losses=settings.max_daily_losses,
        idea_this_window=bool(state.get("idea_this_window")),
        half_sigma_recheck=half_sigma,
        decided_late=phase_name == "last_minute",
    )


def _daily_fifteen_losses(trades: list[dict[str, Any]], now: datetime) -> int:
    from src.clock import same_et_day
    from src.journal import counts_as_filled

    return sum(
        1
        for row in trades
        if row.get("result") == "loss"
        and counts_as_filled(row)
        and same_et_day(
            row.get("resolved_ts_iso") or row.get("resolved_ts") or row.get("ts_iso") or row.get("ts"),
            now,
        )
    )


def _half_sigma_jump(state: dict[str, Any], spots: Any, vol: dict[str, float]) -> bool:
    prev = state.get("recheck_spot") or {}
    if not isinstance(prev, dict) or not prev:
        return False
    if state.get("recheck_used"):
        return False
    for asset, price in getattr(spots, "prices", {}).items():
        old = prev.get(asset)
        sigma = vol.get(asset) or 0.0
        if not old or not price or sigma <= 0:
            continue
        if abs(price - float(old)) >= 0.5 * sigma * float(old):
            return True
    return False


def format_fifteen_report(
    *,
    now: datetime,
    spots: Any,
    markets: list[Any],
    ideas: list[Idea],
    nearby: list[FilterResult],
    avoided: list[FilterResult],
    settlements: list[str],
    pot: dict[str, Any],
    phase: Any,
    halted: bool = True,
) -> str:
    body = format_report(
        now=now,
        spots=spots,
        markets=markets,
        ideas=ideas,
        nearby=nearby,
        avoided=avoided,
        settlements=settlements,
    )
    header = (
        f"# BTC/ETH 15m — {format_et(now)}\n"
        f"- Standing: Trade only BTC/ETH 15m on shard 2, settlement-index fair value, "
        f"maker limits, one idea per window, $0.10–$1.50 risk, own $5 pot "
        f"(separate from hourly) — quit at $0, ask at $10, flat is fine.\n"
        f"- Window: {fifteen_window_key(now)}  phase={getattr(phase, 'name', '?')}  "
        f"into={getattr(phase, 'minutes_into', 0):.1f}m  left={getattr(phase, 'minutes_left', 0):.1f}m\n"
        f"- Pot: ${float(pot.get('pot') or 0):.2f}  (start ${float(pot.get('start') or 5):.2f}; "
        f"ask ${float(pot.get('ask_at') or 10):.0f}; pot_halted={bool(pot.get('halted'))} "
        f"HALTED={halted})\n"
        f"- Settlement: BTC BRTI / ETH ETHUSD_RTI (never ERTI). 60s average before window end.\n"
        f"- Vol lookback: TEMPORARY HEURISTIC — "
        f"{getattr(spots, 'vol_source', {})} (shorter than hourly 4h).\n"
    )
    body = body.replace("# BTC/ETH Hourly", "# BTC/ETH 15m", 1)
    if pot.get("ask_notified"):
        header += f"- ASK: pot reached ${float(pot.get('pot') or 0):.2f}. Notify Crypto Clint — do not auto-stop.\n"
    return header + "\n" + body.split("\n", 1)[-1] if body.startswith("#") else header + body


def try_settle_fifteen_paper(settings: FifteenSettings, client: Any | None) -> list[dict[str, Any]]:
    path = Path(settings.paper_log_path)
    if not path.is_file() or client is None:
        return []
    try:
        return settle_paper_file(
            path,
            lambda asset, close: _fetch_fifteen_print(client, asset, close),
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("15m paper settle skipped: %s", exc)
        return []


def _fetch_fifteen_print(client: Any, asset: str, close: Any) -> float | None:
    """Official 60s average. ETH must be ETHUSD_RTI, never ERTI."""
    from src.cfindex import (
        average_settlement_window,
        history_query_timestamp,
        parse_cf_history_ticks,
    )
    from src.clock import parse_ts

    index_id = fifteen_index_id_for(asset)
    getter = getattr(client, "get_cf_history", None)
    if not index_id or getter is None:
        return None
    if hasattr(client, "can_trade") and not client.can_trade:
        return None
    when = parse_ts(close) if not isinstance(close, datetime) else close
    if when is None:
        return None
    for timespan in ("MINUTE", "HOUR"):
        stamp = history_query_timestamp(when, timespan=timespan)
        try:
            blob = getter(index_id, timestamp=stamp, timespan=timespan)
        except Exception as exc:  # noqa: BLE001
            logger.info("CF history %s %s failed: %s", index_id, timespan, exc)
            continue
        ticks = parse_cf_history_ticks(blob)
        average = average_settlement_window(ticks, when)
        if average:
            return average
    return None


def run_scan(
    settings: FifteenSettings,
    *,
    asset: str | None,
    place: bool,
    force_live: bool,
    armed: bool = False,
) -> int:
    assets = [asset.upper()] if asset else settings.asset_list
    for name in assets:
        if name not in {"BTC", "ETH"}:
            print(f"unsupported asset {name}; only BTC and ETH", file=sys.stderr)
            return EXIT_CONFIG
    for series in settings.series_list:
        if series not in FIFTEEN_SERIES:
            print(f"unsupported series {series}; only KXBTC15M,KXETH15M", file=sys.stderr)
            return EXIT_CONFIG

    artifacts = Path(settings.artifacts_dir)
    artifacts.mkdir(parents=True, exist_ok=True)
    pot = load_pot(Path(settings.pot_path), start=settings.fifteen_pot_start, ask=settings.fifteen_pot_ask)
    if pot_should_halt(pot):
        settings.halted = True
        pot["halted"] = True
        save_pot(Path(settings.pot_path), pot)
        print("15m pot is empty — HALTED. Sitting.", file=sys.stderr)

    state = load_state(Path(settings.state_path))
    client = KalshiClient(
        settings.kalshi_base_url,
        timeout=settings.request_timeout_seconds,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
        trading_base_url=settings.trading_base_url,
    )
    journal_path = Path(settings.trade_log_path)
    spots_svc = fifteen_spot_service(settings, client)
    try:
        fills: list[dict[str, Any]] = []
        fills_available = False
        if client.can_trade:
            try:
                fills = list(client.get_fills(limit=50) or [])
                fills_available = True
            except Exception as exc:  # noqa: BLE001
                logger.info("fills unavailable: %s", exc)
        trades = resolve_pending(
            load_trades(journal_path),
            client.get_market,
            market_result_is_loss,
            fills=fills,
            fills_available=fills_available,
        )
        newly_settled = [row for row in trades if row.get("result") in {"win", "loss"} and row.get("pnl") is not None]
        # Apply newly resolved PnL once (rows already in the file stay put).
        prior = {str(row.get("ticker") or "") + str(row.get("ts_iso") or "") for row in load_trades(journal_path) if row.get("result") in {"win", "loss"}}
        for row in newly_settled:
            key = str(row.get("ticker") or "") + str(row.get("ts_iso") or "")
            if key in prior:
                continue
            apply_pnl(pot, float(row.get("pnl") or 0))
            if row.get("result") == "loss":
                state["loss_this_window"] = True
        write_trades(journal_path, trades)
        if pot_should_halt(pot):
            settings.halted = True
            pot["halted"] = True
        save_pot(Path(settings.pot_path), pot)

        bankroll = settings.fifteen_pot_start
        shard2 = remaining_room(pot)
        if client.can_trade:
            try:
                balance = client.get_balance()
                total = parse_total_value(balance)
                if total > 0:
                    bankroll = total
                shard2 = parse_shard2_cash(balance) or shard2
            except Exception as exc:  # noqa: BLE001
                logger.info("balance unavailable: %s", exc)

        try:
            spots = spots_svc.snapshot(
                assets,
                fallbacks={
                    "BTC": settings.fifteen_vol_fallback_btc,
                    "ETH": settings.fifteen_vol_fallback_eth,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("spot fetch failed")
            print(f"spot fetch failed: {exc}", file=sys.stderr)
            return EXIT_CONFIG

        now = to_et()
        half_sigma = _half_sigma_jump(state, spots, spots.hourly_vol)
        if half_sigma:
            state["recheck_used"] = True
        phase = classify_phase(
            now,
            FifteenFilterConfig(
                edge_loop_min_into=settings.edge_loop_min_into,
                edge_loop_max_into=settings.edge_loop_max_into,
                last_minute_maker=settings.last_minute_maker,
                last_minute_minutes=settings.last_minute_minutes,
                min_minutes_left=settings.min_minutes_left,
                idea_this_window=bool(state.get("idea_this_window")),
                stack_last_minute_with_edge=settings.stack_last_minute_with_edge,
                half_sigma_recheck=half_sigma,
            ),
        )

        try:
            discovery = MarketDiscovery(client)
            markets = discovery.discover_fifteen(
                assets,
                now=now,
                max_per_asset=settings.max_markets_per_asset,
                spots=spots.prices,
                require_exchange_index=settings.exchange_index,
            )
            settlements = [
                f"{m.asset} {format_et(m.close_time)} ({m.series_ticker} BRTI/ETHUSD_RTI)"
                for m in sorted({(x.asset, x.close_time, x.series_ticker): x for x in markets}.values(), key=lambda r: r.close_time)
            ]
        except RateLimitedError as exc:
            print(f"rate limited: {exc}", file=sys.stderr)
            return EXIT_RATE_LIMITED
        except ForbiddenError as exc:
            print(f"SCAN_FAILED_403: {exc}", file=sys.stderr)
            Path(settings.last_run_path).write_text(
                json.dumps({"error": "403", "message": str(exc), "ts": format_et(), "kind": "fifteen"}, indent=2)
            )
            return EXIT_OK

        daily_losses = _daily_fifteen_losses(trades, now)
        cfg = _filter_cfg(
            settings,
            pot=pot,
            state=state,
            bankroll=bankroll,
            shard2_cash=shard2,
            daily_losses=daily_losses,
            phase_name=phase.name,
            half_sigma=half_sigma,
        )
        vol_fallback = {
            "BTC": settings.fifteen_vol_fallback_btc,
            "ETH": settings.fifteen_vol_fallback_eth,
        }
        ideas: list[Idea] = []
        nearby: list[FilterResult] = []
        avoided: list[FilterResult] = []
        for market in markets:
            spot = spots.prices.get(market.asset)
            vol = spots.hourly_vol.get(market.asset)
            if not spot or not vol:
                avoided.append(FilterResult(market=market, avoid_reasons=["missing spot or vol"]))
                continue
            if settings.require_settlement_index and not spots.settlement_ok(market.asset):
                avoided.append(
                    FilterResult(
                        market=market,
                        avoid_reasons=["PROXY / missing BRTI or ETHUSD_RTI — sit this coin"],
                    )
                )
                continue
            result = evaluate_fifteen_market(
                market,
                spot=spot,
                hourly_vol=vol,
                now=now,
                cfg=cfg,
                vol_fallback=vol_fallback.get(market.asset),
                settlement_index=spots.settlement_ok(market.asset),
                phase=phase,
            )
            if result.idea:
                ideas.append(result.idea)
            elif result.nearby:
                nearby.append(result)
            else:
                avoided.append(result)

        ideas.sort(key=lambda i: i.net_edge, reverse=True)
        extra = ideas[settings.max_ideas_per_window :]
        ideas = ideas[: settings.max_ideas_per_window]
        for idea in extra:
            nearby.append(
                FilterResult(
                    market=idea.market,
                    nearby=True,
                    watch_note=f"{idea.side} held back (max {settings.max_ideas_per_window} idea/window)",
                )
            )

        report = format_fifteen_report(
            now=now,
            spots=spots,
            markets=markets,
            ideas=ideas,
            nearby=nearby,
            avoided=avoided,
            settlements=settlements,
            pot=pot,
            phase=phase,
            halted=settings.halted,
        )
        print(report)

        last_run = {
            "kind": "fifteen",
            "ts": format_et(now),
            "window_key": _window_key(now),
            "phase": phase.name,
            "minutes_into": phase.minutes_into,
            "minutes_left": phase.minutes_left,
            "spots": spots.prices,
            "vol": spots.hourly_vol,
            "spot_sources": spots.sources,
            "vol_source": spots.vol_source,
            "settlement_note": spots.note,
            "settlement_ok": {a: spots.settlement_ok(a) for a in spots.prices},
            "markets": [m.ticker for m in markets],
            "actionable": [i.market.ticker for i in ideas],
            "pot": pot.get("pot"),
            "halted": settings.halted or pot_should_halt(pot),
            "live_enabled": settings.live_enabled,
            "report": report,
        }
        Path(settings.last_run_path).parent.mkdir(parents=True, exist_ok=True)
        Path(settings.last_run_path).write_text(json.dumps(last_run, indent=2, default=str))
        Path(settings.scan_log_path).parent.mkdir(parents=True, exist_ok=True)
        with Path(settings.scan_log_path).open("a") as handle:
            handle.write(json.dumps({k: last_run[k] for k in last_run if k != "report"}, default=str) + "\n")

        try_settle_fifteen_paper(settings, client)
        if ideas and not force_live:
            written = record_printed_ideas(
                Path(settings.paper_log_path),
                ideas,
                sources={
                    asset: fifteen_official_index_label(asset, spots.sources.get(asset, spots.source))
                    for asset in spots.sources
                },
                default_source="PROXY",
                fill_model=settings.paper_fill_model,
                hourly_vol=spots.hourly_vol,
            )
            for row in written:
                row["settlement_index"] = fifteen_index_id_for(str(row.get("asset") or ""))
                print(describe_paper_append(row))

        if last_ticker := state.get("last_ticker"):
            _maybe_manage_exit(settings, client, state, last_ticker, ideas, live=force_live and armed)

        if not ideas:
            state["window_key"] = _window_key(now)
            state["recheck_spot"] = dict(spots.prices)
            save_state(Path(settings.state_path), state)
            return EXIT_OK

        if place or force_live:
            if settings.halted and force_live:
                print(HALTED_MESSAGE, file=sys.stderr)
                return EXIT_CONFIG
            live = bool(force_live and (armed or settings.live_enabled))
            if force_live and not live:
                print(
                    "LIVE refused: type LIVE at the prompt, pass --confirm LIVE, "
                    "or set LIVE_TRADING=true and CONFIRM_LIVE=YES.",
                    file=sys.stderr,
                )
                return EXIT_CONFIG
            if live and not client.can_trade:
                print("LIVE refused: missing API key / private key.", file=sys.stderr)
                return EXIT_CONFIG
            try:
                placed = execute_ideas(
                    ideas,
                    client=client,
                    artifacts_dir=artifacts,
                    live=live,
                    confirm_live=live,
                    rest_matcher=is_fifteen_rest,
                    exchange_index=CRYPTO_SHARD,
                )
            except RateLimitedError as exc:
                print(f"rate limited: {exc}", file=sys.stderr)
                return EXIT_RATE_LIMITED
            except ForbiddenError as exc:
                print(f"order 403, falling back to read-only: {exc}", file=sys.stderr)
                client.read_only = True
                placed = {"placed": [], "errors": [str(exc)], "orders": []}
            except AuthConfigError as exc:
                print(f"auth/config: {exc}", file=sys.stderr)
                return EXIT_CONFIG
            # execute_ideas writes last_run.json — restore the 15m scan snapshot plus orders.
            last_run["orders"] = placed.get("orders")
            last_run["placed"] = [p.get("order_id") for p in (placed.get("placed") or [])]
            last_run["mode"] = placed.get("mode")
            Path(settings.last_run_path).write_text(json.dumps(last_run, indent=2, default=str))
            if live and placed.get("placed"):
                idea = ideas[0]
                order = placed["placed"][0]
                state["window_key"] = _window_key(now)
                state["idea_this_window"] = True
                state["last_contracts"] = idea.contracts
                state["last_ticker"] = idea.market.ticker
                state["last_side"] = idea.side
                state["last_fill_price"] = idea.limit_price
                state["last_risk"] = idea.risk_dollars
                append_trade(
                    journal_path,
                    new_trade_row(
                        ticker=idea.market.ticker,
                        asset=idea.market.asset,
                        side=idea.side,
                        strike=idea.market.threshold,
                        spot=idea.spot or spots.prices.get(idea.market.asset) or 0.0,
                        minutes_left=idea.minutes_left,
                        fair=idea.fair,
                        kalshi_price=idea.entry_price,
                        limit_price=idea.limit_price,
                        contracts=idea.contracts,
                        risk_dollars=idea.risk_dollars,
                        hourly_vol=spots.hourly_vol.get(idea.market.asset) or 0.0,
                        source=fifteen_official_index_label(
                            idea.market.asset, spots.sources.get(idea.market.asset, spots.source)
                        ),
                        order_id=str(order.get("order_id") or ""),
                        client_order_id=str((placed.get("orders") or [{}])[0].get("client_order_id") or ""),
                        fill_status=fill_status_from_order(order),
                        filled_contracts=parse_count(order.get("fill_count")),
                    ),
                )
                save_state(Path(settings.state_path), state)
        else:
            state["window_key"] = _window_key(now)
            state["recheck_spot"] = dict(spots.prices)
            if ideas:
                state["idea_this_window"] = True
            save_state(Path(settings.state_path), state)
        return EXIT_OK
    finally:
        client.close()
        spots_svc.close()


def _maybe_manage_exit(
    settings: FifteenSettings,
    client: KalshiClient,
    state: dict[str, Any],
    ticker: str,
    ideas: list[Idea],
    *,
    live: bool,
) -> None:
    """Ticket stop / TP on a later look. Maker only; never cross."""
    getter = getattr(client, "get_market", None)
    if getter is None:
        return
    try:
        raw = getter(ticker)
    except Exception as exc:  # noqa: BLE001
        logger.info("exit quote failed for %s: %s", ticker, exc)
        return
    if not isinstance(raw, dict):
        return
    from src.markets import _quote

    yes_bid, yes_ask, no_bid, no_ask = _quote(raw)
    side = str(state.get("last_side") or "").lower()
    fill = float(state.get("last_fill_price") or 0)
    risk = float(state.get("last_risk") or 0)
    bid = yes_bid if side == "yes" else no_bid
    if fill <= 0:
        return
    tp = should_take_profit(fill_price=fill, bid=bid, take_profit_cents=settings.take_profit_cents)
    stop = should_stop_ticket(
        fill_price=fill,
        mark=bid,
        risk_dollars=risk,
        stop_frac_of_risk=settings.stop_frac_of_risk,
        stop_frac_from_fill=settings.stop_frac_from_fill,
        stop_dollar_cap=settings.stop_dollar_cap,
    )
    if tp:
        print(f"15m TP signal {ticker}: bid {bid:.2f} vs fill {fill:.2f} (fill+2¢ or 99¢).")
    if stop:
        print(f"15m STOP signal {ticker}: bid {bid:.2f} vs fill {fill:.2f} (25% risk / 10% fill, cap $0.40).")
    if not live:
        return
    # Exits are operator-notified on this oneshot; do not lift. A later live run can flatten.


def run_eval(settings: FifteenSettings) -> int:
    artifacts = Path(settings.artifacts_dir)
    client = None
    try:
        client = KalshiClient(
            settings.kalshi_base_url,
            timeout=settings.request_timeout_seconds,
            api_key_id=settings.kalshi_api_key_id,
            private_key_path=settings.kalshi_private_key_path,
            trading_base_url=settings.trading_base_url,
        )
        try_settle_fifteen_paper(settings, client)
    except Exception as exc:  # noqa: BLE001
        logger.info("eval settle skipped: %s", exc)
    finally:
        if client is not None:
            client.close()
    from src.evaluate import load_jsonl
    from src.paper import load_paper

    trades = summarize_trades(load_trades(Path(settings.trade_log_path)))
    scans = summarize_scans(load_jsonl(Path(settings.scan_log_path)))
    paper = summarize_paper(load_paper(Path(settings.paper_log_path)))
    pot = load_pot(Path(settings.pot_path), start=settings.fifteen_pot_start, ask=settings.fifteen_pot_ask)
    lines = [
        "# BTC/ETH 15m evaluation",
        "",
        "Not financial advice. Separate from hourly paper / live journals.",
        f"- Pot: ${float(pot.get('pot') or 0):.2f}  halted={bool(pot.get('halted'))}",
        f"- Settlement ids: BTC=BRTI  ETH=ETHUSD_RTI (never ERTI)",
        "",
        *format_paper_section(paper),
        "",
        f"## 15m live journal (`{settings.trade_log_path}`)",
        f"- Rows: {trades['n_rows']}",
        f"- Filled and settled: {trades['n_filled_settled']} "
        f"({trades['n_wins']} win / {trades['n_losses']} loss)",
        f"- Filled PnL: ${trades['pnl']:.2f}",
        "",
        f"## 15m scan log (`{settings.scan_log_path}`)",
        f"- Scans: {scans['n_scans']} ({scans['n_sits']} sit / {scans['n_scans_with_idea']} with an idea)",
        "",
        "This is not live profitability. Do not retune from paper tape.",
        "",
    ]
    # format_paper_section hardcodes hourly paper_log path — rewrite the heading.
    text = "\n".join(lines).replace("artifacts/paper_log.jsonl", settings.paper_log_path)
    text = text.replace("BRTI / ERTI", "BRTI / ETHUSD_RTI")
    print(text)
    return EXIT_OK


def run_env(settings: FifteenSettings, *, prod: bool, demo: bool) -> int:
    path = Path(".env")
    if prod:
        upsert_dotenv(path, "USE_DEMO", "false")
        settings.use_demo = False
        print(f"Wrote USE_DEMO=false to {path.resolve()}")
    elif demo:
        upsert_dotenv(path, "USE_DEMO", "true")
        settings.use_demo = True
        print(f"Wrote USE_DEMO=true to {path.resolve()}")
    host = "DEMO" if settings.use_demo else "PROD"
    print(f"This 15m checkout uses {host}: {settings.trading_base_url}")
    print(f"HALTED={settings.halted} LIVE_TRADING={settings.live_trading} CONFIRM_LIVE={settings.confirm_live}")
    print(f"Pot file: {Path(settings.pot_path).resolve()}")
    print("Edit other knobs in this checkout's .env (not /home/KalshiBot/.env):")
    print(f"  nano {path.resolve()}")
    return EXIT_OK


def run_auth(settings: FifteenSettings) -> int:
    from src.main import run_auth as hourly_auth

    # Reuse hourly auth probe; print 15m settlement reminder.
    code = hourly_auth(settings)  # type: ignore[arg-type]
    print("15m settlement ids: BTC=BRTI  ETH=ETHUSD_RTI (never ERTI)")
    return code


MODE_ALIASES = {
    "1": "scan",
    "s": "scan",
    "scan": "scan",
    "2": "once",
    "o": "once",
    "once": "once",
    "3": "live",
    "l": "live",
    "live": "live",
    "4": "env",
    "e": "env",
    "env": "env",
    "5": "eval",
    "v": "eval",
    "eval": "eval",
    "6": "paper",
    "p": "paper",
    "paper": "paper",
    "7": "auth",
    "a": "auth",
    "auth": "auth",
}

MODE_MENU = """\
KalshiBot 15m — pick a mode
  1  scan   report only (default)
  2  once   dry-run limit payloads
  3  live   real limits (type LIVE — no .env edit)
  4  env    show / set DEMO vs PROD in this checkout's .env
  5  eval   15m paper + trade log (no orders)
  6  paper  paper PnL only
  7  auth   test key + PEM

Mode [scan]: """


def normalize_argv(
    argv: list[str] | None,
    *,
    isatty: bool | None = None,
    prompt: Callable[[str], str] | None = None,
) -> list[str]:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in {"-h", "--help"}:
        return raw
    if not raw:
        if isatty is None:
            isatty = sys.stdin.isatty()
        if not isatty:
            return ["scan"]
        reply = (prompt or input)(MODE_MENU).strip() or "scan"
        mapped = MODE_ALIASES.get(reply.lower())
        if mapped is None:
            raise SystemExit(f"unknown mode: {reply}")
        return [mapped]
    mapped = MODE_ALIASES.get(raw[0].lower())
    if mapped is not None:
        return [mapped, *raw[1:]]
    return raw


def _add_host_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--prod", action="store_true", help="Use live Kalshi this run (not demo)")
    group.add_argument("--demo", action="store_true", help="Use demo Kalshi this run")


def cli() -> None:
    raise SystemExit(main())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Kalshi 15-minute BTC/ETH up/down bot (KXBTC15M / KXETH15M). "
        "Separate from hourly. Shortcuts: s/o/l/e/v/p/a."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Scan 15m books and print the report (no orders)")
    scan.add_argument("--asset", choices=["BTC", "ETH", "btc", "eth"], default=None)
    _add_host_flags(scan)

    once = sub.add_parser("once", help="Scan and print dry-run limit order payloads")
    _add_host_flags(once)

    live = sub.add_parser("live", help="Place 15m limits after typing LIVE (or --confirm LIVE)")
    live.add_argument("--confirm", default="", metavar="LIVE", help="Pass LIVE to arm without a prompt")
    _add_host_flags(live)

    envp = sub.add_parser("env", help="Show or set demo vs prod in this checkout's .env")
    _add_host_flags(envp)

    sub.add_parser("eval", help="Summarize 15m paper PnL and live journal (no orders)")
    sub.add_parser("paper", help="Same as eval; 15m paper tape is separate from hourly")
    auth = sub.add_parser("auth", help="Test Kalshi API key + PEM (no orders)")
    _add_host_flags(auth)

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
            print(HALTED_MESSAGE, file=sys.stderr)
            return EXIT_CONFIG
        ok, payload = probe_balance(settings, use_demo=settings.use_demo)  # type: ignore[arg-type]
        if not ok:
            print(f"LIVE refused: auth failed: {payload}", file=sys.stderr)
            return EXIT_CONFIG
        if not live_is_armed(settings, confirm=args.confirm):  # type: ignore[arg-type]
            print(
                "LIVE refused: type LIVE at the prompt, pass --confirm LIVE, "
                "or set LIVE_TRADING=true and CONFIRM_LIVE=YES.",
                file=sys.stderr,
            )
            return EXIT_CONFIG
        return run_scan(settings, asset=None, place=True, force_live=True, armed=True)
    if args.command == "env":
        return run_env(settings, prod=bool(args.prod), demo=bool(args.demo))
    if args.command in {"eval", "paper"}:
        return run_eval(settings)
    return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
