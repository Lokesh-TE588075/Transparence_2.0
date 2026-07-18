"""Health check endpoints."""

from fastapi import APIRouter
from app.config import settings

router = APIRouter()


@router.get("/health")
async def health_check():
    """Application health check."""
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
    }


@router.get("/ping")
async def ping():
    """Simple liveness probe."""
    return {"status": "alive"}
