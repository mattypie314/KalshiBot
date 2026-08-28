from __future__ import annotations

import asyncio
import json
import logging
import os

from kalshibot.campaign.engine import CampaignEngine
from kalshibot.campaign.sizing import apply_phone_overrides, playbook_from_sizing


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    requested = os.environ.get("LOOP", "auto").strip().lower() or "auto"
    engine = CampaignEngine()
    notes = apply_phone_overrides(
        engine.tracker,
        bankroll=os.environ.get("BANKROLL"),
        follow=os.environ.get("FOLLOW_KALSHI"),
        maker_auto=os.environ.get("MAKER_AUTO"),
        risk_percent=os.environ.get("RISK_PERCENT"),
        risk_cap_percent=os.environ.get("RISK_CAP_PERCENT"),
    )
    engine.playbook = playbook_from_sizing(engine.cfg, engine.tracker.state.get("sizing") or {})
    for note in notes:
        print(note)
        engine.tracker.note(note, "sizing")
    engine.tracker.save()
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
        results.append(await engine.fire("maker"))
        print(json.dumps({"results": results, "status": engine.status()}, indent=2, default=str))
    finally:
        await engine.aclose()


if __name__ == "__main__":
    asyncio.run(main())
