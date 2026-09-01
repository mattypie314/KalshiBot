from __future__ import annotations

import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from kalshibot.assets import SECTIONS
from kalshibot.campaign.engine import CampaignEngine
from kalshibot.campaign.sizing import apply_phone_overrides
from kalshibot.charts import market_chart
from kalshibot.scanner import Scanner

STATIC_DIR = Path(__file__).parent / "static"

# Raspberry Pi / slim OS images often lack /etc/mime.types. Safari then
# refuses to apply CSS or run JS served as application/octet-stream.
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("image/png", ".png")
mimetypes.add_type("application/manifest+json", ".webmanifest")


def _index_html() -> str:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8").replace("</", "<\\/")
    html = html.replace(
        '<link rel="stylesheet" href="/static/styles.css" />',
        f"<style>\n{css}\n</style>",
        1,
    )
    html = html.replace(
        '<script src="/static/app.js"></script>',
        f"<script>\n{js}\n</script>",
        1,
    )
    return html


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


@app.get("/manifest.webmanifest")
async def manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/")
@app.get("/portfolio")
@app.get("/market/{ticker:path}")
async def index(ticker: str | None = None) -> HTMLResponse:
    return HTMLResponse(_index_html(), headers={"Cache-Control": "no-store"})


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
    live: bool | None = None
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
    if payload.live is True and not engine.kalshi.can_trade:
        raise HTTPException(
            status_code=400,
            detail="Need Kalshi API key and private key on this host before going LIVE.",
        )
    notes = apply_phone_overrides(
        engine.tracker,
        halted=_flag(payload.halted),
        live=_flag(payload.live),
        maker_auto=_flag(payload.maker_auto),
        follow=_flag(payload.follow_kalshi_cash),
    )
    engine._reload_playbook()
    for note in notes:
        engine.tracker.note(note, "sizing")
    engine.tracker.save()
    return {"notes": notes, "status": engine.status()}


@app.get("/api/chart/{series_ticker}/{ticker}")
async def chart(series_ticker: str, ticker: str, hours: float = 6.0) -> dict:
    scanner: Scanner = app.state.scanner
    try:
        return await market_chart(scanner._kalshi, series_ticker, ticker, hours=hours)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Chart unavailable: {exc}") from exc


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
