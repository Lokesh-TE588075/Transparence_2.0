"""Audit logging and feedback service.

Uses Databricks SDK Statement Execution API.
"""

import csv
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

from app.config import settings

logger = logging.getLogger(__name__)


class AuditService:
    """Handles query audit logging, user feedback, and CSV exports."""

    def __init__(self):
        self._warehouse_id = settings.DATABRICKS_SQL_WAREHOUSE_PATH.split("/")[-1]
        self._client = WorkspaceClient()
        self._feedback_table = settings.FEEDBACK_TABLE_NAME
        self._audit_table = settings.AUDIT_TABLE_NAME
        self._export_path = settings.EXPORT_VOLUME_PATH or "/tmp/chatbot_exports"
        os.makedirs(self._export_path, exist_ok=True)

    def _execute_write(self, sql: str) -> bool:
        """Execute a write SQL statement. Returns success."""
        try:
            response = self._client.statement_execution.execute_statement(
                statement=sql,
                warehouse_id=self._warehouse_id,
                wait_timeout="30s",
            )
            if response.status and response.status.state == StatementState.FAILED:
                error = response.status.error.message if response.status.error else "Unknown"
                logger.error("Audit write failed: %s", error[:200])
                return False
            return True
        except Exception as e:
            logger.error("Audit service error: %s", str(e)[:200])
            return False

    def _execute_read(self, sql: str) -> List[List]:
        """Execute a read SQL and return rows."""
        try:
            response = self._client.statement_execution.execute_statement(
                statement=sql,
                warehouse_id=self._warehouse_id,
                wait_timeout="30s",
            )
            if response.result and response.result.data_array:
                return response.result.data_array
            return []
        except Exception as e:
            logger.error("Audit read error: %s", str(e)[:200])
            return []

    @staticmethod
    def _escape(text: str) -> str:
        """Escape single quotes for SQL."""
        if text is None:
            return ""
        return str(text).replace("'", "''").replace("\\", "\\\\")

    def log_query(self, user_id: str = None, user_query: str = None,
                  intent: str = None, sql_generated: str = None,
                  sql_validated: bool = None, validation_issues: str = None,
                  execution_status: str = None, error_message: str = None,
                  row_count: int = None, execution_time_ms: int = None,
                  llm_time_ms: int = None, total_time_ms: int = None,
                  was_repaired: bool = False, conversation_id: str = None) -> str:
        """Log a query audit record. Never raises - fails silently."""
        try:
            audit_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            def val(v, is_str=True):
                if v is None:
                    return "NULL"
                if is_str:
                    return f"'{self._escape(str(v)[:500])}'"
                return str(v)

            sql = f"""INSERT INTO {self._audit_table}
                (audit_id, conversation_id, user_id, user_query, intent,
                 sql_generated, sql_validated, validation_issues,
                 execution_status, error_message, row_count,
                 execution_time_ms, llm_time_ms, total_time_ms,
                 was_repaired, created_at)
                VALUES ('{audit_id}', {val(conversation_id)}, {val(user_id)},
                        {val(user_query)}, {val(intent)},
                        {val(sql_generated)}, {val(sql_validated, False) if sql_validated is not None else 'NULL'},
                        {val(validation_issues)},
                        {val(execution_status)}, {val(error_message)},
                        {val(row_count, False) if row_count is not None else 'NULL'},
                        {val(execution_time_ms, False) if execution_time_ms is not None else 'NULL'},
                        {val(llm_time_ms, False) if llm_time_ms is not None else 'NULL'},
                        {val(total_time_ms, False) if total_time_ms is not None else 'NULL'},
                        {str(was_repaired).upper()}, '{now}')"""

            self._execute_write(sql)
            return audit_id
        except Exception as e:
            logger.error("Audit log failed (non-fatal): %s", str(e)[:100])
            return ""

    def submit_feedback(self, user_id: str, rating: int,
                        conversation_id: str = None, message_id: str = None,
                        comment: str = None, user_query: str = None,
                        sql_generated: str = None) -> str:
        """Submit user feedback."""
        feedback_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        def val(v):
            return f"'{self._escape(str(v))}'" if v else "NULL"

        sql = f"""INSERT INTO {self._feedback_table}
            (feedback_id, conversation_id, message_id, user_id, rating,
             comment, user_query, sql_generated, created_at)
            VALUES ('{feedback_id}', {val(conversation_id)}, {val(message_id)},
                    '{self._escape(user_id)}', {rating},
                    {val(comment)}, {val(user_query)}, {val(sql_generated)}, '{now}')"""

        self._execute_write(sql)
        return feedback_id

    def create_export(self, headers: List[str], rows: List[List],
                      filename_prefix: str = "export") -> str:
        """Create a CSV export file. Returns download_key."""
        download_key = str(uuid.uuid4())
        filename = f"{filename_prefix}_{download_key[:8]}.csv"
        filepath = os.path.join(self._export_path, filename)

        try:
            with open(filepath, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                writer.writerows(rows)

            # Write metadata
            meta_path = filepath + ".meta"
            with open(meta_path, "w") as f:
                f.write(download_key)

            return download_key
        except Exception as e:
            logger.error("Export creation failed: %s", str(e)[:100])
            return None

    def update_export(self, download_key: str, headers: List[str], rows: List[List]) -> bool:
        """Update an existing export with full data (called from async thread)."""
        filepath = self.get_export_path(download_key)
        if not filepath:
            logger.warning(f"Cannot update export: key {download_key} not found")
            return False
        try:
            with open(filepath, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                writer.writerows(rows)
            logger.info(f"Export updated: {filepath} ({len(rows)} rows)")
            return True
        except Exception as e:
            logger.error(f"Export update failed: {e}")
            return False

    def get_export_path(self, download_key: str) -> Optional[str]:
        """Get the file path for a download key."""
        for fname in os.listdir(self._export_path):
            if fname.endswith(".meta"):
                meta_path = os.path.join(self._export_path, fname)
                with open(meta_path) as f:
                    if f.read().strip() == download_key:
                        return meta_path.replace(".meta", "")
        return None

    def is_export_ready(self, download_key: str) -> bool:
        """Check if an export file is ready."""
        return self.get_export_path(download_key) is not None
