from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from kalshibot.assets import SECTIONS
from kalshibot.scanner import Scanner

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    scanner = Scanner()
    app.state.scanner = scanner
    try:
        yield
    finally:
        await scanner.aclose()


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
