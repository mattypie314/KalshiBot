from __future__ import annotations

import argparse
import asyncio
import json
import logging

from kalshibot.assets import SECTION_LABELS, SECTIONS


def main() -> None:
    parser = argparse.ArgumentParser(description="KalshiBot — small-account campaign + prediction desk")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the dashboard")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)

    scan = sub.add_parser("scan", help="Print a research scan")
    scan.add_argument("--section", choices=SECTIONS, default=None)
    scan.add_argument("--json", action="store_true")
    scan.add_argument("--limit", type=int, default=12)

    campaign = sub.add_parser("campaign", help="Run Matt's 15m / hourly / maker loops")
    camp_sub = campaign.add_subparsers(dest="campaign_cmd", required=True)
    camp_sub.add_parser("status", help="Show campaign book, tickets, and recent log")
    fire = camp_sub.add_parser("fire", help="Run one loop once (dry-run unless KALSHI_LIVE=1)")
    fire.add_argument("loop", choices=["fifteen", "hourly", "maker"])
    camp_sub.add_parser("run", help="Scheduler: 15m every 3 min, hourly every 5 min, maker last 3 min")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "serve":
        import uvicorn

        uvicorn.run("kalshibot.web:app", host=args.host, port=args.port, reload=False)
        return
    if args.command == "scan":
        asyncio.run(_scan(args.section, args.json, args.limit))
        return
    asyncio.run(_campaign(args))


async def _scan(section: str | None, as_json: bool, limit: int) -> None:
    from kalshibot.scanner import Scanner

    scanner = Scanner()
    try:
        snap = await scanner.snapshot(force=True)
    finally:
        await scanner.aclose()

    if as_json:
        if section:
            print(json.dumps(snap["sections"][section], indent=2))
        else:
            print(json.dumps(snap, indent=2))
        return

    targets = [section] if section else list(SECTIONS)
    for key in targets:
        block = snap["sections"][key]
        print(f"\n=== {SECTION_LABELS[key]} ({block['stats']['opportunities']} edges ≥ 2¢) ===")
        rows = block["predictions"][:limit]
        if not rows:
            print("  (no open markets found)")
            continue
        for row in rows:
            model = f"{100 * (row['model_prob'] or 0):5.1f}%"
            mkt = f"{100 * (row['market_prob'] or 0):5.1f}%"
            edge = f"{100 * row['edge']:+5.1f}¢"
            print(
                f"  {row['side']:<3} {edge}  model {model}  mkt {mkt}  "
                f"{row['event_title'][:48]:<48}  {row['subtitle'][:28]}"
            )


async def _campaign(args: argparse.Namespace) -> None:
    from kalshibot.campaign.engine import CampaignEngine, run_scheduler

    engine = CampaignEngine()
    try:
        if args.campaign_cmd == "status":
            print(json.dumps(await engine.public_status(), indent=2))
            return
        if args.campaign_cmd == "fire":
            result = await engine.fire(args.loop)
            print(json.dumps(result, indent=2, default=str))
            return
        print("Scheduler started (15m / hourly / maker). Ctrl-C to stop.")
        await run_scheduler(engine)
    finally:
        await engine.aclose()


if __name__ == "__main__":
    main()
