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
                host = getattr(client, "trading_base_url", "") or ""
                print(f"LIVE order failed auth (401) on {host or 'this host'}.", file=sys.stderr)
                if "demo-api" in host:
                    print(
                        "That is a live Kalshi key on the demo API. Re-run: ./kb live --prod",
                        file=sys.stderr,
                    )
                    print("To make prod stick: ./kb env --prod", file=sys.stderr)
                else:
                    print("Key id and private PEM must match. Run: ./kb auth --prod", file=sys.stderr)
                return EXIT_CONFIG
            state["hour_key"] = _hour_key(now)
            state["last_contracts"] = ideas[0].contracts
            save_state(Path(settings.state_path), state)
        return EXIT_OK
    finally:
        client.close()
        spots_svc.close()


def _host_client(settings: HourlySettings, *, use_demo: bool) -> KalshiClient:
    url = settings.kalshi_demo_url if use_demo else settings.kalshi_base_url
    return KalshiClient(
        url,
        timeout=settings.request_timeout_seconds,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
        trading_base_url=url,
    )


def _auth_miss(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "401" in text or "authentication_error" in text


def probe_balance(settings: HourlySettings, *, use_demo: bool) -> tuple[bool, Any]:
    """GET /portfolio/balance on demo or prod. Does not print secrets."""
    client = _host_client(settings, use_demo=use_demo)
    try:
        return True, client.get_balance()
    except Exception as exc:  # noqa: BLE001
        return False, exc
    finally:
        client.close()


def apply_host_flags(settings: HourlySettings, args: argparse.Namespace) -> None:
    if getattr(args, "prod", False):
        settings.use_demo = False
    elif getattr(args, "demo", False):
        settings.use_demo = True


def _add_host_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--prod", action="store_true", help="Use live Kalshi this run (not demo)")
    group.add_argument("--demo", action="store_true", help="Use demo Kalshi this run")


def run_auth(settings: HourlySettings) -> int:
    """Check that signed Kalshi calls work. Prints no secret material."""
    settings.ensure_private_key_file()
    client = _host_client(settings, use_demo=settings.use_demo)
    try:
        info = client.auth_status()
    finally:
        client.close()

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

    ok, payload = probe_balance(settings, use_demo=settings.use_demo)
    if ok:
        cash = payload.get("balance_dollars") or payload.get("balance") if isinstance(payload, dict) else payload
        print(f"AUTH OK. Kalshi balance field: {cash}")
        return EXIT_OK

    print(f"AUTH FAILED on {'DEMO' if settings.use_demo else 'PROD'}: {payload}")
    other_demo = not settings.use_demo
    other_ok, other_payload = probe_balance(settings, use_demo=other_demo)
    if other_ok:
        label = "DEMO" if other_demo else "PROD"
        flag = "--demo" if other_demo else "--prod"
        cash = (
            other_payload.get("balance_dollars") or other_payload.get("balance")
            if isinstance(other_payload, dict)
            else other_payload
        )
        print(f"AUTH OK on {label}. This key belongs there, not the host above.")
        print(f"Balance field: {cash}")
        print(f"Re-run with: ./kb auth {flag}")
        print(f"Live on that host: ./kb live {flag}")
        return EXIT_CONFIG
    print("The other host also failed. Key id and private PEM must be a matching pair from the same Kalshi account.")
    return EXIT_CONFIG


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
  4  live   real limits (type LIVE — no .env edit)

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


LIVE_CONFIRM = "LIVE"


def _typed_live(value: str) -> bool:
    return str(value or "").strip().upper() == LIVE_CONFIRM


def live_is_armed(
    settings: HourlySettings,
    *,
    confirm: str = "",
    isatty: bool | None = None,
    prompt: Callable[[str], str] | None = None,
) -> bool:
    """Arm live for this run only. Env flags, --confirm LIVE, or a TTY LIVE prompt."""
    if settings.live_enabled:
        return True
    if _typed_live(confirm):
        return True
    if isatty is None:
        isatty = sys.stdin.isatty()
    if not isatty:
        return False
    where = " on DEMO" if settings.use_demo else " on PROD"
    reply = (prompt or input)(f"Type LIVE to place live limits{where} (anything else aborts): ")
    return _typed_live(reply)


def upsert_dotenv(path: Path, key: str, value: str) -> None:
    """Set KEY=value in .env without touching other lines or secrets."""
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text().splitlines()
    found = False
    out: list[str] = []
    prefix = f"{key}="
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(prefix) or stripped.startswith(f"#{prefix}"):
            if not found:
                out.append(f"{key}={value}")
                found = True
            continue
        out.append(line)
    if not found:
        out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip() + "\n")


def run_env(settings: HourlySettings, *, prod: bool, demo: bool) -> int:
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
    print(f"This checkout uses {host}: {settings.trading_base_url}")
    print("Edit other knobs in .env with nano:")
    print(f"  nano {path.resolve()}")
    return EXIT_OK


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
    _add_host_flags(scan)

    once = sub.add_parser("once", help="Scan and print dry-run limit order payloads")
    _add_host_flags(once)

    auth = sub.add_parser("auth", help="Test Kalshi API key + PEM (no orders)")
    _add_host_flags(auth)

    live = sub.add_parser(
        "live",
        help="Place limits after typing LIVE (or --confirm LIVE). .env can stay dry.",
    )
    live.add_argument(
        "--confirm",
        default="",
        metavar="LIVE",
        help="Pass LIVE to arm live without a prompt. Leaves .env unchanged.",
    )
    _add_host_flags(live)

    envp = sub.add_parser("env", help="Show or set demo vs prod in .env")
    _add_host_flags(envp)

    args = parser.parse_args(normalize_argv(argv))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        settings = load_settings()
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
        ok, payload = probe_balance(settings, use_demo=settings.use_demo)
        if not ok:
            label = "DEMO" if settings.use_demo else "PROD"
            print(f"LIVE refused: auth failed on {label}: {payload}", file=sys.stderr)
            other = not settings.use_demo
            other_ok, _other = probe_balance(settings, use_demo=other)
            if other_ok:
                flag = "--demo" if other else "--prod"
                print(
                    f"This key works on {'DEMO' if other else 'PROD'}. Re-run: ./kb live {flag}",
                    file=sys.stderr,
                )
            else:
                print("Run: ./kb auth", file=sys.stderr)
            return EXIT_CONFIG
        if not live_is_armed(settings, confirm=args.confirm):
            print(
                "LIVE refused: type LIVE at the prompt, pass --confirm LIVE, "
                "or set LIVE_TRADING=true and CONFIRM_LIVE=YES.",
                file=sys.stderr,
            )
            return EXIT_CONFIG
        return run_scan(settings, asset=None, place=True, force_live=True, armed=True)
    if args.command == "env":
        return run_env(settings, prod=bool(args.prod), demo=bool(args.demo))
    return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
