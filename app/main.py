"""TransparencE Shipment Chatbot — FastAPI application entry point.

This is the main application module. It initializes the FastAPI app,
configures middleware (CORS, session), registers all route modules,
and serves the React static frontend build.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routes import chat, feedback, export, health
from app.services.genie_backend_factory import reset_genie_pipeline
from app.utils.logging import setup_logging

# Initialize structured logging
setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown events."""
    logger.info(
        "Starting %s v%s", settings.APP_NAME, settings.APP_VERSION
    )
    # TODO: Initialize SQL connection pool
    # TODO: Initialize LLM service
    # TODO: Initialize conversation manager
    try:
        yield
    finally:
        # Shutdown
        logger.info("Shutting down %s", settings.APP_NAME)
        # TODO: Close SQL connections
        # TODO: Flush pending audit logs
        try:
            reset_genie_pipeline()
        except Exception:
            logger.error(
                "Genie pipeline cleanup failed during shutdown",
                exc_info=True,
            )


# Create FastAPI application
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# --- Middleware ---

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Session middleware (cookie-based session ID)
@app.middleware("http")
async def session_middleware(request: Request, call_next):
    """Attach or create a session ID for each request."""
    import secrets
    import re

    _SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{20,200}$")
    cookie_val = request.cookies.get(settings.SESSION_COOKIE_NAME, "")

    if _SESSION_ID_RE.fullmatch(cookie_val):
        session_id = cookie_val
    else:
        session_id = secrets.token_urlsafe(32)

    request.state.session_id = session_id
    response = await call_next(request)

    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        secure=not settings.DEBUG,
        samesite="lax",
        path="/",
        max_age=settings.SESSION_MAX_AGE_HOURS * 3600,
    )
    return response


# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch unhandled exceptions and return safe user message."""
    logger.error(
        "Unhandled exception on %s %s: %s",
        request.method,
        request.url.path,
        exc,
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "message": "An internal error occurred. Please try again.",
        },
    )


# --- Register Routes ---
app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(chat.router, prefix="/api", tags=["chat"])
app.include_router(feedback.router, prefix="/api", tags=["feedback"])
app.include_router(export.router, prefix="/api", tags=["export"])


# --- Static Files (React frontend build) ---
# Mount AFTER API routes so /api/* takes priority
import os
_static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
if os.path.isdir(_static_dir):
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="frontend")
