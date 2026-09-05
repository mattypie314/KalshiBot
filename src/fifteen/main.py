"""CLI for the 15m BTC/ETH edge-loop bot."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from src.cfindex import FIFTEEN_INDEX_BY_ASSET, fifteen_index_id_for
from src.clock import configure_logging, format_et, to_et
from src.executor import CRYPTO_SHARD, execute_ideas, is_fifteen_rest
from src.fees import taker_fee_dollars
from src.filters import Idea
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
)
from src.fifteen.pot import credit_pot, load_pot, save_pot, set_open_risk
from src.kalshi_client import AuthConfigError, ForbiddenError, KalshiClient, RateLimitedError
from src.markets import HourlyMarket, MarketDiscovery
from src.model import fair_prob, hours_left, model_z
from src.paper import FILL_ASSUMED_MAKER, record_printed_ideas, try_settle_paper
from src.sizer import size_idea
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
    limit = float(decision.join_price)
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
            f"maker join {limit:.2f} on {side}",
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
) -> tuple[list[Idea], list[str], Any]:
    now = to_et(now)
    notes: list[str] = []
    assets = [asset.upper()] if asset else list(settings.asset_list)

    if settings.news_pause:
        return [], ["NEWS_PAUSE — operator sit"], None
    news = news_blackout(now)
    if news:
        return [], [f"news blackout ({news})"], None
    if fifteen_stopped(state, now):
        return [], ["15m session stopped (3 losses)"], None
    if in_fifteen_revenge(state, now):
        return [], ["revenge window after a loser"], None
    if fifteen_working(state, now):
        return [], ["already working a 15m ticket this window"], None
    if not in_fifteen_entry_window(now):
        notes.append(f"outside entry window (minute {now.minute % 15}; want 2-4)")

    spots_svc = SpotService(
        preferred=settings.spot_source,
        kalshi=client,
        index_id_fn=fifteen_index_id_for,
        vol_lookback_minutes=60,
        settlement_labels=dict(FIFTEEN_INDEX_BY_ASSET),
    )
    try:
        spots = spots_svc.snapshot(
            assets,
            fallbacks={
                "BTC": settings.hourly_vol_fallback_btc,
                "ETH": settings.hourly_vol_fallback_eth,
            },
        )
    finally:
        spots_svc.close()

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
        decision = pass_fail(
            model_yes=fair_prob(spot, market.threshold, vol, hrs),
            yes_bid=market.yes_bid,
            yes_ask=market.yes_ask,
            secs_left=secs,
            sigma=model_z(spot, market.threshold, vol, hrs),
        )
        if not decision.passed:
            notes.append(f"{market.ticker}: {decision.line}")
            continue
        if market.spread > settings.max_spread + 1e-12 and abs(decision.edge) <= market.spread:
            notes.append(f"{market.ticker}: spread wider than edge")
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
    return candidates[: settings.max_ideas_per_run], notes, spots


def append_scan_log(
    settings: FifteenSettings,
    *,
    mode: str,
    ideas: list[Idea],
    notes: list[str],
    spots: Any,
) -> None:
    path = Path(settings.scan_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": format_et(),
        "mode": mode,
        "window_id": fifteen_window_id(),
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


def run_scan(
    settings: FifteenSettings,
    *,
    asset: str | None,
    place: bool,
    force_live: bool,
    armed: bool = False,
) -> int:
    Path(settings.artifacts_dir).mkdir(parents=True, exist_ok=True)
    state_path = Path(settings.state_path)
    state = load_state(state_path)
    pot = load_pot(settings.pot_path)
    pot.start = settings.pot_start
    pot.double_at = settings.pot_double

    if force_live and pot.stopped:
        print(f"15m pot stopped at ${pot.balance:.2f}. Refusing live.")
        save_pot(pot, settings.pot_path)
        return EXIT_OK
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

    try:
        ideas, notes, spots = collect_ideas(
            settings,
            client=client,
            state=state,
            pot_room=pot.room,
            bankroll=bankroll,
            asset=asset,
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
        f"window {fifteen_window_id()} | halted={settings.halted}"
    )
    if pot.ask_to_continue:
        print(f"POT DOUBLE: ${pot.balance:.2f} >= ${pot.double_at:.2f} — ask Matt.")
    if spots is not None:
        for name, price in spots.prices.items():
            src = spots.sources.get(name, spots.source)
            tag = "settlement" if spots.settlement_ok(name) else "PROXY"
            print(f"  {name} {price:.2f} ({src}, {tag})")

    if not ideas:
        print("NO_ACTIONABLE_EDGE")
        for note in notes[:12]:
            print(f"  sit: {note}")
        save_state(state_path, state)
        save_pot(pot, settings.pot_path)
        append_scan_log(settings, mode=mode, ideas=[], notes=notes, spots=spots)
        return EXIT_OK

    for idea in ideas:
        print(
            f"PASS {idea.market.ticker} {idea.side} @ {idea.limit_price:.2f} "
            f"x {idea.contracts} (fair {idea.fair:.2f}, edge {idea.net_edge:+.2f}, "
            f"risk ${idea.risk_dollars:.2f})"
        )
        for line in idea.rationale:
            print(f"  · {line}")

    if not place and not force_live:
        written = record_printed_ideas(
            Path(settings.paper_log_path),
            ideas,
            sources=(spots.sources if spots else {}),
            fill_model=FILL_ASSUMED_MAKER,
            hourly_vol=(spots.hourly_vol if spots else None),
        )
        for row in written:
            print(f"PAPER: logged {row.get('ticker')} (not live PnL)")
        save_state(state_path, state)
        save_pot(pot, settings.pot_path)
        append_scan_log(settings, mode=mode, ideas=ideas, notes=notes, spots=spots)
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
        set_open_risk(pot, sum(i.risk_dollars for i in ideas))
        wid = fifteen_window_id()
        for idea in ideas:
            state.setdefault("tickets", []).append(
                {
                    "status": "open",
                    "loop": "fifteen",
                    "window_id": wid,
                    "ticker": idea.market.ticker,
                    "side": idea.side,
                    "contracts": idea.contracts,
                    "limit": idea.limit_price,
                }
            )
        print(f"LIVE: placed {len(result['placed'])} 15m maker limit(s).")
        _ = credit_pot
    elif place or force_live:
        print("DRY-RUN: order payloads written (not live).")

    save_state(state_path, state)
    save_pot(pot, settings.pot_path)
    append_scan_log(settings, mode=mode, ideas=ideas, notes=notes, spots=spots)
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
    path = Path(settings.paper_log_path)
    print(f"=== 15m eval (paper={path}) ===")
    if path.is_file():
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        print(f"paper tickets: {len(rows)}")
        for row in rows[-10:]:
            print(
                f"  {row.get('ticker')} {row.get('side')} "
                f"result={row.get('result')} pnl={row.get('pnl')}"
            )
    else:
        print("no paper log yet")
    pot = load_pot(settings.pot_path)
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

    scan = sub.add_parser("scan", help="Scan + paper on Pass")
    scan.add_argument("--asset", choices=["BTC", "ETH", "btc", "eth"], default=None)
    add_host_flags(scan)

    once = sub.add_parser("once", help="Scan + dry-run payloads")
    add_host_flags(once)

    auth = sub.add_parser("auth", help="Test API key + PEM")
    add_host_flags(auth)

    live = sub.add_parser("live", help="Place maker limits after LIVE confirm")
    live.add_argument("--confirm", default="", metavar="LIVE")
    add_host_flags(live)

    sub.add_parser("eval", help="Paper log + pot summary")
    sub.add_parser("paper", help="Same as eval")

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
    return EXIT_CONFIG


def cli() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
