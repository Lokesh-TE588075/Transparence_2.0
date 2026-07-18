"""Lakebase (PostgreSQL) conversation repository adapter.

This module implements `ConversationRepository` using parameterized SQL against
a PostgreSQL-compatible database (Databricks Lakebase).  It does NOT:

- Import psycopg or any database driver at module level.
- Open connections on import or construction.
- Read environment variables.
- Create connection pools.
- Execute DDL.
- Access the network.

All database access is performed through an **injected connection provider**
that is called per-operation.  This design allows complete unit testing with
in-memory fakes and defers real connection management to the integration layer.

Phase 2B1 -- TransparencE Genie State Persistence
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Collection,
    ContextManager,
    List,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

from app.services.conversation_repository import (
    ConversationAlreadyExistsError,
    ConversationNotFoundError,
    ConversationOwnershipError,
    ConversationRecord,
    ConversationRepositoryError,
    ConversationRepositoryUnavailableError,
    ConversationStatus,
    ConversationVersionConflictError,
)

# =============================================================================
# CONNECTION ABSTRACTIONS (Structural Protocols)
# =============================================================================


@runtime_checkable
class CursorLike(Protocol):
    """Minimal cursor contract required by the repository.

    Supports parameterized execution, row fetching, and context-manager usage.
    """

    def execute(self, sql: str, parameters: Any = None) -> None:
        ...

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        ...

    def fetchall(self) -> List[Tuple[Any, ...]]:
        ...

    @property
    def rowcount(self) -> int:
        ...

    def __enter__(self) -> "CursorLike":
        ...

    def __exit__(self, *args: Any) -> None:
        ...


@runtime_checkable
class ConnectionLike(Protocol):
    """Minimal connection contract required by the repository."""

    def cursor(self) -> CursorLike:
        ...

    def commit(self) -> None:
        ...

    def rollback(self) -> None:
        ...


# Type alias for the injectable connection provider.
# A callable returning a context-manager that yields a ConnectionLike.
ConnectionContextProvider = Callable[[], ContextManager[ConnectionLike]]


# =============================================================================
# TABLE AND COLUMN CONTRACT
# =============================================================================

_TABLE_NAME = "app_conversation"

# Ordered column list used for SELECT and RETURNING clauses.
# This single source of truth prevents drift between operations.
_COLUMNS: Tuple[str, ...] = (
    "conversation_id",
    "owner_user_id_hash",
    "frontend_conversation_id",
    "genie_conversation_id",
    "last_genie_message_id",
    "status",
    "version",
    "created_at",
    "updated_at",
    "last_active_at",
)

_COLUMNS_CSV = ", ".join(_COLUMNS)
_RETURNING_CLAUSE = f"RETURNING {_COLUMNS_CSV}"

# Unique constraint SQLSTATE for PostgreSQL
_UNIQUE_VIOLATION_SQLSTATE = "23505"


# =============================================================================
# UTC HELPER
# =============================================================================


def _utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


def _validate_now(now: Optional[datetime]) -> datetime:
    """Return *now* if provided (must be timezone-aware), else current UTC."""
    if now is None:
        return _utcnow()
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError(f"now must be timezone-aware; got naive {now!r}")
    return now


# =============================================================================
# ROW MAPPING
# =============================================================================


def _row_to_record(row: Optional[Tuple[Any, ...]]) -> Optional[ConversationRecord]:
    """Convert a database row tuple to a ConversationRecord.

    Returns None when row is None.  Raises ConversationRepositoryError on
    malformed data.
    """
    if row is None:
        return None

    try:
        if len(row) != len(_COLUMNS):
            raise ValueError(
                f"Expected {len(_COLUMNS)} columns, got {len(row)}"
            )

        # Map positional columns to record fields
        status_raw = row[5]
        try:
            status = ConversationStatus(status_raw)
        except (ValueError, KeyError):
            raise ValueError(f"Invalid status value: {status_raw!r}")

        return ConversationRecord(
            conversation_id=str(row[0]),
            owner_user_id_hash=str(row[1]),
            frontend_conversation_id=str(row[2]),
            genie_conversation_id=row[3] if row[3] is not None else None,
            last_genie_message_id=row[4] if row[4] is not None else None,
            status=status,
            version=int(row[6]),
            created_at=row[7],
            updated_at=row[8],
            last_active_at=row[9],
        )
    except ConversationRepositoryError:
        raise
    except Exception as exc:
        raise ConversationRepositoryError(
            "Failed to map database row to conversation record"
        ) from exc


def _require_row_to_record(row: Optional[Tuple[Any, ...]]) -> ConversationRecord:
    """Like _row_to_record but raises if the result is None."""
    record = _row_to_record(row)
    if record is None:
        raise ConversationRepositoryError(
            "Expected a row from the database but received None"
        )
    return record


# =============================================================================
# ERROR HELPERS
# =============================================================================


def _get_sqlstate(exc: BaseException) -> Optional[str]:
    """Extract PostgreSQL SQLSTATE from an exception.

    Supports both `exception.sqlstate` and `exception.pgcode` attributes,
    without importing any database-specific exception class.
    """
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate:
        return str(sqlstate)
    pgcode = getattr(exc, "pgcode", None)
    if pgcode:
        return str(pgcode)
    return None


def _is_unique_violation(exc: BaseException) -> bool:
    """Return True if *exc* represents a PostgreSQL unique constraint violation."""
    return _get_sqlstate(exc) == _UNIQUE_VIOLATION_SQLSTATE


def _wrap_unavailable(exc: BaseException) -> ConversationRepositoryUnavailableError:
    """Wrap a database error into a sanitized unavailability error."""
    err = ConversationRepositoryUnavailableError(
        "Conversation repository is temporarily unavailable"
    )
    err.__cause__ = exc
    return err


# =============================================================================
# LAKEBASE CONVERSATION REPOSITORY
# =============================================================================


class LakebaseConversationRepository:
    """PostgreSQL-backed conversation repository using injected connections.

    This class implements the full ConversationRepository Protocol using
    parameterized SQL.  It requires a connection_provider callable that
    yields a ConnectionLike context manager on each call.

    No connection is opened during construction.  Each operation independently
    requests a connection through the provider.
    """

    def __init__(self, connection_provider: ConnectionContextProvider) -> None:
        """Initialize with an injected connection provider.

        No connection is opened.  No environment variables are read.
        """
        self._connection_provider = connection_provider

    # -------------------------------------------------------------------------
    # Lookup Operations
    # -------------------------------------------------------------------------

    def get_by_id(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
    ) -> Optional[ConversationRecord]:
        """Return the record for conversation_id owned by owner_user_id_hash."""
        sql = (
            f"SELECT {_COLUMNS_CSV} FROM {_TABLE_NAME} "
            "WHERE conversation_id = %s AND owner_user_id_hash = %s"
        )
        try:
            with self._connection_provider() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (conversation_id, owner_user_id_hash))
                    row = cur.fetchone()
                    return _row_to_record(row)
        except ConversationRepositoryError:
            raise
        except Exception as exc:
            raise _wrap_unavailable(exc)

    def get_by_frontend_id(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
    ) -> Optional[ConversationRecord]:
        """Return the record for (owner, frontend_id) or None."""
        sql = (
            f"SELECT {_COLUMNS_CSV} FROM {_TABLE_NAME} "
            "WHERE owner_user_id_hash = %s AND frontend_conversation_id = %s"
        )
        try:
            with self._connection_provider() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (owner_user_id_hash, frontend_conversation_id))
                    row = cur.fetchone()
                    return _row_to_record(row)
        except ConversationRepositoryError:
            raise
        except Exception as exc:
            raise _wrap_unavailable(exc)

    # -------------------------------------------------------------------------
    # Creation
    # -------------------------------------------------------------------------

    def create_conversation(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
        *,
        conversation_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        """Create a new conversation or return the existing one idempotently."""
        ts = _validate_now(now)
        cid = conversation_id if conversation_id is not None else str(uuid.uuid4())

        insert_sql = (
            f"INSERT INTO {_TABLE_NAME} "
            "(conversation_id, owner_user_id_hash, frontend_conversation_id, "
            "genie_conversation_id, last_genie_message_id, status, version, "
            "created_at, updated_at, last_active_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (owner_user_id_hash, frontend_conversation_id) "
            f"DO NOTHING {_RETURNING_CLAUSE}"
        )

        select_by_frontend_sql = (
            f"SELECT {_COLUMNS_CSV} FROM {_TABLE_NAME} "
            "WHERE owner_user_id_hash = %s AND frontend_conversation_id = %s"
        )

        select_by_id_sql = (
            f"SELECT {_COLUMNS_CSV} FROM {_TABLE_NAME} "
            "WHERE conversation_id = %s"
        )

        params = (
            cid,
            owner_user_id_hash,
            frontend_conversation_id,
            None,  # genie_conversation_id
            None,  # last_genie_message_id
            ConversationStatus.ACTIVE.value,
            1,     # version
            ts,
            ts,
            ts,
        )

        try:
            with self._connection_provider() as conn:
                with conn.cursor() as cur:
                    try:
                        cur.execute(insert_sql, params)
                    except Exception as insert_exc:
                        if _is_unique_violation(insert_exc):
                            conn.rollback()
                            return self._handle_id_conflict(
                                conn, cur, cid, owner_user_id_hash,
                                frontend_conversation_id, select_by_id_sql,
                                select_by_frontend_sql,
                            )
                        conn.rollback()
                        raise

                    row = cur.fetchone()

                    if row is not None:
                        # Insert succeeded
                        conn.commit()
                        return _require_row_to_record(row)

                    # ON CONFLICT fired: logical duplicate exists
                    cur.execute(
                        select_by_frontend_sql,
                        (owner_user_id_hash, frontend_conversation_id),
                    )
                    existing_row = cur.fetchone()
                    conn.commit()

                    if existing_row is not None:
                        return _require_row_to_record(existing_row)

                    raise ConversationRepositoryError(
                        "Failed to create or retrieve conversation record"
                    )

        except ConversationRepositoryError:
            raise
        except Exception as exc:
            raise _wrap_unavailable(exc)

    def _handle_id_conflict(
        self,
        conn: Any,
        cur: Any,
        conversation_id: str,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
        select_by_id_sql: str,
        select_by_frontend_sql: str,
    ) -> ConversationRecord:
        """Handle a conversation_id PK conflict during create."""
        cur.execute(select_by_id_sql, (conversation_id,))
        conflict_row = cur.fetchone()

        if conflict_row is None:
            raise ConversationRepositoryError(
                "Failed to create conversation record"
            )

        conflict_record = _require_row_to_record(conflict_row)

        if conflict_record.owner_user_id_hash != owner_user_id_hash:
            raise ConversationOwnershipError(
                "Conversation ID is already in use by another owner"
            )

        if conflict_record.frontend_conversation_id == frontend_conversation_id:
            return conflict_record

        raise ConversationAlreadyExistsError(
            "Conversation ID is already associated with a different "
            "logical conversation for this owner"
        )

    # -------------------------------------------------------------------------
    # Mutations (all require expected_version)
    # -------------------------------------------------------------------------

    def bind_genie_conversation(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        genie_conversation_id: str,
        *,
        expected_version: int,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        """Attach a Genie conversation ID to an existing record."""
        if not genie_conversation_id:
            raise ValueError("genie_conversation_id must not be empty")

        ts = _validate_now(now)
        sql = (
            f"UPDATE {_TABLE_NAME} SET "
            "genie_conversation_id = %s, "
            "version = version + 1, "
            "updated_at = %s "
            "WHERE owner_user_id_hash = %s "
            "AND conversation_id = %s "
            f"AND version = %s {_RETURNING_CLAUSE}"
        )
        params = (
            genie_conversation_id, ts,
            owner_user_id_hash, conversation_id, expected_version,
        )
        return self._execute_versioned_update(
            sql, params, owner_user_id_hash, conversation_id, expected_version
        )

    def update_last_genie_message(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        last_genie_message_id: str,
        *,
        expected_version: int,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        """Update the most recent Genie message ID."""
        if not last_genie_message_id:
            raise ValueError("last_genie_message_id must not be empty")

        ts = _validate_now(now)
        sql = (
            f"UPDATE {_TABLE_NAME} SET "
            "last_genie_message_id = %s, "
            "version = version + 1, "
            "updated_at = %s "
            "WHERE owner_user_id_hash = %s "
            "AND conversation_id = %s "
            f"AND version = %s {_RETURNING_CLAUSE}"
        )
        params = (
            last_genie_message_id, ts,
            owner_user_id_hash, conversation_id, expected_version,
        )
        return self._execute_versioned_update(
            sql, params, owner_user_id_hash, conversation_id, expected_version
        )

    def touch(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        *,
        expected_version: int,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        """Update updated_at and last_active_at without changing other fields."""
        ts = _validate_now(now)
        sql = (
            f"UPDATE {_TABLE_NAME} SET "
            "version = version + 1, "
            "updated_at = %s, "
            "last_active_at = %s "
            "WHERE owner_user_id_hash = %s "
            "AND conversation_id = %s "
            f"AND version = %s {_RETURNING_CLAUSE}"
        )
        params = (ts, ts, owner_user_id_hash, conversation_id, expected_version)
        return self._execute_versioned_update(
            sql, params, owner_user_id_hash, conversation_id, expected_version
        )

    def set_status(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        status: ConversationStatus,
        *,
        expected_version: int,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        """Set the lifecycle status of a conversation."""
        ts = _validate_now(now)
        sql = (
            f"UPDATE {_TABLE_NAME} SET "
            "status = %s, "
            "version = version + 1, "
            "updated_at = %s "
            "WHERE owner_user_id_hash = %s "
            "AND conversation_id = %s "
            f"AND version = %s {_RETURNING_CLAUSE}"
        )
        params = (
            status.value, ts,
            owner_user_id_hash, conversation_id, expected_version,
        )
        return self._execute_versioned_update(
            sql, params, owner_user_id_hash, conversation_id, expected_version
        )

    def compare_and_update(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        *,
        expected_version: int,
        genie_conversation_id: Optional[str] = None,
        last_genie_message_id: Optional[str] = None,
        status: Optional[ConversationStatus] = None,
        touch_last_active: bool = True,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        """Atomically update multiple fields in a single compare-and-swap."""
        ts = _validate_now(now)

        # Build dynamic SET clause
        set_parts: List[str] = ["version = version + 1", "updated_at = %s"]
        params_list: List[Any] = [ts]

        if genie_conversation_id is not None:
            set_parts.append("genie_conversation_id = %s")
            params_list.append(genie_conversation_id)

        if last_genie_message_id is not None:
            set_parts.append("last_genie_message_id = %s")
            params_list.append(last_genie_message_id)

        if status is not None:
            set_parts.append("status = %s")
            params_list.append(status.value)

        if touch_last_active:
            set_parts.append("last_active_at = %s")
            params_list.append(ts)

        set_clause = ", ".join(set_parts)

        # WHERE clause params
        params_list.extend([owner_user_id_hash, conversation_id, expected_version])

        sql = (
            f"UPDATE {_TABLE_NAME} SET {set_clause} "
            "WHERE owner_user_id_hash = %s "
            "AND conversation_id = %s "
            f"AND version = %s {_RETURNING_CLAUSE}"
        )
        params = tuple(params_list)
        return self._execute_versioned_update(
            sql, params, owner_user_id_hash, conversation_id, expected_version
        )

    # -------------------------------------------------------------------------
    # Listing
    # -------------------------------------------------------------------------

    def list_for_owner(
        self,
        owner_user_id_hash: str,
        *,
        statuses: Optional[Collection[ConversationStatus]] = None,
        limit: int = 50,
    ) -> List[ConversationRecord]:
        """Return conversations owned by owner_user_id_hash."""
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit!r}")

        # Empty statuses collection = no results (by contract)
        if statuses is not None and len(statuses) == 0:
            return []

        params_list: List[Any] = [owner_user_id_hash]

        if statuses is not None:
            status_values = [s.value for s in statuses]
            placeholders = ", ".join(["%s"] * len(status_values))
            status_filter = f" AND status IN ({placeholders})"
            params_list.extend(status_values)
        else:
            status_filter = ""

        params_list.append(limit)

        sql = (
            f"SELECT {_COLUMNS_CSV} FROM {_TABLE_NAME} "
            f"WHERE owner_user_id_hash = %s{status_filter} "
            "ORDER BY updated_at DESC, conversation_id ASC "
            "LIMIT %s"
        )

        try:
            with self._connection_provider() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, tuple(params_list))
                    rows = cur.fetchall()
                    return [_require_row_to_record(r) for r in rows]
        except ConversationRepositoryError:
            raise
        except Exception as exc:
            raise _wrap_unavailable(exc)

    # -------------------------------------------------------------------------
    # Deletion
    # -------------------------------------------------------------------------

    def delete_conversation(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
    ) -> bool:
        """Permanently remove a conversation record."""
        sql = (
            f"DELETE FROM {_TABLE_NAME} "
            "WHERE owner_user_id_hash = %s AND conversation_id = %s"
        )
        try:
            with self._connection_provider() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (owner_user_id_hash, conversation_id))
                    deleted = cur.rowcount > 0
                    if deleted:
                        conn.commit()
                    return deleted
        except ConversationRepositoryError:
            raise
        except Exception as exc:
            raise _wrap_unavailable(exc)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _execute_versioned_update(
        self,
        sql: str,
        params: Tuple[Any, ...],
        owner_user_id_hash: str,
        conversation_id: str,
        expected_version: int,
    ) -> ConversationRecord:
        """Execute UPDATE ... RETURNING with version check and diagnosis.

        When the UPDATE returns no row, performs a follow-up SELECT to
        distinguish between not-found and version-conflict scenarios.
        """
        diagnose_sql = (
            f"SELECT version FROM {_TABLE_NAME} "
            "WHERE owner_user_id_hash = %s AND conversation_id = %s"
        )

        try:
            with self._connection_provider() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    row = cur.fetchone()

                    if row is not None:
                        # Update succeeded
                        conn.commit()
                        return _require_row_to_record(row)

                    # Update returned no row -- diagnose why
                    cur.execute(
                        diagnose_sql,
                        (owner_user_id_hash, conversation_id),
                    )
                    diag_row = cur.fetchone()
                    conn.rollback()

                    if diag_row is None:
                        raise ConversationNotFoundError(
                            "Conversation not found for this owner"
                        )

                    raise ConversationVersionConflictError(
                        "Version conflict: record has been modified"
                    )

        except ConversationRepositoryError:
            raise
        except Exception as exc:
            raise _wrap_unavailable(exc)
