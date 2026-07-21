"""Lakebase (PostgreSQL) message repository adapter.

Implements ``MessageRepository`` using parameterized SQL against
Databricks Lakebase.  Design mirrors LakebaseConversationRepository:

  - No imports of psycopg or any database driver at module level.
  - No connections opened on import or construction.
  - No environment-variable reads.
  - All database access through an injected connection_provider.
  - Fully testable with in-memory fakes.

Table: transparence_state.app_conversation_message
  (resolved without schema prefix because search_path=transparence_state,public
   is enforced by LakebaseConnectionProvider at connection time)

H1 -- TransparencE Conversation History Persistence
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, ContextManager, List, Optional, Protocol, Tuple, runtime_checkable

from app.services.message_repository import (
    MAX_PAGE_SIZE,
    MESSAGE_TEXT_MAX_LEN,
    PAYLOAD_JSON_MAX_BYTES,
    MessageRecord,
    MessageRepositoryError,
    MessageRepositoryUnavailableError,
    MessageSequenceConflictError,
    MessageValidationError,
)

# =============================================================================
# CONNECTION ABSTRACTIONS (mirrors lakebase_conversation_repository.py)
# =============================================================================


@runtime_checkable
class CursorLike(Protocol):
    def execute(self, sql: str, parameters: Any = None) -> None: ...
    def fetchone(self) -> Optional[Tuple[Any, ...]]: ...
    def fetchall(self) -> List[Tuple[Any, ...]]: ...
    @property
    def rowcount(self) -> int: ...
    def __enter__(self) -> "CursorLike": ...
    def __exit__(self, *args: Any) -> None: ...


@runtime_checkable
class ConnectionLike(Protocol):
    def cursor(self) -> CursorLike: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


ConnectionContextProvider = Callable[[], ContextManager[ConnectionLike]]


# =============================================================================
# TABLE AND COLUMN CONTRACT
# =============================================================================

_TABLE_NAME = "app_conversation_message"

# Positional column order — must match 002_create_conversation_message.sql
_MSG_COLUMNS: Tuple[str, ...] = (
    "message_id",               # 0
    "owner_user_id_hash",        # 1
    "frontend_conversation_id", # 2
    "message_sequence",         # 3
    "role",                     # 4
    "message_text",             # 5
    "response_payload_json",    # 6
    "is_active",                # 7
    "created_at",               # 8
    "updated_at",               # 9
)

_COLUMNS_CSV = ", ".join(_MSG_COLUMNS)
_RETURNING = f"RETURNING {_COLUMNS_CSV}"

_UNIQUE_VIOLATION_SQLSTATE = "23505"


# =============================================================================
# UTC HELPERS
# =============================================================================


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_now(now: Optional[datetime]) -> datetime:
    if now is None:
        return _utcnow()
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError(f"now must be timezone-aware; got naive {now!r}")
    return now


# =============================================================================
# ROW MAPPING
# =============================================================================


def _row_to_record(row: Optional[Tuple[Any, ...]]) -> Optional[MessageRecord]:
    if row is None:
        return None
    try:
        if len(row) != len(_MSG_COLUMNS):
            raise ValueError(f"Expected {len(_MSG_COLUMNS)} columns, got {len(row)}")
        return MessageRecord(
            message_id=str(row[0]),
            owner_user_id_hash=str(row[1]),
            frontend_conversation_id=str(row[2]),
            message_sequence=int(row[3]),
            role=str(row[4]),
            message_text=str(row[5]),
            response_payload_json=row[6] if row[6] is not None else None,
            is_active=bool(row[7]),
            created_at=row[8],
            updated_at=row[9],
        )
    except MessageRepositoryError:
        raise
    except Exception as exc:
        raise MessageRepositoryError("Failed to map database row to MessageRecord") from exc


# =============================================================================
# ERROR HELPERS
# =============================================================================


def _get_sqlstate(exc: BaseException) -> Optional[str]:
    state = getattr(exc, "sqlstate", None)
    if state:
        return str(state)
    code = getattr(exc, "pgcode", None)
    if code:
        return str(code)
    return None


def _is_unique_violation(exc: BaseException) -> bool:
    return _get_sqlstate(exc) == _UNIQUE_VIOLATION_SQLSTATE


def _wrap_unavailable(exc: BaseException) -> MessageRepositoryUnavailableError:
    err = MessageRepositoryUnavailableError(
        "Message repository is temporarily unavailable"
    )
    err.__cause__ = exc
    return err


# =============================================================================
# LAKEBASE MESSAGE REPOSITORY
# =============================================================================


class LakebaseMessageRepository:
    """PostgreSQL-backed message repository using injected connections.

    Uses parameterized SQL via the injected connection_provider.
    No connection is opened during construction.
    """

    def __init__(self, connection_provider: ConnectionContextProvider) -> None:
        self._cp = connection_provider

    # -------------------------------------------------------------------------
    # append_message
    # -------------------------------------------------------------------------

    def append_message(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
        role: str,
        message_text: str,
        response_payload_json: Optional[str] = None,
        message_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> MessageRecord:
        """Append a message, assigning the next sequence number atomically."""
        # --- validation ---
        if not owner_user_id_hash:
            raise MessageValidationError("owner_user_id_hash must not be empty")
        if not frontend_conversation_id:
            raise MessageValidationError("frontend_conversation_id must not be empty")
        if role not in ("user", "assistant"):
            raise MessageValidationError("role must be 'user' or 'assistant'")
        if not message_text:
            raise MessageValidationError("message_text must not be empty")
        if len(message_text) > MESSAGE_TEXT_MAX_LEN:
            raise MessageValidationError(
                f"message_text exceeds {MESSAGE_TEXT_MAX_LEN} characters"
            )
        if response_payload_json is not None:
            if len(response_payload_json.encode("utf-8")) > PAYLOAD_JSON_MAX_BYTES:
                raise MessageValidationError(
                    f"response_payload_json exceeds {PAYLOAD_JSON_MAX_BYTES} bytes"
                )

        ts = _validate_now(now)
        mid = message_id or str(uuid.uuid4())

        try:
            with self._cp() as conn:
                with conn.cursor() as cur:
                    # Compute next sequence atomically within the transaction.
                    # The unique constraint on (owner, frontend_id, sequence)
                    # will catch any concurrent-write races.
                    cur.execute(
                        """
                        SELECT COALESCE(MAX(message_sequence), 0) + 1
                        FROM app_conversation_message
                        WHERE owner_user_id_hash = %s
                          AND frontend_conversation_id = %s
                        """,
                        (owner_user_id_hash, frontend_conversation_id),
                    )
                    row = cur.fetchone()
                    next_seq = row[0] if row else 1

                    cur.execute(
                        f"""
                        INSERT INTO {_TABLE_NAME}
                            ({_COLUMNS_CSV})
                        VALUES
                            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        {_RETURNING}
                        """,
                        (
                            mid,
                            owner_user_id_hash,
                            frontend_conversation_id,
                            next_seq,
                            role,
                            message_text,
                            response_payload_json,
                            True,   # is_active
                            ts,
                            ts,     # updated_at = created_at on insert
                        ),
                    )
                    inserted = cur.fetchone()
                    conn.commit()
                    record = _row_to_record(inserted)
                    if record is None:
                        raise MessageRepositoryError(
                            "INSERT did not return a row"
                        )
                    return record

        except MessageRepositoryError:
            raise
        except Exception as exc:
            if _is_unique_violation(exc):
                raise MessageSequenceConflictError(
                    "Sequence conflict: concurrent write on "
                    f"(owner, frontend_id, sequence={next_seq})"
                ) from exc
            raise _wrap_unavailable(exc) from exc

    # -------------------------------------------------------------------------
    # list_messages
    # -------------------------------------------------------------------------

    def list_messages(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[MessageRecord], int]:
        """Return active messages in ascending sequence order (paginated)."""
        ps = min(max(1, page_size), MAX_PAGE_SIZE)
        pg = max(1, page)
        offset = (pg - 1) * ps

        try:
            with self._cp() as conn:
                with conn.cursor() as cur:
                    # Total count of active messages
                    cur.execute(
                        """
                        SELECT COUNT(*)
                        FROM app_conversation_message
                        WHERE owner_user_id_hash = %s
                          AND frontend_conversation_id = %s
                          AND is_active = TRUE
                        """,
                        (owner_user_id_hash, frontend_conversation_id),
                    )
                    count_row = cur.fetchone()
                    total = int(count_row[0]) if count_row else 0

                    if total == 0:
                        return [], 0

                    # Paginated fetch
                    cur.execute(
                        f"""
                        SELECT {_COLUMNS_CSV}
                        FROM {_TABLE_NAME}
                        WHERE owner_user_id_hash = %s
                          AND frontend_conversation_id = %s
                          AND is_active = TRUE
                        ORDER BY message_sequence ASC
                        LIMIT %s OFFSET %s
                        """,
                        (owner_user_id_hash, frontend_conversation_id, ps, offset),
                    )
                    rows = cur.fetchall()
                    records = [r for row in rows if (r := _row_to_record(row)) is not None]
                    return records, total

        except MessageRepositoryError:
            raise
        except Exception as exc:
            raise _wrap_unavailable(exc) from exc

    # -------------------------------------------------------------------------
    # deactivate_messages
    # -------------------------------------------------------------------------

    def deactivate_messages(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
        now: Optional[datetime] = None,
    ) -> int:
        """Set is_active=FALSE for all active messages in this conversation."""
        ts = _validate_now(now)
        try:
            with self._cp() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE app_conversation_message
                        SET is_active = FALSE, updated_at = %s
                        WHERE owner_user_id_hash = %s
                          AND frontend_conversation_id = %s
                          AND is_active = TRUE
                        """,
                        (ts, owner_user_id_hash, frontend_conversation_id),
                    )
                    count = cur.rowcount
                    conn.commit()
                    return count
        except MessageRepositoryError:
            raise
        except Exception as exc:
            raise _wrap_unavailable(exc) from exc
