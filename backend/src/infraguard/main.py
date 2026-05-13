"""FastAPI app entrypoint.

Run with: uvicorn infraguard.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .drift import build_scanner_from_settings
from .routes import router
from .store import drift_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def _drift_scan_loop() -> None:
    """Periodic background drift scan. Only runs when AWS_DRIFT_ENABLED=true."""
    scanner = build_scanner_from_settings()
    interval = settings.aws_drift_scan_interval_seconds
    logger.info("Drift scan loop starting (interval=%ss, scanner=%s)", interval, scanner.name)
    while True:
        try:
            findings = await scanner.scan()
            counts = await drift_store.apply_scan(findings)
            logger.info("Drift scan complete: %s", counts)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Drift scan failed; continuing loop")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task: asyncio.Task | None = None
    if settings.aws_drift_enabled:
        task = asyncio.create_task(_drift_scan_loop())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="InfraGuard Backend",
    description="AI-Managed IaC Workflow — Claude Managed Agents driving Terraform remediation",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
