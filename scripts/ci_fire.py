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
    mode = "LIVE MONEY" if engine.live else "PRACTICE (no real Kalshi orders)"
    print(f"KalshiBot campaign: {mode} · loop={requested} · can_trade={engine.kalshi.can_trade}")
    try:
        reset_flag = os.environ.get("RESET_FIFTEEN", "").strip().lower()
        if reset_flag in {"1", "true", "yes"}:
            engine.tracker.load()
            engine.tracker.reset_pot("fifteen")
            engine.tracker.note("Reset $5 fifteen pot to $5 / $0 / unstopped.", "fifteen", quiet=False)
            engine.tracker.save()
            print("Reset fifteen pot: bankroll $5, realized $0, stopped false")
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
