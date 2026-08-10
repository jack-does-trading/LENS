from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import analyses, books, daily_logs, ingestion, principles, streaks, suggestions, users

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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
