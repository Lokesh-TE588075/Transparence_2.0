"""Conversation Manager - Delta-backed chat history.

Uses Databricks SDK Statement Execution API for reads/writes.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

from app.config import settings

logger = logging.getLogger(__name__)


class ConversationManager:
    """Manages conversation and message persistence in Delta tables."""

    def __init__(self):
        self._warehouse_id = settings.DATABRICKS_SQL_WAREHOUSE_PATH.split("/")[-1]
        self._client = WorkspaceClient()
        self._conversations_table = settings.CONVERSATIONS_TABLE_NAME
        self._messages_table = settings.MESSAGES_TABLE_NAME

    def _execute(self, sql: str) -> List[List]:
        """Execute SQL and return rows."""
        try:
            response = self._client.statement_execution.execute_statement(
                statement=sql,
                warehouse_id=self._warehouse_id,
                wait_timeout="30s",
            )
            if response.status and response.status.state == StatementState.FAILED:
                error = response.status.error.message if response.status.error else "Unknown"
                logger.error("SQL failed: %s | Query: %s", error, sql[:100])
                return []
            if response.result and response.result.data_array:
                return response.result.data_array
            return []
        except Exception as e:
            logger.error("ConversationManager SQL error: %s", str(e)[:200])
            return []

    def _execute_write(self, sql: str) -> bool:
        """Execute a write SQL (INSERT/UPDATE). Returns success."""
        try:
            response = self._client.statement_execution.execute_statement(
                statement=sql,
                warehouse_id=self._warehouse_id,
                wait_timeout="30s",
            )
            if response.status and response.status.state == StatementState.FAILED:
                error = response.status.error.message if response.status.error else "Unknown"
                logger.error("Write failed: %s | Query: %s", error, sql[:100])
                return False
            return True
        except Exception as e:
            logger.error("ConversationManager write error: %s", str(e)[:200])
            return False

    def create_conversation(self, user_id: str, title: str = None) -> str:
        """Create a new conversation. Returns conversation_id."""
        conv_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        title_val = f"'{self._escape(title)}'" if title else "NULL"

        sql = f"""INSERT INTO {self._conversations_table}
            (conversation_id, user_id, title, created_at, last_active_at, message_count, is_archived)
            VALUES ('{conv_id}', '{self._escape(user_id)}', {title_val}, '{now}', '{now}', 0, FALSE)"""

        self._execute_write(sql)
        return conv_id

    def get_conversation(self, conversation_id: str) -> Optional[Dict]:
        """Get conversation metadata."""
        sql = f"""SELECT conversation_id, user_id, title, created_at, last_active_at, message_count, is_archived
            FROM {self._conversations_table}
            WHERE conversation_id = '{self._escape(conversation_id)}'"""
        rows = self._execute(sql)
        if not rows:
            return None
        r = rows[0]
        return {"conversation_id": r[0], "user_id": r[1], "title": r[2],
                "created_at": r[3], "last_active_at": r[4], "message_count": r[5], "is_archived": r[6]}

    def list_conversations(self, user_id: str, limit: int = 20, include_archived: bool = False) -> List[Dict]:
        """List recent conversations for a user."""
        archive_filter = "" if include_archived else "AND is_archived = FALSE"
        sql = f"""SELECT conversation_id, title, last_active_at, message_count
            FROM {self._conversations_table}
            WHERE user_id = '{self._escape(user_id)}' {archive_filter}
            ORDER BY last_active_at DESC LIMIT {limit}"""
        rows = self._execute(sql)
        return [{"conversation_id": r[0], "title": r[1], "last_active_at": r[2], "message_count": r[3]} for r in rows]

    def update_title(self, conversation_id: str, title: str):
        """Update conversation title."""
        sql = f"""UPDATE {self._conversations_table}
            SET title = '{self._escape(title)}'
            WHERE conversation_id = '{self._escape(conversation_id)}'"""
        self._execute_write(sql)

    def archive_conversation(self, conversation_id: str):
        """Soft-delete a conversation."""
        sql = f"""UPDATE {self._conversations_table}
            SET is_archived = TRUE
            WHERE conversation_id = '{self._escape(conversation_id)}'"""
        self._execute_write(sql)

    def add_message(self, conversation_id: str, role: str, content: str,
                    sql_generated: str = None, sql_result_summary: str = None,
                    row_count: int = None, execution_time_ms: int = None,
                    intent: str = None) -> str:
        """Add a message to a conversation."""
        msg_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        sql_gen = f"'{self._escape(sql_generated)}'" if sql_generated else "NULL"
        sql_sum = f"'{self._escape(sql_result_summary[:200])}'" if sql_result_summary else "NULL"
        rc = str(row_count) if row_count is not None else "NULL"
        et = str(execution_time_ms) if execution_time_ms is not None else "NULL"
        intent_val = f"'{self._escape(intent)}'" if intent else "NULL"

        sql = f"""INSERT INTO {self._messages_table}
            (message_id, conversation_id, role, content, sql_generated, sql_result_summary,
             row_count, execution_time_ms, intent, created_at)
            VALUES ('{msg_id}', '{self._escape(conversation_id)}', '{role}',
                    '{self._escape(content[:2000])}', {sql_gen}, {sql_sum},
                    {rc}, {et}, {intent_val}, '{now}')"""

        self._execute_write(sql)

        # Update conversation
        update_sql = f"""UPDATE {self._conversations_table}
            SET last_active_at = '{now}', message_count = message_count + 1
            WHERE conversation_id = '{self._escape(conversation_id)}'"""
        self._execute_write(update_sql)

        return msg_id

    def get_history(self, conversation_id: str, limit: int = 20) -> List[Dict]:
        """Get message history for a conversation."""
        sql = f"""SELECT role, content, sql_generated, row_count, created_at
            FROM {self._messages_table}
            WHERE conversation_id = '{self._escape(conversation_id)}'
            ORDER BY created_at ASC LIMIT {limit}"""
        rows = self._execute(sql)
        return [{"role": r[0], "content": r[1], "sql_generated": r[2],
                 "row_count": r[3], "created_at": r[4]} for r in rows]

    def get_llm_context(self, conversation_id: str, max_turns: int = 4) -> List[Dict[str, str]]:
        """Get recent messages in OpenAI format for LLM context."""
        sql = f"""SELECT role, content FROM {self._messages_table}
            WHERE conversation_id = '{self._escape(conversation_id)}'
            ORDER BY created_at DESC LIMIT {max_turns * 2}"""
        rows = self._execute(sql)
        messages = [{"role": r[0], "content": r[1]} for r in reversed(rows)]
        return messages

    def generate_title(self, first_message: str) -> str:
        """Generate a short title from the first message."""
        title = first_message[:50].strip()
        if len(first_message) > 50:
            title = title[:47] + "..."
        return title

    @staticmethod
    def _escape(text: str) -> str:
        """Escape single quotes for SQL."""
        if text is None:
            return ""
        return text.replace("'", "''").replace("\\", "\\\\")
