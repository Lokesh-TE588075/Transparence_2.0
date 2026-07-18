"""Factory / singleton provider for GeniePipeline.

Keeps all Genie object construction out of chat.py so the route stays a
thin orchestrator.  The singleton is created lazily on the first
``get_genie_pipeline()`` call and reused for every subsequent request
within the same worker process.

Thread-safety: a module-level threading.Lock guards the double-checked
locking pattern so the pipeline is created exactly once even if multiple
requests arrive simultaneously on startup.

Auth note:
  When ``user_token`` is passed (extracted from the
  ``X-Forwarded-Access-Token`` header in Databricks Apps), a *fresh*
  GenieClient is created per-request so that Genie calls run under the
  *user's* permissions.  The pipeline singleton is only used for
  service-principal / test callers that do not supply a user token.
  No tokens are read or stored in this module.
"""

import logging
import threading
from typing import Optional

from app.services.genie_client import GenieClient
from app.services.genie_pipeline import GeniePipeline
from app.services.genie_session_store import GenieSessionStore
from app.services.audit_service import AuditService
from app.services.export_job_manager import get_export_job_manager
from app.services.sql_service import SQLService

logger = logging.getLogger(__name__)

_lock: threading.Lock = threading.Lock()
_genie_pipeline: Optional[GeniePipeline] = None


def get_genie_pipeline(
    user_token: Optional[str] = None,
) -> GeniePipeline:
    """Return a GeniePipeline wired for the current request.

    When *user_token* is supplied (e.g. the ``X-Forwarded-Access-Token``
    forwarded by Databricks Apps), a fresh ``GenieClient`` is created for
    this call so the Genie API is invoked under the *user's* permissions.
    The ``GenieSessionStore`` singleton is still shared across requests for
    efficient session caching.

    When *user_token* is absent or empty, the process-level singleton is
    returned as before (SP / CLI / env-var auth).

    Raises:
        Exception: If the pipeline cannot be constructed (e.g. bad config).
                   chat.py wraps calls in try/except so the app stays up.
    """
    if user_token:
        # Per-request pipeline with user-delegated credentials
        return _build_pipeline(user_token=user_token)

    global _genie_pipeline
    if _genie_pipeline is None:           # fast path — no lock
        with _lock:                       # slow path — only one thread builds
            if _genie_pipeline is None:
                _genie_pipeline = _build_pipeline()
    return _genie_pipeline


def reset_genie_pipeline() -> None:
    """Discard the singleton so the next call to get_genie_pipeline() rebuilds it.

    Intended for tests and for live config reloads (e.g. after changing
    GENIE_SPACE_ID via env var hot-reload).
    """
    global _genie_pipeline
    with _lock:
        _genie_pipeline = None
    logger.debug("GeniePipeline singleton reset")


def _build_pipeline(user_token: Optional[str] = None) -> GeniePipeline:
    """Construct and wire a full Genie pipeline from app settings.

    Config is read lazily here (inside the lock) so that any test that
    patches settings before the first call sees the patched values.

    Args:
        user_token: Optional OAuth token.  When provided, the GenieClient
                    will use it instead of the SDK credential chain so that
                    Genie calls run under the user's own workspace permissions.
    """
    # Import settings lazily so tests can patch it before this runs
    from app.config import settings

    client = GenieClient(
        host=settings.DATABRICKS_HOST,
        timeout_seconds=settings.GENIE_RESPONSE_TIMEOUT_SECONDS,
        user_token=user_token or "",
    )
    store = GenieSessionStore()
    audit_svc = AuditService()
    export_job_manager = get_export_job_manager(
        ttl_hours=settings.GENIE_EXPORT_STATUS_TTL_HOURS
    )
    sql_service = SQLService()
    pipeline = GeniePipeline(
        genie_client=client,
        session_store=store,
        space_id=settings.GENIE_SPACE_ID,
        timeout_seconds=settings.GENIE_RESPONSE_TIMEOUT_SECONDS,
        poll_interval_seconds=settings.GENIE_POLL_INTERVAL_SECONDS,
        debug=settings.GENIE_DEBUG,
        fetch_query_results=True,
        enable_prompt_enrichment=settings.GENIE_ENABLE_PROMPT_ENRICHMENT,
        table_display_row_limit=settings.GENIE_EXPORT_PREVIEW_ROW_LIMIT,
        max_download_rows=settings.GENIE_MAX_DOWNLOAD_ROWS,
        audit_service=audit_svc,
        async_export_enabled=settings.GENIE_ASYNC_EXPORT_ENABLED,
        export_mode=settings.GENIE_EXPORT_MODE,
        max_export_rows=settings.GENIE_MAX_EXPORT_ROWS,
        export_query_timeout_seconds=settings.GENIE_EXPORT_QUERY_TIMEOUT_SECONDS,
        export_strip_limit=settings.GENIE_EXPORT_STRIP_LIMIT,
        export_job_manager=export_job_manager,
        sql_service=sql_service,
        # Q3: table summarizer
        enable_table_summary=settings.GENIE_ENABLE_TABLE_SUMMARY,
        summary_threshold=settings.GENIE_RAW_TABLE_SUMMARY_THRESHOLD,
        summary_top_n=settings.GENIE_SUMMARY_TOP_N,
        enable_computed_chart=settings.GENIE_ENABLE_COMPUTED_CHART,
    )
    logger.info(
        "GeniePipeline built: space_id=%s timeout=%ss debug=%s enrichment=%s summary=%s",
        settings.GENIE_SPACE_ID,
        settings.GENIE_RESPONSE_TIMEOUT_SECONDS,
        settings.GENIE_DEBUG,
        settings.GENIE_ENABLE_PROMPT_ENRICHMENT,
        settings.GENIE_ENABLE_TABLE_SUMMARY,
    )
    return pipeline
