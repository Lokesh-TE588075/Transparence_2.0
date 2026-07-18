"""Databricks SQL Execution Service.

Uses the Databricks SDK Statement Execution API for queries.
This approach works natively with Databricks Apps OAuth credentials
(no separate CAN_USE permission needed on the warehouse).
"""

import logging
import time
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import pandas as pd
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

from app.config import settings

logger = logging.getLogger(__name__)

# Constants
API_ROW_LIMIT = 500
EXPORT_ROW_LIMIT = 100_000


@dataclass
class QueryResult:
    """Result of a SQL query execution."""
    headers: List[str] = field(default_factory=list)
    rows: List[List] = field(default_factory=list)
    row_count: int = 0
    total_row_count: int = 0
    execution_time_ms: int = 0
    truncated: bool = False
    error: Optional[str] = None


class SQLService:
    """Manages SQL query execution via Databricks SDK Statement Execution API."""

    def __init__(self):
        """Initialize SQL service with workspace client."""
        self._warehouse_id = settings.DATABRICKS_SQL_WAREHOUSE_PATH.split("/")[-1]
        self._client = WorkspaceClient()

        if not self._warehouse_id:
            logger.warning("DATABRICKS_SQL_WAREHOUSE_PATH not configured.")

    def execute_query(self, sql: str, row_limit: int = API_ROW_LIMIT,
                      timeout_seconds: int = None) -> QueryResult:
        """Execute a SQL query and return results.

        Args:
            sql: SQL query string (must be read-only).
            row_limit: Max rows to return.
            timeout_seconds: Query timeout.

        Returns:
            QueryResult with headers, rows, and metadata.
        """
        timeout = timeout_seconds or settings.SQL_QUERY_TIMEOUT_SECONDS
        self._enforce_read_only(sql)

        start_time = time.time()

        try:
            # Statement Execution API: wait_timeout must be 0 or 5-50s
            wait = min(timeout, 50)
            response = self._client.statement_execution.execute_statement(
                statement=sql,
                warehouse_id=self._warehouse_id,
                wait_timeout=f"{wait}s",
                row_limit=row_limit + 1,  # +1 to detect truncation
                byte_limit=10_000_000,
            )

            # If still running after initial wait, poll until complete
            import time as _time
            poll_start = _time.time()
            while (response.status and 
                   response.status.state in (StatementState.PENDING, StatementState.RUNNING) and
                   (_time.time() - poll_start) < timeout):
                _time.sleep(2)
                response = self._client.statement_execution.get_statement(response.id)

            elapsed_ms = int((time.time() - start_time) * 1000)

            if response.status and response.status.state == StatementState.FAILED:
                error_msg = response.status.error.message if response.status.error else "Query failed"
                return QueryResult(error=error_msg, execution_time_ms=elapsed_ms)

            if response.status and response.status.state == StatementState.CANCELED:
                return QueryResult(error="Query was cancelled (timeout)", execution_time_ms=elapsed_ms)

            # Extract results
            headers = []
            rows = []

            if response.manifest and response.manifest.schema and response.manifest.schema.columns:
                headers = [col.name for col in response.manifest.schema.columns]

            if response.result and response.result.data_array:
                rows = response.result.data_array

            # Detect truncation
            truncated = len(rows) > row_limit
            if truncated:
                total_row_count = len(rows)  # At least this many
                rows = rows[:row_limit]
            else:
                total_row_count = len(rows)

            # Try to get exact count from manifest
            if response.manifest and response.manifest.total_row_count:
                total_row_count = response.manifest.total_row_count

            return QueryResult(
                headers=headers,
                rows=rows,
                row_count=len(rows),
                total_row_count=total_row_count,
                execution_time_ms=elapsed_ms,
                truncated=truncated,
            )

        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            error_msg = str(e)
            logger.error("SQL execution error: %s", error_msg[:200])
            return QueryResult(error=error_msg, execution_time_ms=elapsed_ms)

    def execute_to_dataframe(self, sql: str, row_limit: int = EXPORT_ROW_LIMIT) -> Tuple[pd.DataFrame, int]:
        """Execute query and return as DataFrame (for export)."""
        result = self.execute_query(sql, row_limit=row_limit)
        if result.error:
            raise RuntimeError(f"Query failed: {result.error}")
        df = pd.DataFrame(result.rows, columns=result.headers)
        return df, result.total_row_count

    def test_connection(self) -> dict:
        """Test the SQL warehouse connection."""
        start = time.time()
        result = self.execute_query("SELECT 1 AS test")
        elapsed = int((time.time() - start) * 1000)
        return {
            "status": "ok" if not result.error else "error",
            "warehouse_id": self._warehouse_id,
            "latency_ms": elapsed,
            "error": result.error,
        }

    @staticmethod
    def _enforce_read_only(sql: str) -> None:
        """Reject non-SELECT SQL statements."""
        # Strip leading SQL comments
        cleaned = sql.strip()
        cleaned = re.sub(r"^(\s*--[^\n]*\n)+", "", cleaned).strip()
        cleaned = re.sub(r"^/\*.*?\*/\s*", "", cleaned, flags=re.DOTALL).strip()

        # Allow WITH (CTE) + SELECT
        if cleaned.upper().startswith("WITH"):
            return
        # Allow SELECT
        if cleaned.upper().startswith("SELECT"):
            return

        # Check for dangerous operations
        dangerous = ["INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE", "MERGE"]
        first_word = cleaned.split()[0].upper() if cleaned.split() else ""
        if first_word in dangerous:
            raise ValueError(
                f"Only SELECT queries are allowed. Detected unsafe operation in: "
                f"{cleaned[:80]}..."
            )

        raise ValueError(
            f"Only SELECT queries are allowed. Query does not start with SELECT or WITH: "
            f"{cleaned[:80]}..."
        )
