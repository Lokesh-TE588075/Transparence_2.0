"""Application configuration using pydantic-settings.

All external dependencies (LLM endpoints, SQL warehouse, table names) are
configured through environment variables. Nothing is hardcoded.

In Databricks Apps, DATABRICKS_HOST is auto-injected. Auth token is obtained
via the Databricks SDK's default credential chain (service principal in Apps).
"""

import os
from typing import List, Optional
from pydantic_settings import BaseSettings


def _detect_databricks_host() -> str:
    """Auto-detect workspace host from environment (set in Databricks Apps)."""
    return os.getenv("DATABRICKS_HOST", os.getenv("DB_HOST", "https://te-ss-coe-dev.cloud.databricks.com"))


def _get_auth_token() -> str:
    """Get auth token using Databricks SDK credential chain.
    
    In Databricks Apps: uses service principal credentials (auto-injected).
    In development: uses PAT from DATABRICKS_TOKEN env var or CLI auth.
    """
    # First check explicit token
    token = os.getenv("DATABRICKS_TOKEN", "")
    if token:
        return token
    
    # Fall back to SDK credential chain (works in Apps and local dev with CLI)
    try:
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient()
        headers = w.config.authenticate()
        for k, v in headers.items():
            if k.lower() == "authorization":
                return v.replace("Bearer ", "")
    except Exception:
        pass
    
    return ""


class Settings(BaseSettings):
    """Application settings. All values come from environment variables."""

    # --- Databricks Connectivity ---
    DATABRICKS_HOST: str = _detect_databricks_host()
    DATABRICKS_TOKEN: str = _get_auth_token()
    DATABRICKS_SQL_WAREHOUSE_PATH: str = os.getenv(
        "DATABRICKS_SQL_WAREHOUSE_PATH", "/sql/1.0/warehouses/8e46614f7064d8fd"
    )

    # --- Data Tables (fully qualified: catalog.schema.table) ---
    SHIPMENT_TABLE_NAME: str = os.getenv(
        "SHIPMENT_TABLE_NAME", "onedata_fn_ion_dev.ion_l0_raw.lbn_with_scorecard"
    )
    FEEDBACK_TABLE_NAME: str = os.getenv(
        "FEEDBACK_TABLE_NAME", "onedata_fn_ion_dev.ion_l0_raw.chatbot_feedback"
    )
    AUDIT_TABLE_NAME: str = os.getenv(
        "AUDIT_TABLE_NAME", "onedata_fn_ion_dev.ion_l0_raw.query_audit_log"
    )
    CONVERSATIONS_TABLE_NAME: str = os.getenv(
        "CONVERSATIONS_TABLE_NAME", "onedata_fn_ion_dev.ion_l0_raw.shipmate_conversations"
    )
    MESSAGES_TABLE_NAME: str = os.getenv(
        "MESSAGES_TABLE_NAME", "onedata_fn_ion_dev.ion_l0_raw.shipmate_messages"
    )

    # --- LLM Endpoints (configured via env vars in app.yaml) ---
    LLM_ENDPOINT_PRIMARY: str = os.getenv("LLM_ENDPOINT_PRIMARY", "")
    LLM_ENDPOINT_FAST: str = os.getenv("LLM_ENDPOINT_FAST", "")
    LLM_MAX_TOKENS_SQL: int = 1000
    LLM_MAX_TOKENS_SUMMARY: int = 500
    LLM_TEMPERATURE: float = 0.0
    LLM_TIMEOUT_SECONDS: int = 60

    # --- Export / Storage ---
    EXPORT_VOLUME_PATH: str = os.getenv("EXPORT_VOLUME_PATH", "/tmp/chatbot_exports")

    # --- SQL Execution ---
    SQL_QUERY_TIMEOUT_SECONDS: int = 120
    SQL_MAX_RETRY_ATTEMPTS: int = 2

    # --- Session ---
    SESSION_MAX_AGE_HOURS: int = 24
    SESSION_COOKIE_NAME: str = "transparence_session_id"

    # --- CORS (Databricks Apps proxies all traffic, but keep for local dev) ---
    CORS_ALLOWED_ORIGINS: List[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://localhost:8000",
    ]

    # --- App Meta ---
    APP_NAME: str = "TransparencE - Shipment Intelligence"
    APP_VERSION: str = "2.0.0"
    APP_PUBLIC_NAME: str = os.getenv(
        "APP_PUBLIC_NAME", "TransparencE Shipment Intelligence"
    )
    APP_BRAND_OWNER: str = os.getenv(
        "APP_BRAND_OWNER", "GLOG team"
    )
    DEBUG: bool = False

    # Phase 8: Delta conversation state persistence
    USE_DELTA_CONVERSATION_STATE: bool = False
    CONVERSATION_STATE_TABLE_NAME: str = "onedata_fn_ion_dev.ion_l0_raw.shipmate_conversation_state"
    CONVERSATION_STATE_TTL_HOURS: int = 24
    CONVERSATION_STATE_CLEANUP_HARD_DELETE: bool = False

    # --- New Accuracy Pipeline Feature Flags (Phase 7B) ---
    USE_NEW_ACCURACY_PIPELINE: bool = os.getenv(
        "USE_NEW_ACCURACY_PIPELINE", "false"
    ).lower() in ("true", "1", "yes")
    NEW_PIPELINE_FALLBACK_TO_OLD: bool = os.getenv(
        "NEW_PIPELINE_FALLBACK_TO_OLD", "true"
    ).lower() in ("true", "1", "yes")
    NEW_PIPELINE_DEBUG: bool = os.getenv(
        "NEW_PIPELINE_DEBUG", "false"
    ).lower() in ("true", "1", "yes")

    # --- Genie Backend Feature Flags (Phase G4/G5) ---
    # USE_GENIE_BACKEND=false by default — Genie is never the active path
    # until explicitly enabled per-environment.
    USE_GENIE_BACKEND: bool = os.getenv(
        "USE_GENIE_BACKEND", "false"
    ).lower() in ("true", "1", "yes")
    GENIE_FALLBACK_TO_CUSTOM_PIPELINE: bool = os.getenv(
        "GENIE_FALLBACK_TO_CUSTOM_PIPELINE", "true"
    ).lower() in ("true", "1", "yes")
    GENIE_SPACE_ID: str = os.getenv(
        "GENIE_SPACE_ID", "01f17a93e6aa1b97a9da7ef329e15e46"
    )
    # Numeric flags: pydantic-settings reads the matching env var and coerces the type.
    GENIE_RESPONSE_TIMEOUT_SECONDS: int = 120
    GENIE_POLL_INTERVAL_SECONDS: float = 2.0
    GENIE_ENABLE_VISUALIZATION: bool = os.getenv(
        "GENIE_ENABLE_VISUALIZATION", "true"
    ).lower() in ("true", "1", "yes")
    GENIE_DEBUG: bool = os.getenv(
        "GENIE_DEBUG", "false"
    ).lower() in ("true", "1", "yes")

    # --- Genie UX & Quality flags (Phase G9) ---
    # Prompt enrichment: steer Genie toward analytical summaries for broad questions.
    GENIE_ENABLE_PROMPT_ENRICHMENT: bool = os.getenv(
        "GENIE_ENABLE_PROMPT_ENRICHMENT", "true"
    ).lower() in ("true", "1", "yes")
    # Show generated SQL in UI (false = hide from normal users; true = show for debug/admin).
    GENIE_SHOW_SQL: bool = os.getenv(
        "GENIE_SHOW_SQL", "false"
    ).lower() in ("true", "1", "yes")
    # Maximum rows to store in CSV export from already returned Genie rows.
    GENIE_MAX_DOWNLOAD_ROWS: int = int(os.getenv("GENIE_MAX_DOWNLOAD_ROWS", "5000"))
    # Number of rows to render in the frontend table preview (legacy flag retained).
    GENIE_TABLE_DISPLAY_ROW_LIMIT: int = int(os.getenv("GENIE_TABLE_DISPLAY_ROW_LIMIT", "100"))
    # E4: async export flags.
    GENIE_ASYNC_EXPORT_ENABLED: bool = os.getenv(
        "GENIE_ASYNC_EXPORT_ENABLED", "true"
    ).lower() in ("true", "1", "yes")
    # Safety default is returned_rows_only until full-query export is explicitly enabled.
    GENIE_EXPORT_MODE: str = os.getenv(
        "GENIE_EXPORT_MODE", "returned_rows_only"
    )
    GENIE_MAX_EXPORT_ROWS: int = int(os.getenv("GENIE_MAX_EXPORT_ROWS", "100000"))
    GENIE_EXPORT_PREVIEW_ROW_LIMIT: int = int(
        os.getenv("GENIE_EXPORT_PREVIEW_ROW_LIMIT", "100")
    )
    GENIE_EXPORT_QUERY_TIMEOUT_SECONDS: int = int(
        os.getenv("GENIE_EXPORT_QUERY_TIMEOUT_SECONDS", "300")
    )
    GENIE_EXPORT_STRIP_LIMIT: bool = os.getenv(
        "GENIE_EXPORT_STRIP_LIMIT", "true"
    ).lower() in ("true", "1", "yes")
    GENIE_EXPORT_STATUS_TTL_HOURS: int = int(
        os.getenv("GENIE_EXPORT_STATUS_TTL_HOURS", "24")
    )

    # --- Genie Table Summarizer flags (Phase Q3) ---
    # Enable deterministic post-processing when Genie returns a raw row dump.
    GENIE_ENABLE_TABLE_SUMMARY: bool = os.getenv(
        "GENIE_ENABLE_TABLE_SUMMARY", "true"
    ).lower() in ("true", "1", "yes")
    # Minimum rows before the summarizer activates (avoids summarizing small results).
    GENIE_RAW_TABLE_SUMMARY_THRESHOLD: int = int(
        os.getenv("GENIE_RAW_TABLE_SUMMARY_THRESHOLD", "50")
    )
    # How many top entries to include in breakdowns.
    GENIE_SUMMARY_TOP_N: int = int(os.getenv("GENIE_SUMMARY_TOP_N", "5"))
    # Enable computed_chart_data for fallback charting when Genie returns no viz.
    GENIE_ENABLE_COMPUTED_CHART: bool = os.getenv(
        "GENIE_ENABLE_COMPUTED_CHART", "true"
    ).lower() in ("true", "1", "yes")

    # -------------------------------------------------------------------------
    # Diagnostic Tracing (Phase DIAG)
    # -------------------------------------------------------------------------
    # Master switch.  All other DIAG flags are ignored when this is false.
    # Default: false — zero impact on production unless explicitly enabled.
    TRANSPARENCE_DIAGNOSTIC_TRACING_ENABLED: bool = os.getenv(
        "TRANSPARENCE_DIAGNOSTIC_TRACING_ENABLED", "false"
    ).lower() in ("true", "1", "yes")

    # Storage backend: "delta" writes to Delta table; "none" skips persistence.
    TRANSPARENCE_DIAGNOSTIC_STORE: str = os.getenv(
        "TRANSPARENCE_DIAGNOSTIC_STORE", "delta"
    )

    # Fully qualified Delta table for trace records.
    TRANSPARENCE_DIAGNOSTIC_TABLE: str = os.getenv(
        "TRANSPARENCE_DIAGNOSTIC_TABLE",
        "onedata_fn_ion_dev.ion_l0_raw.transparence_diagnostic_trace",
    )

    # Include safe prompt excerpts in traces.  True by default when tracing is on.
    TRANSPARENCE_DIAGNOSTIC_LOG_PROMPT_EXCERPT: bool = os.getenv(
        "TRANSPARENCE_DIAGNOSTIC_LOG_PROMPT_EXCERPT", "true"
    ).lower() in ("true", "1", "yes")

    # Include full SQL in traces.  FALSE by default — set true only for isolated
    # diagnostic deployments where the SQL is safe to persist.
    TRANSPARENCE_DIAGNOSTIC_LOG_SQL: bool = os.getenv(
        "TRANSPARENCE_DIAGNOSTIC_LOG_SQL", "false"
    ).lower() in ("true", "1", "yes")

    # Maximum characters stored for prompt/SQL excerpts.
    TRANSPARENCE_DIAGNOSTIC_PROMPT_EXCERPT_LENGTH: int = int(
        os.getenv("TRANSPARENCE_DIAGNOSTIC_PROMPT_EXCERPT_LENGTH", "200")
    )

    # TTL in days.  Rows older than this are eligible for cleanup_expired_rows().
    TRANSPARENCE_DIAGNOSTIC_TTL_DAYS: int = int(
        os.getenv("TRANSPARENCE_DIAGNOSTIC_TTL_DAYS", "7")
    )

    # Optional deployment ID tag written to every trace row.
    # Set to the active deployment UUID in app.yaml when deploying for a soak test.
    TRANSPARENCE_DEPLOYMENT_ID: str = os.getenv(
        "TRANSPARENCE_DEPLOYMENT_ID", ""
    )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Singleton instance
settings = Settings()
