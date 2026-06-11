"""FastAPI server for the sci-fi HUD dashboard."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import Settings
from .data import build_payload, empty_payload

log = logging.getLogger("app.dashboard")

DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "dashboard"
_STATE_TIMEOUT_S = 8.0
_cache: dict[str, object] = {"payload": None, "ts": 0.0, "mode": "paper"}


def create_app(cfg: Settings) -> FastAPI:
    app = FastAPI(title="Polymarket Bot HUD", docs_url="/docs", redoc_url=None)

    @app.get("/api/state")
    async def api_state(mode: str = "paper"):
        now = time.time()
        cached = _cache.get("payload")
        if cached and _cache.get("mode") == mode and now - float(_cache.get("ts", 0)) < 2.0:
            return cached
        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(build_payload, cfg, mode),
                timeout=_STATE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            payload = empty_payload(cfg, mode=mode, error="database query timed out")
        except Exception as e:
            log.exception("dashboard state failed")
            payload = empty_payload(cfg, mode=mode, error=str(e))
        _cache["payload"] = payload
        _cache["ts"] = now
        _cache["mode"] = mode
        return payload

    @app.get("/api/health")
    def api_health():
        return {"ok": True}

    static = DASHBOARD_DIR / "static"
    if static.is_dir():
        app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        html = DASHBOARD_DIR / "index.html"
        if html.is_file():
            return FileResponse(html)
        return {"error": "dashboard/index.html not found"}

    return app


def run_dashboard(cfg: Settings, host: str = "127.0.0.1", port: int = 8080) -> None:
    log.info("HUD dashboard at http://%s:%d (polling /api/state)", host, port)
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="info")
