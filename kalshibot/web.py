from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from kalshibot.assets import SECTIONS
from kalshibot.campaign.engine import CampaignEngine
from kalshibot.campaign.sizing import apply_phone_overrides
from kalshibot.scanner import Scanner

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    scanner = Scanner()
    campaign = CampaignEngine()
    app.state.scanner = scanner
    app.state.campaign = campaign
    try:
        yield
    finally:
        await scanner.aclose()
        await campaign.aclose()


app = FastAPI(title="KalshiBot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/predictions")
async def predictions(force: bool = False) -> dict:
    scanner: Scanner = app.state.scanner
    return await scanner.snapshot(force=force)


@app.get("/api/campaign")
async def campaign_status() -> dict:
    engine: CampaignEngine = app.state.campaign
    return engine.status()


class CampaignControl(BaseModel):
    halted: bool | None = None
    maker_auto: bool | None = None
    follow_kalshi_cash: bool | None = None


def _flag(value: bool | None) -> str | None:
    if value is None:
        return None
    return "yes" if value else "no"


@app.post("/api/campaign/fire/{loop}")
async def campaign_fire(loop: str) -> dict:
    if loop not in {"fifteen", "hourly", "maker"}:
        raise HTTPException(status_code=404, detail="Unknown loop")
    engine: CampaignEngine = app.state.campaign
    return await engine.fire(loop)


@app.post("/api/campaign/control")
async def campaign_control(payload: CampaignControl) -> dict:
    engine: CampaignEngine = app.state.campaign
    notes = apply_phone_overrides(
        engine.tracker,
        halted=_flag(payload.halted),
        maker_auto=_flag(payload.maker_auto),
        follow=_flag(payload.follow_kalshi_cash),
    )
    engine._reload_playbook()
    for note in notes:
        engine.tracker.note(note, "sizing")
    engine.tracker.save()
    return {"notes": notes, "status": engine.status()}


@app.get("/api/sections/{section_id}")
async def section(section_id: str, force: bool = False) -> dict:
    if section_id not in SECTIONS:
        raise HTTPException(status_code=404, detail="Unknown section")
    scanner: Scanner = app.state.scanner
    snap = await scanner.snapshot(force=force)
    payload = snap["sections"][section_id]
    payload["generated_at"] = snap["generated_at"]
    payload["disclaimer"] = snap["disclaimer"]
    return payload
