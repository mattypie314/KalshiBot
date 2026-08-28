from __future__ import annotations

import asyncio
import json
import logging
import os

from kalshibot.campaign.engine import CampaignEngine
from kalshibot.campaign.rules import in_maker_window


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    requested = os.environ.get("LOOP", "auto").strip().lower() or "auto"
    engine = CampaignEngine()
    raw_bankroll = os.environ.get("BANKROLL", "").strip()
    if raw_bankroll:
        engine.tracker.load()
        engine.tracker.set_bankroll(float(raw_bankroll.replace("$", "").replace(",", "")))
        engine.tracker.save()
        print(f"Campaign bankroll set to ${float(raw_bankroll):.2f}")
    mode = "LIVE MONEY" if engine.live else "PRACTICE (no real Kalshi orders)"
    print(f"KalshiBot campaign: {mode} · loop={requested} · can_trade={engine.kalshi.can_trade}")
    try:
        if requested in {"fifteen", "hourly", "maker"}:
            result = await engine.fire(requested)
            print(json.dumps(result, indent=2, default=str))
            return
        results = []
        results.append(await engine.fire("fifteen"))
        results.append(await engine.fire("hourly"))
        if in_maker_window():
            results.append(await engine.fire("maker"))
        print(json.dumps({"results": results, "status": engine.status()}, indent=2, default=str))
    finally:
        await engine.aclose()


if __name__ == "__main__":
    asyncio.run(main())
