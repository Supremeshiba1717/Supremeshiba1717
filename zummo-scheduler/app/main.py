"""FastAPI application entrypoint.

Wires routers, starts the APScheduler background jobs on startup, and serves
generated PDFs at a public /files path (used as the MMS media URL).
"""

import logging
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.routers import admin, dashboard, sms_webhook
from app.scheduler import create_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(title="Zummo Bike Scheduling Bot")
app.include_router(dashboard.router)
app.include_router(sms_webhook.router)
app.include_router(admin.router)

_scheduler = None


@app.on_event("startup")
def _startup():
    global _scheduler
    _scheduler = create_scheduler()
    _scheduler.start()
    logging.getLogger("main").info("Scheduler started")


@app.on_event("shutdown")
def _shutdown():
    if _scheduler:
        _scheduler.shutdown(wait=False)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/files/{filename}")
def serve_file(filename: str):
    """Publicly serve a generated PDF (used as the MMS media URL for Twilio).

    Basename-only guard prevents path traversal.
    """
    safe = os.path.basename(filename)
    path = os.path.join("output", "pdfs", safe)
    if not os.path.exists(path):
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="application/pdf")
