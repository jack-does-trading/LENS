import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import (
    analyses,
    books,
    daily_logs,
    ingestion,
    metrics,
    presence,
    principles,
    streaks,
    suggestions,
    users,
)
from app.telemetry import configure_json_logging

# Opt-in (set LENS_JSON_LOGS=1 on Render) rather than always-on: plain text
# reads better locally and under pytest, and reconfiguring the root logger
# unconditionally at import time would fight uvicorn's own handler setup.
if os.environ.get("LENS_JSON_LOGS") == "1":
    configure_json_logging(logging.INFO)

app = FastAPI(title="Lens API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-Ingestion-Api-Key"],
)

app.include_router(books.router, prefix="/api")
app.include_router(principles.router, prefix="/api")
app.include_router(daily_logs.router, prefix="/api")
app.include_router(ingestion.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(analyses.router, prefix="/api")
app.include_router(suggestions.router, prefix="/api")
app.include_router(streaks.router, prefix="/api")
app.include_router(presence.router, prefix="/api")
app.include_router(metrics.router, prefix="/api")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
