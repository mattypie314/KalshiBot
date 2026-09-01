"""CLI: scan / once / live. Default is dry-run."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import EXIT_CONFIG, EXIT_OK, EXIT_RATE_LIMITED, HourlySettings, load_settings
from src.executor import execute_ideas
from src.filters import FilterConfig, FilterResult, Idea, evaluate_market, news_blackout_active
from src.kalshi_client import AuthConfigError, ForbiddenError, KalshiClient, RateLimitedError
from src.markets import MarketDiscovery
from src.report import format_report
from src.spot import SpotService

logger = logging.getLogger(__name__)


def _filter_cfg(settings: HourlySettings, state: dict[str, Any]) -> FilterConfig:
    return FilterConfig(
        min_net_edge=settings.min_net_edge,
        soft_net_edge=settings.soft_net_edge,
        max_spread=settings.max_spread,
        min_minutes_left=settings.min_minutes_left,
        min_visible_depth=settings.min_visible_depth_contracts,
        bankroll=settings.bankroll,
        kelly_mult=settings.kelly_mult,
        max_risk_pct=settings.max_risk_pct,
        max_risk_dollars=settings.max_risk_dollars,
        preferred_risk_dollars=settings.preferred_risk_dollars,
        last_loss_same_hour=bool(state.get("loss_this_hour")),
        last_contracts=state.get("last_contracts"),
        news_blackout=news_blackout_active(),
    )


def _hour_key(now: datetime) -> str:
    return now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    if data.get("hour_key") != _hour_key(datetime.now(timezone.utc)):
        return {"hour_key": _hour_key(datetime.now(timezone.utc))}
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def _mark_losses_from_fills(client: KalshiClient, state: dict[str, Any]) -> dict[str, Any]:
    if not client.can_trade:
        return state
    try:
        fills = client.get_fills(limit=50)
    except Exception as exc:  # noqa: BLE001
        logger.info("fills unavailable: %s", exc)
        return state
    hour = _hour_key(datetime.now(timezone.utc))
    lost = False
    for fill in fills:
        ts = str(fill.get("created_time") or fill.get("ts") or "")
        if hour[:13] not in ts:
            continue
        yes_price = float(fill.get("yes_price_dollars") or 0)
        # A losing fill this hour is recorded if the fill side is marked is_taker and
        # we previously stored that ticker as working. Keep this conservative.
        if fill.get("is_confirmed_loss") or fill.get("pnl", 0) not in (None, "", 0) and float(fill.get("pnl") or 0) < 0:
            lost = True
    if lost:
        state["loss_this_hour"] = True
        state["hour_key"] = hour
    return state


def run_scan(
    settings: HourlySettings,
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

    artifacts = Path(settings.artifacts_dir)
    artifacts.mkdir(parents=True, exist_ok=True)
    state = load_state(Path(settings.state_path))

    client = KalshiClient(
        settings.kalshi_base_url,
        timeout=settings.request_timeout_seconds,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
        trading_base_url=settings.trading_base_url,
    )
    spots_svc = SpotService(preferred=settings.spot_source)
    try:
        try:
            spots = spots_svc.snapshot(
                assets,
                fallbacks={
                    "BTC": settings.hourly_vol_fallback_btc,
                    "ETH": settings.hourly_vol_fallback_eth,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("spot fetch failed")
            print(f"spot fetch failed: {exc}", file=sys.stderr)
            return EXIT_CONFIG

        try:
            discovery = MarketDiscovery(client)
            markets = discovery.discover(
                assets,
                max_per_asset=settings.max_markets_per_asset,
                spots=spots.prices,
            )
            settlements = discovery.next_settlements(markets)
        except RateLimitedError as exc:
            print(f"rate limited: {exc}", file=sys.stderr)
            return EXIT_RATE_LIMITED
        except ForbiddenError as exc:
            print(f"SCAN_FAILED_403: {exc}", file=sys.stderr)
            print("Kalshi blocked this IP (common on GitHub Actions). Scan locally or on a VPS.")
            payload = {
                "error": "403",
                "message": str(exc),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            (artifacts / "last_run.json").write_text(json.dumps(payload, indent=2))
            return EXIT_OK

        if force_live or place:
            state = _mark_losses_from_fills(client, state)

        cfg = _filter_cfg(settings, state)
        now = datetime.now(timezone.utc)
        ideas: list[Idea] = []
        nearby: list[FilterResult] = []
        avoided: list[FilterResult] = []
        for market in markets:
            spot = spots.prices.get(market.asset)
            vol = spots.hourly_vol.get(market.asset)
            if not spot or not vol:
                continue
            result = evaluate_market(market, spot=spot, hourly_vol=vol, now=now, cfg=cfg)
            if result.idea:
                ideas.append(result.idea)
            elif result.nearby:
                nearby.append(result)
            else:
                avoided.append(result)

        ideas.sort(key=lambda i: i.net_edge, reverse=True)
        extra = ideas[settings.max_ideas_per_run :]
        ideas = ideas[: settings.max_ideas_per_run]
        for idea in extra:
            nearby.append(
                FilterResult(
                    market=idea.market,
                    nearby=True,
                    watch_note=f"{idea.side} net {idea.net_edge:.1%} held back (max {settings.max_ideas_per_run} idea/run)",
                )
            )
        report = format_report(
            now=now,
            spots=spots,
            markets=markets,
            ideas=ideas,
            nearby=nearby,
            avoided=avoided,
            settlements=settlements,
        )
        print(report)

        scan_blob = {
            "ts": now.isoformat(),
            "spots": spots.prices,
            "vol": spots.hourly_vol,
            "spot_source": spots.source,
            "settlement_note": spots.note,
            "markets": [m.ticker for m in markets],
            "actionable": [i.market.ticker for i in ideas],
            "report": report,
        }
        (artifacts / "last_run.json").write_text(json.dumps(scan_blob, indent=2, default=str))

        if not ideas:
            return EXIT_OK

        if place or force_live:
            live = bool(force_live and (armed or settings.live_enabled))
            if force_live and not live:
                print(
                    "LIVE refused: type YES at the prompt, pass --confirm YES, "
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
                )
            except RateLimitedError as exc:
                print(f"rate limited: {exc}", file=sys.stderr)
                return EXIT_RATE_LIMITED
            except ForbiddenError as exc:
                print(f"order 403, falling back to read-only: {exc}", file=sys.stderr)
                client.read_only = True
            except AuthConfigError as exc:
                print(f"auth/config: {exc}", file=sys.stderr)
                return EXIT_CONFIG
            if live and any("401" in str(err) for err in placed.get("errors") or []):
                print("LIVE order failed auth (401). Run: python -m src.main auth", file=sys.stderr)
                return EXIT_CONFIG
            state["hour_key"] = _hour_key(now)
            state["last_contracts"] = ideas[0].contracts
            save_state(Path(settings.state_path), state)
        return EXIT_OK
    finally:
        client.close()
        spots_svc.close()


def run_auth(settings: HourlySettings) -> int:
    """Check that signed Kalshi calls work. Prints no secret material."""
    settings.ensure_private_key_file()
    client = KalshiClient(
        settings.kalshi_base_url,
        timeout=settings.request_timeout_seconds,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
        trading_base_url=settings.trading_base_url,
    )
    try:
        info = client.auth_status()
        print(f"trading host: {info['trading_host']}")
        print(f"USE_DEMO={settings.use_demo} LIVE_TRADING={settings.live_trading} CONFIRM_LIVE={settings.confirm_live}")
        print(f"key id set: {info['key_id_set']} (len {info['key_id_len']})")
        print(f"pem: {info['pem_path'] or '(none)'} exists={info['pem_exists']} private={info['pem_looks_private']}")
        if not info["key_id_set"] or not info["pem_exists"]:
            print("Missing key id or PEM. Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH.")
            return EXIT_CONFIG
        if not info["pem_looks_private"]:
            print("PEM does not look like a PRIVATE key. Use kalshi_private_key.pem, not the public file.")
            return EXIT_CONFIG
        try:
            bal = client.get_balance()
        except Exception as exc:  # noqa: BLE001
            print(f"AUTH FAILED: {exc}")
            print("Use the same key id + private PEM + host as your Kalshi account.")
            print("Live Kalshi key + USE_DEMO=false + KALSHI_BASE_URL=https://external-api.kalshi.com/trade-api/v2")
            return EXIT_CONFIG
        cash = bal.get("balance_dollars") or bal.get("balance")
        print(f"AUTH OK. Kalshi balance field: {cash}")
        return EXIT_OK
    finally:
        client.close()


MODE_ALIASES = {
    "1": "scan",
    "s": "scan",
    "scan": "scan",
    "2": "once",
    "o": "once",
    "once": "once",
    "3": "auth",
    "a": "auth",
    "auth": "auth",
    "4": "live",
    "l": "live",
    "live": "live",
}

MODE_MENU = """\
KalshiBot — pick a mode
  1  scan   report only (default)
  2  once   dry-run limit payloads
  3  auth   test key + PEM
  4  live   real limits (type YES — no .env edit)

Mode [scan]: """


def normalize_argv(
    argv: list[str] | None,
    *,
    isatty: bool | None = None,
    prompt: Callable[[str], str] | None = None,
) -> list[str]:
    """Map shortcuts / a menu pick onto scan|once|auth|live."""
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


def live_is_armed(
    settings: HourlySettings,
    *,
    confirm: str = "",
    isatty: bool | None = None,
    prompt: Callable[[str], str] | None = None,
) -> bool:
    """Arm live for this run only. Env flags, --confirm YES, or a TTY YES prompt."""
    if settings.live_enabled:
        return True
    if str(confirm or "").strip().upper() == "YES":
        return True
    if isatty is None:
        isatty = sys.stdin.isatty()
    if not isatty:
        return False
    where = " on DEMO" if settings.use_demo else " on PROD"
    reply = (prompt or input)(f"Type YES to place LIVE limits{where} (anything else aborts): ")
    return reply.strip().upper() == "YES"


def cli() -> None:
    raise SystemExit(main())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Kalshi hourly BTC/ETH threshold scanner. "
        "Run with no args for a mode menu. Shortcuts: s/o/a/l or 1-4."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Scan books and print the report (no orders)")
    scan.add_argument("--asset", choices=["BTC", "ETH", "btc", "eth"], default=None)

    sub.add_parser("once", help="Scan and print dry-run limit order payloads")
    sub.add_parser("auth", help="Test Kalshi API key + PEM (no orders)")
    live = sub.add_parser(
        "live",
        help="Place limits after typing YES (or --confirm YES). .env can stay dry.",
    )
    live.add_argument(
        "--confirm",
        default="",
        metavar="YES",
        help="Pass YES to arm live without a prompt. Leaves .env unchanged.",
    )

    args = parser.parse_args(normalize_argv(argv))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.command == "auth":
        return run_auth(settings)
    if args.command == "scan":
        return run_scan(settings, asset=args.asset, place=False, force_live=False)
    if args.command == "once":
        return run_scan(settings, asset=None, place=True, force_live=False)
    if args.command == "live":
        if not live_is_armed(settings, confirm=args.confirm):
            print(
                "LIVE refused: type YES at the prompt, pass --confirm YES, "
                "or set LIVE_TRADING=true and CONFIRM_LIVE=YES.",
                file=sys.stderr,
            )
            return EXIT_CONFIG
        return run_scan(settings, asset=None, place=True, force_live=True, armed=True)
    return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
