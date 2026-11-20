
import os
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.database import get_connection, init_db
from app.routes.summaries import router as summaries_router
from app.routes.jobs import router as jobs_router
from app.services.readiness import readiness_checks


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="StudyHub Synthesis Service",
    version="0.4.0",
    lifespan=lifespan
)

app.include_router(summaries_router)
app.include_router(jobs_router)


@app.get("/ready")
def ready():
    checks = readiness_checks()
    healthy = all(checks.values())
    return JSONResponse(status_code=200 if healthy else 503, content={
        "status": "ready" if healthy else "not_ready", "checks": checks,
        "document_provider": os.getenv("DOCUMENT_PROVIDER", "search"),
        "heartbeat_max_age_seconds": 45,
    })


@app.get("/")
def root():
    return {
        "service": "synthesis",
        "status": "running",
        "version": "0.4.0"
    }


@app.get("/health")
def health():
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()

        return {
            "status": "healthy",
            "database": "connected"
        }

    except psycopg.Error:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable"
        )
