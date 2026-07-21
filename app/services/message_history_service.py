"""Message history service for TransparencE conversation persistence.

Orchestrates owner-scoped message writes and history reads:
  - Builds safe response_payload_json from genie_result dicts
  - Enforces payload and text size limits
  - Validates conversation state before writes
  - Handles persistence failures explicitly
  - Never stores internal identifiers, credentials, or raw traces

Security contract:
  MUST NOT be stored in response_payload_json:
    - owner_user_id_hash
    - genie_conversation_id / genie_message_id
    - process-local conversation keys
    - download_key / export_id / export_status / export_mode
    - generated_sql
    - genie_thought_description
    - raw exception stack traces

H1 -- TransparencE Conversation History Persistence
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.services.message_repository import (
    MESSAGE_TEXT_MAX_LEN,
    MAX_PAGE_SIZE,
    PAYLOAD_JSON_MAX_BYTES,
    MessageRecord,
    MessageRepository,
    MessageRepositoryError,
    MessageRepositoryUnavailableError,
    MessageSequenceConflictError,
)
from app.services.conversation_repository import ConversationStatus

log = logging.getLogger(__name__)

# Maximum table rows stored in history payload.
# Matches the frontend display_row_limit default (pipeline default: 100).
HISTORY_MAX_TABLE_ROWS: int = 100

# Maximum suggested questions stored per message.
HISTORY_MAX_SUGGESTED_QUESTIONS: int = 10

# Maximum retry attempts on MessageSequenceConflictError.
_MAX_SEQ_RETRIES: int = 2


# =============================================================================
# PAYLOAD BUILDING
# =============================================================================


# Fields from genie_result that are FORBIDDEN in stored payloads.
_FORBIDDEN_PAYLOAD_FIELDS = frozenset({
    "genie_conversation_id",
    "genie_message_id",
    "genie_thought_description",
    "download_key",
    "export_id",
    "export_status",
    "export_mode",
    "export_row_count",
    "generated_sql",
    "fallback_recommended",
    "shape_retry_exhausted",
    "debug_info",
})


def build_response_payload(genie_result: Dict[str, Any]) -> Optional[str]:
    """Build a JSON payload from a Genie pipeline result dict.

    Returns None if genie_result is empty or has no meaningful content.
    Returns a JSON string if content is present.
    Raises MessageRepositoryError if the payload exceeds PAYLOAD_JSON_MAX_BYTES.

    Excluded fields (security):
      - genie_conversation_id, genie_message_id (internal Genie IDs)
      - download_key, export_id, export_status, export_mode (ephemeral)
      - generated_sql (SQL exposure)
      - genie_thought_description (debug-only)
      - fallback_recommended, shape_retry_exhausted (internal routing flags)
      - debug_info (internal)
      - owner_user_id_hash is never in genie_result but excluded for safety

    Table rows are capped at HISTORY_MAX_TABLE_ROWS.
    Suggested questions are capped at HISTORY_MAX_SUGGESTED_QUESTIONS.
    """
    if not genie_result:
        return None

    payload: Dict[str, Any] = {}

    # Scalar fields
    for field in ("status", "source", "query_description"):
        val = genie_result.get(field)
        if val is not None:
            payload[field] = val

    # Boolean flags
    payload["is_table"] = bool(genie_result.get("is_table", False))
    payload["has_visualization"] = bool(genie_result.get("has_visualization", False))

    # Row count
    row_count = genie_result.get("row_count", 0)
    payload["row_count"] = int(row_count) if row_count is not None else 0

    # Table data — cap rows
    table_data = genie_result.get("table_data")
    if table_data and isinstance(table_data, dict):
        headers = table_data.get("headers", [])
        rows = table_data.get("rows", [])
        if headers:
            payload["table_data"] = {
                "headers": headers,
                "rows": rows[:HISTORY_MAX_TABLE_ROWS],
            }

    # Suggested questions — cap count
    sq = genie_result.get("suggested_questions")
    if sq and isinstance(sq, list) and len(sq) > 0:
        payload["suggested_questions"] = [
            str(q) for q in sq[:HISTORY_MAX_SUGGESTED_QUESTIONS]
        ]

    # Computed chart data
    ccd = genie_result.get("computed_chart_data")
    if ccd and isinstance(ccd, dict):
        payload["computed_chart_data"] = ccd

    # Computed metrics
    cm = genie_result.get("computed_metrics")
    if cm and isinstance(cm, dict):
        payload["computed_metrics"] = cm

    try:
        json_str = json.dumps(payload, default=str, separators=(",", ":"))
    except Exception as exc:
        raise MessageRepositoryError(
            "Failed to serialize response payload to JSON"
        ) from exc

    byte_size = len(json_str.encode("utf-8"))
    if byte_size > PAYLOAD_JSON_MAX_BYTES:
        # Attempt to reduce by dropping table rows
        payload.pop("table_data", None)
        payload.pop("computed_chart_data", None)
        payload.pop("computed_metrics", None)
        try:
            json_str = json.dumps(payload, default=str, separators=(",", ":"))
        except Exception as exc:
            raise MessageRepositoryError(
                "Failed to serialize reduced response payload to JSON"
            ) from exc
        byte_size = len(json_str.encode("utf-8"))
        if byte_size > PAYLOAD_JSON_MAX_BYTES:
            raise MessageRepositoryError(
                f"Response payload still exceeds {PAYLOAD_JSON_MAX_BYTES} bytes after reduction"
            )
        log.warning(
            "history_service: payload exceeded size limit; table_data and chart data were dropped"
        )

    return json_str


def parse_response_payload(json_str: Optional[str]) -> Dict[str, Any]:
    """Parse stored response_payload_json back to a dict.

    Returns {} on None or parse failure (safe for callers to handle).
    """
    if not json_str:
        return {}
    try:
        result = json.loads(json_str)
        if not isinstance(result, dict):
            return {}
        return result
    except Exception:
        log.warning("history_service: failed to parse response_payload_json")
        return {}


# =============================================================================
# CONVERSATION STATE GUARD
# =============================================================================


def _is_conversation_active(
    conversation_record: Any,  # ConversationRecord or None
) -> bool:
    """Return True only when the conversation record is ACTIVE."""
    if conversation_record is None:
        return False
    status = getattr(conversation_record, "status", None)
    return status == ConversationStatus.ACTIVE


# =============================================================================
# PERSISTENCE OPERATIONS
# =============================================================================


class MessageHistoryPersistenceError(Exception):
    """Raised when message persistence fails after retries.

    This is a non-fatal error — the caller should log it and continue
    returning the chat response to the user.  The error must NOT be
    silently swallowed; callers must record it in logs.
    """

    def __init__(self, msg: str, cause: Optional[Exception] = None) -> None:
        super().__init__(msg)
        self.cause = cause


def persist_user_message(
    repo: MessageRepository,
    owner_user_id_hash: str,
    frontend_conversation_id: str,
    message_text: str,
    conversation_record: Any,  # ConversationRecord or None
    message_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[MessageRecord]:
    """Persist a user message after ownership and state validation.

    Validation:
      - conversation_record must be ACTIVE.
      - message_text is truncated to MESSAGE_TEXT_MAX_LEN.

    Raises MessageHistoryPersistenceError when persistence fails after retries.
    Returns None when the conversation is inactive (caller logs, not an error).
    """
    if not _is_conversation_active(conversation_record):
        log.info(
            "history_service: skipping user-message persist — conversation is not ACTIVE "
            "(status=%s)",
            getattr(conversation_record, "status", "None"),
        )
        return None

    text = message_text[:MESSAGE_TEXT_MAX_LEN]

    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_SEQ_RETRIES + 2):
        try:
            record = repo.append_message(
                owner_user_id_hash=owner_user_id_hash,
                frontend_conversation_id=frontend_conversation_id,
                role="user",
                message_text=text,
                response_payload_json=None,
                message_id=message_id,
                now=now,
            )
            return record
        except MessageSequenceConflictError as exc:
            last_exc = exc
            log.warning(
                "history_service: sequence conflict on user message (attempt %d/%d)",
                attempt,
                _MAX_SEQ_RETRIES + 1,
            )
            # Retry: repository will re-read MAX(sequence) on next call
            continue
        except MessageRepositoryError as exc:
            raise MessageHistoryPersistenceError(
                "Failed to persist user message", cause=exc
            ) from exc

    raise MessageHistoryPersistenceError(
        f"Sequence conflict persisted after {_MAX_SEQ_RETRIES + 1} attempts",
        cause=last_exc,
    )


def persist_assistant_response(
    repo: MessageRepository,
    owner_user_id_hash: str,
    frontend_conversation_id: str,
    message_text: str,
    genie_result: Dict[str, Any],
    conversation_record: Any,  # ConversationRecord or None
    message_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[MessageRecord]:
    """Persist an assistant response after ownership and state validation.

    Validation:
      - conversation_record must be ACTIVE.
      - Payload is built from genie_result with forbidden fields stripped.
      - message_text is truncated to MESSAGE_TEXT_MAX_LEN.

    Raises MessageHistoryPersistenceError when persistence fails after retries.
    Returns None when the conversation is inactive.
    """
    if not _is_conversation_active(conversation_record):
        log.info(
            "history_service: skipping assistant-response persist — conversation not ACTIVE"
        )
        return None

    text = message_text[:MESSAGE_TEXT_MAX_LEN]

    # Build payload — may raise MessageRepositoryError on size violations
    try:
        payload_json = build_response_payload(genie_result)
    except MessageRepositoryError as exc:
        raise MessageHistoryPersistenceError(
            "Failed to build response payload for history", cause=exc
        ) from exc

    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_SEQ_RETRIES + 2):
        try:
            record = repo.append_message(
                owner_user_id_hash=owner_user_id_hash,
                frontend_conversation_id=frontend_conversation_id,
                role="assistant",
                message_text=text,
                response_payload_json=payload_json,
                message_id=message_id,
                now=now,
            )
            return record
        except MessageSequenceConflictError as exc:
            last_exc = exc
            log.warning(
                "history_service: sequence conflict on assistant response (attempt %d/%d)",
                attempt,
                _MAX_SEQ_RETRIES + 1,
            )
            continue
        except MessageRepositoryError as exc:
            raise MessageHistoryPersistenceError(
                "Failed to persist assistant response", cause=exc
            ) from exc

    raise MessageHistoryPersistenceError(
        f"Sequence conflict persisted after {_MAX_SEQ_RETRIES + 1} attempts",
        cause=last_exc,
    )


def deactivate_conversation_messages(
    repo: MessageRepository,
    owner_user_id_hash: str,
    frontend_conversation_id: str,
    now: Optional[datetime] = None,
) -> int:
    """Deactivate messages when a conversation is reset or expired.

    Returns count of deactivated rows.  Logs errors without raising so
    the conversation reset flow is not blocked by history failures.
    """
    try:
        count = repo.deactivate_messages(
            owner_user_id_hash=owner_user_id_hash,
            frontend_conversation_id=frontend_conversation_id,
            now=now,
        )
        log.info(
            "history_service: deactivated %d messages for frontend_id=%s",
            count,
            frontend_conversation_id[:8] + "...",
        )
        return count
    except Exception as exc:  # noqa: BLE001
        # Non-blocking: log and swallow all errors so reset flow is never blocked.
        log.error(
            "history_service: failed to deactivate messages: %s",
            str(exc)[:200],
        )
        return 0
