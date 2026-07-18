"""Delta-backed conversation state persistence for the TransparencE chatbot.

Provides the same public API as ConversationStateManager but stores structured
state in a Unity Catalog Delta table for cross-worker persistence and app-restart
survival.

Falls back to in-memory storage on Delta write failures — never breaks the user query.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.models.canonical_query import (
    CanonicalQuery,
    ConversationState,
    DateRange,
    QueryFilters,
    QueryIntent,
)
from app.services.conversation_state import (
    BusinessContext,
    ConversationStateManager,
    ResultMetadata,
    _NON_BUSINESS_INTENTS,
    DEFAULT_SESSION_TTL_SECONDS,
)

logger = logging.getLogger(__name__)


# =============================================================================
# SERIALIZATION HELPERS
# =============================================================================


def serialize_business_context(ctx: BusinessContext) -> str:
    """Serialize BusinessContext to JSON string for Delta storage."""

    def _default(obj):
        if hasattr(obj, "__dataclass_fields__"):
            return asdict(obj)
        if isinstance(obj, (datetime,)):
            return obj.isoformat()
        return str(obj)

    data = asdict(ctx)
    return json.dumps(data, default=_default)


def deserialize_business_context(json_str: str) -> BusinessContext:
    """Deserialize BusinessContext from JSON string.

    Reconstructs nested dataclasses (CanonicalQuery, QueryFilters, DateRange, etc.)
    from their dict representations.
    """
    data = json.loads(json_str)

    # Reconstruct nested dataclasses
    if data.get("last_canonical_query"):
        cq_data = data["last_canonical_query"]
        filters_data = cq_data.get("filters") or {}
        date_range = None
        if filters_data.get("date_range"):
            dr = filters_data["date_range"]
            date_range = DateRange(
                field=dr.get("field", "eta"),
                start=dr.get("start"),
                end=dr.get("end"),
            )
        filters = QueryFilters(
            source_=filters_data.get("source_"),
            destination=filters_data.get("destination"),
            transportation_mode_desc=filters_data.get("transportation_mode_desc"),
            business_unit_id=filters_data.get("business_unit_id"),
            status_category=filters_data.get("status_category"),
            date_range=date_range,
            shipment_ids=filters_data.get("shipment_ids"),
            execution_status_values=filters_data.get("execution_status_values"),
        )
        cq = CanonicalQuery(
            intent=cq_data.get("intent", ""),
            filters=filters,
            metrics=cq_data.get("metrics", []),
            group_by=cq_data.get("group_by", []),
            sort=cq_data.get("sort", []),
            limit=cq_data.get("limit", 500),
            confidence=cq_data.get("confidence", 0.0),
            source=cq_data.get("source", ""),
            requires_clarification=cq_data.get("requires_clarification", False),
            clarification_question=cq_data.get("clarification_question"),
            raw_input=cq_data.get("raw_input", ""),
            normalized_input=cq_data.get("normalized_input", ""),
        )
        data["last_canonical_query"] = cq
    else:
        data["last_canonical_query"] = None

    # Reconstruct last_filters
    if data.get("last_filters"):
        f = data["last_filters"]
        date_range = None
        if f.get("date_range"):
            dr = f["date_range"]
            date_range = DateRange(
                field=dr.get("field", "eta"),
                start=dr.get("start"),
                end=dr.get("end"),
            )
        data["last_filters"] = QueryFilters(
            source_=f.get("source_"),
            destination=f.get("destination"),
            transportation_mode_desc=f.get("transportation_mode_desc"),
            business_unit_id=f.get("business_unit_id"),
            status_category=f.get("status_category"),
            date_range=date_range,
            shipment_ids=f.get("shipment_ids"),
            execution_status_values=f.get("execution_status_values"),
        )
    else:
        data["last_filters"] = None

    # Reconstruct result metadata
    if data.get("last_result_metadata"):
        rm = data["last_result_metadata"]
        data["last_result_metadata"] = ResultMetadata(
            total_row_count=rm.get("total_row_count", 0),
            displayed_rows=rm.get("displayed_rows", 0),
            columns=rm.get("columns", []),
            has_aggregates=rm.get("has_aggregates", False),
            is_empty=rm.get("is_empty", True),
            execution_time_ms=rm.get("execution_time_ms"),
        )
    else:
        data["last_result_metadata"] = None

    return BusinessContext(
        last_user_input=data.get("last_user_input"),
        last_normalized_input=data.get("last_normalized_input"),
        last_canonical_query=data.get("last_canonical_query"),
        last_validated_sql=data.get("last_validated_sql"),
        last_result_metadata=data.get("last_result_metadata"),
        last_displayed_rows=data.get("last_displayed_rows"),
        last_total_row_count=data.get("last_total_row_count"),
        last_download_key=data.get("last_download_key"),
        last_filters=data.get("last_filters"),
        last_assistant_suggestion=data.get("last_assistant_suggestion"),
        last_result_summary=data.get("last_result_summary"),
        last_updated_at=data.get("last_updated_at", 0.0),
        query_count=data.get("query_count", 0),
    )


# =============================================================================
# DELTA SQL EXECUTOR (bypasses chat SQL validator)
# =============================================================================


class DeltaSQLExecutor:
    """Direct SQL executor for Delta state operations.

    Bypasses the chat SQL validator (which only allows SELECT).
    Uses the Databricks SDK Statement Execution API for DDL/DML.
    """

    def __init__(self, warehouse_id: str = None):
        self._warehouse_id = warehouse_id

    def execute(self, sql: str):
        """Execute SQL via Databricks SDK statement execution."""
        try:
            from databricks.sdk import WorkspaceClient
            from databricks.sdk.service.sql import StatementState

            w = WorkspaceClient()
            warehouse_id = self._warehouse_id
            if not warehouse_id:
                from app.config import settings
                # Extract warehouse ID from path like "/sql/1.0/warehouses/8e46614f7064d8fd"
                wh_path = settings.DATABRICKS_SQL_WAREHOUSE_PATH
                warehouse_id = wh_path.rstrip("/").split("/")[-1]

            response = w.statement_execution.execute_statement(
                warehouse_id=warehouse_id,
                statement=sql,
                wait_timeout="30s",
            )

            if response.status and response.status.state == StatementState.FAILED:
                error_msg = ""
                if response.status.error:
                    error_msg = response.status.error.message or str(response.status.error)
                return _DeltaResult(error=error_msg)

            rows = []
            if response.result and response.result.data_array:
                columns = []
                if response.manifest and response.manifest.schema and response.manifest.schema.columns:
                    columns = [c.name for c in response.manifest.schema.columns]
                for row_data in response.result.data_array:
                    if columns:
                        rows.append(dict(zip(columns, row_data)))
                    else:
                        rows.append(row_data)

            return _DeltaResult(rows=rows)

        except Exception as e:
            return _DeltaResult(error=str(e)[:500])


class _DeltaResult:
    """Simple result container for Delta SQL operations."""
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error


# =============================================================================
# DELTA SQL EXECUTOR (bypasses chat SQL validator)
# =============================================================================


class DeltaSQLExecutor:
    """Direct SQL executor for Delta state operations.

    Bypasses the chat SQL validator (which only allows SELECT).
    Uses the Databricks SDK Statement Execution API for DDL/DML.
    """

    def __init__(self, warehouse_id: str = None):
        self._warehouse_id = warehouse_id

    def execute(self, sql: str):
        """Execute SQL via Databricks SDK statement execution."""
        try:
            from databricks.sdk import WorkspaceClient
            from databricks.sdk.service.sql import StatementState

            w = WorkspaceClient()
            warehouse_id = self._warehouse_id
            if not warehouse_id:
                from app.config import settings
                # Extract warehouse ID from path like "/sql/1.0/warehouses/8e46614f7064d8fd"
                wh_path = settings.DATABRICKS_SQL_WAREHOUSE_PATH
                warehouse_id = wh_path.rstrip("/").split("/")[-1]

            response = w.statement_execution.execute_statement(
                warehouse_id=warehouse_id,
                statement=sql,
                wait_timeout="30s",
            )

            if response.status and response.status.state == StatementState.FAILED:
                error_msg = ""
                if response.status.error:
                    error_msg = response.status.error.message or str(response.status.error)
                return _DeltaResult(error=error_msg)

            rows = []
            if response.result and response.result.data_array:
                columns = []
                if response.manifest and response.manifest.schema and response.manifest.schema.columns:
                    columns = [c.name for c in response.manifest.schema.columns]
                for row_data in response.result.data_array:
                    if columns:
                        rows.append(dict(zip(columns, row_data)))
                    else:
                        rows.append(row_data)

            return _DeltaResult(rows=rows)

        except Exception as e:
            return _DeltaResult(error=str(e)[:500])


class _DeltaResult:
    """Simple result container for Delta SQL operations."""
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error


# =============================================================================
# DELTA-BACKED STATE MANAGER
# =============================================================================


class DeltaConversationStateManager(ConversationStateManager):
    """Delta-persisted conversation state with in-memory cache and fallback.

    Extends ConversationStateManager with Delta read/write operations.
    Falls back to in-memory-only mode on any Delta failure.

    Schema:
        conversation_id STRING
        user_id STRING
        updated_at TIMESTAMP
        expires_at TIMESTAMP
        state_json STRING
        last_intent STRING
        last_filters_json STRING
        last_result_metadata_json STRING
        is_active BOOLEAN
    """

    def __init__(
        self,
        table_name: str,
        ttl_hours: int = 24,
        sql_executor=None,
    ):
        super().__init__(session_ttl_seconds=ttl_hours * 3600)
        self._table_name = table_name
        self._ttl_hours = ttl_hours
        self._sql_executor = sql_executor
        self._delta_available = False
        self._init_delta()

    def _init_delta(self):
        """Initialize Delta table (create if not exists)."""
        if not self._sql_executor:
            logger.info("No SQL executor provided, Delta state disabled")
            return

        try:
            create_sql = f"""
            CREATE TABLE IF NOT EXISTS {self._table_name} (
                conversation_id STRING NOT NULL,
                user_id STRING,
                updated_at TIMESTAMP,
                expires_at TIMESTAMP,
                state_json STRING,
                last_intent STRING,
                last_filters_json STRING,
                last_result_metadata_json STRING,
                is_active BOOLEAN
            )
            USING DELTA
            COMMENT 'Structured conversation state for TransparencE chatbot'
            """
            result = self._sql_executor.execute(create_sql)
            if hasattr(result, "error") and result.error:
                logger.warning("Delta table creation failed: %s", result.error[:200])
            else:
                self._delta_available = True
                logger.info("Delta conversation state table ready: %s", self._table_name)
        except Exception as e:
            logger.warning("Delta init failed, using in-memory only: %s", str(e)[:200])

    # -------------------------------------------------------------------------
    # OVERRIDE: update_after_query (persist to Delta after in-memory update)
    # -------------------------------------------------------------------------

    def update_after_query(self, conversation_id: str, **kwargs) -> None:
        """Update business context and persist to Delta."""
        # First: update in-memory (parent behavior)
        super().update_after_query(conversation_id, **kwargs)

        # Then: persist to Delta (non-blocking, best-effort)
        if self._delta_available:
            self._persist_to_delta(conversation_id)

    # -------------------------------------------------------------------------
    # OVERRIDE: get_context (try Delta first if not in memory)
    # -------------------------------------------------------------------------

    def get_context(self, conversation_id: str) -> Optional[BusinessContext]:
        """Get context from memory first, then Delta if not found."""
        # Try in-memory first (fast path)
        ctx = super().get_context(conversation_id)
        if ctx is not None:
            return ctx

        # Try Delta (cold start / cross-worker recovery)
        if self._delta_available:
            ctx = self._load_from_delta(conversation_id)
            if ctx is not None:
                # Cache in memory for subsequent fast access
                self._sessions[conversation_id] = ctx
                return ctx

        return None

    # -------------------------------------------------------------------------
    # OVERRIDE: reset_context (mark inactive in Delta)
    # -------------------------------------------------------------------------

    def reset_context(self, conversation_id: str) -> None:
        """Clear context from memory and mark inactive in Delta."""
        super().reset_context(conversation_id)
        if self._delta_available:
            self._mark_inactive_in_delta(conversation_id)

    # -------------------------------------------------------------------------
    # DELTA OPERATIONS
    # -------------------------------------------------------------------------

    def _persist_to_delta(self, conversation_id: str) -> None:
        """Persist current context to Delta (best-effort)."""
        ctx = self._sessions.get(conversation_id)
        if ctx is None:
            return

        try:
            state_json = serialize_business_context(ctx)
            now = datetime.now(timezone.utc)
            expires = now + timedelta(hours=self._ttl_hours)

            # Extract searchable fields
            last_intent = ""
            last_filters_json = "{}"
            last_metadata_json = "{}"

            if ctx.last_canonical_query:
                last_intent = ctx.last_canonical_query.intent or ""
            if ctx.last_filters:
                last_filters_json = json.dumps(asdict(ctx.last_filters), default=str)
            if ctx.last_result_metadata:
                last_metadata_json = json.dumps(asdict(ctx.last_result_metadata), default=str)

            # Base64-encode state_json to safely embed in SQL string literal
            # (avoids issues with newlines, quotes, backslashes in SQL/JSON)
            state_json_b64 = base64.b64encode(state_json.encode()).decode()
            last_filters_escaped = last_filters_json.replace("'", "''").replace("\n", "\\n")
            last_metadata_escaped = last_metadata_json.replace("'", "''").replace("\n", "\\n")

            merge_sql = f"""
            MERGE INTO {self._table_name} AS target
            USING (SELECT '{conversation_id}' AS conversation_id) AS source
            ON target.conversation_id = source.conversation_id
            WHEN MATCHED THEN UPDATE SET
                updated_at = TIMESTAMP '{now.strftime("%Y-%m-%d %H:%M:%S")}',
                expires_at = TIMESTAMP '{expires.strftime("%Y-%m-%d %H:%M:%S")}',
                state_json = '{state_json_b64}',
                last_intent = '{last_intent}',
                last_filters_json = '{last_filters_escaped}',
                last_result_metadata_json = '{last_metadata_escaped}',
                is_active = true
            WHEN NOT MATCHED THEN INSERT (
                conversation_id, updated_at, expires_at, state_json,
                last_intent, last_filters_json, last_result_metadata_json, is_active
            ) VALUES (
                '{conversation_id}',
                TIMESTAMP '{now.strftime("%Y-%m-%d %H:%M:%S")}',
                TIMESTAMP '{expires.strftime("%Y-%m-%d %H:%M:%S")}',
                '{state_json_b64}',
                '{last_intent}',
                '{last_filters_escaped}',
                '{last_metadata_escaped}',
                true
            )
            """
            result = self._sql_executor.execute(merge_sql)
            if hasattr(result, "error") and result.error:
                logger.warning("Delta persist failed: %s", result.error[:200])
        except Exception as e:
            logger.warning("Delta persist error (falling back to memory): %s", str(e)[:200])

    def _load_from_delta(self, conversation_id: str) -> Optional[BusinessContext]:
        """Load context from Delta table."""
        try:
            query = f"""
            SELECT state_json
            FROM {self._table_name}
            WHERE conversation_id = '{conversation_id}'
              AND is_active = true
              AND expires_at > CURRENT_TIMESTAMP()
            ORDER BY updated_at DESC
            LIMIT 1
            """
            result = self._sql_executor.execute(query)
            if hasattr(result, "error") and result.error:
                return None
            if hasattr(result, "rows") and result.rows:
                row = result.rows[0]
                state_json = row.get("state_json") if isinstance(row, dict) else row[0]
                if state_json:
                    # Decode base64 back to JSON string
                    try:
                        decoded = base64.b64decode(state_json).decode()
                    except Exception:
                        decoded = state_json  # Fallback: try raw JSON
                    return deserialize_business_context(decoded)
        except Exception as e:
            logger.warning("Delta load error: %s", str(e)[:100])
        return None

    def _mark_inactive_in_delta(self, conversation_id: str) -> None:
        """Mark a conversation as inactive in Delta."""
        try:
            sql = f"""
            UPDATE {self._table_name}
            SET is_active = false
            WHERE conversation_id = '{conversation_id}'
            """
            self._sql_executor.execute(sql)
        except Exception as e:
            logger.warning("Delta mark-inactive error: %s", str(e)[:100])

    def cleanup_expired_sessions(self) -> int:
        """Mark expired sessions as inactive in Delta. Returns count affected."""
        if not self._delta_available:
            return 0
        try:
            sql = f"""
            UPDATE {self._table_name}
            SET is_active = false
            WHERE is_active = true AND expires_at < CURRENT_TIMESTAMP()
            """
            result = self._sql_executor.execute(sql)
            # Try to get affected count
            if hasattr(result, "rows") and result.rows:
                return len(result.rows)
            return 0
        except Exception as e:
            logger.warning("Delta cleanup error: %s", str(e)[:100])
            return 0

    @property
    def is_delta_available(self) -> bool:
        """Check if Delta persistence is active."""
        return self._delta_available
