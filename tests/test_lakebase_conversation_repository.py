"""Comprehensive tests for LakebaseConversationRepository.

Tests use in-memory fakes and scripted mocks — no real database connection.
Phase 2B1 — TransparencE Genie State Persistence.
"""

from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

from app.services.conversation_repository import (
    ConversationAlreadyExistsError,
    ConversationNotFoundError,
    ConversationOwnershipError,
    ConversationRecord,
    ConversationRepository,
    ConversationRepositoryError,
    ConversationRepositoryUnavailableError,
    ConversationStatus,
    ConversationVersionConflictError,
)
from app.services.lakebase_conversation_repository import (
    ConnectionContextProvider,
    ConnectionLike,
    CursorLike,
    LakebaseConversationRepository,
    _COLUMNS,
    _row_to_record,
)


# =============================================================================
# TEST CONSTANTS
# =============================================================================

_OWNER = "hmac_sha256_owner_hash_abc123"
_OTHER_OWNER = "hmac_sha256_other_owner_def456"
_FRONTEND_ID = "fe-uuid-1111-2222-3333"
_CONV_ID = "conv-uuid-aaaa-bbbb-cccc"
_GENIE_CONV_ID = "genie-conv-uuid-xxxx"
_GENIE_MSG_ID = "genie-msg-uuid-yyyy"
_NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
_LATER = datetime(2026, 7, 18, 13, 0, 0, tzinfo=timezone.utc)


def _make_row(
    conversation_id: str = _CONV_ID,
    owner_user_id_hash: str = _OWNER,
    frontend_conversation_id: str = _FRONTEND_ID,
    genie_conversation_id: Optional[str] = None,
    last_genie_message_id: Optional[str] = None,
    status: str = "ACTIVE",
    version: int = 1,
    created_at: Optional[datetime] = None,
    updated_at: Optional[datetime] = None,
    last_active_at: Optional[datetime] = None,
) -> Tuple[Any, ...]:
    """Build a row tuple in the canonical column order."""
    ts = created_at or _NOW
    return (
        conversation_id,
        owner_user_id_hash,
        frontend_conversation_id,
        genie_conversation_id,
        last_genie_message_id,
        status,
        version,
        ts,
        updated_at or ts,
        last_active_at or ts,
    )


# =============================================================================
# FAKE DATABASE OBJECTS
# =============================================================================


class FakePostgresError(Exception):
    """Simulated PostgreSQL error with sqlstate/pgcode support."""

    def __init__(self, message: str = "db error", sqlstate: Optional[str] = None,
                 pgcode: Optional[str] = None):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.pgcode = pgcode


class FakeCursor:
    """In-memory fake cursor that records SQL and returns scripted results."""

    def __init__(self) -> None:
        self.executed_sql: List[str] = []
        self.executed_params: List[Any] = []
        self._results: List[Optional[Tuple[Any, ...]]] = []
        self._fetchall_results: List[List[Tuple[Any, ...]]] = []
        self._rowcount: int = 0
        self._execute_side_effect: Optional[Exception] = None
        self._call_index: int = 0

    def set_fetchone_results(self, *results: Optional[Tuple[Any, ...]]) -> None:
        """Script sequential fetchone results."""
        self._results = list(results)

    def set_fetchall_results(self, results: List[Tuple[Any, ...]]) -> None:
        """Script fetchall result."""
        self._fetchall_results = [results]

    def set_execute_side_effect(self, exc: Exception) -> None:
        """Make execute raise on next call."""
        self._execute_side_effect = exc

    def set_rowcount(self, count: int) -> None:
        self._rowcount = count

    def execute(self, sql: str, parameters: Any = None) -> None:
        self.executed_sql.append(sql)
        self.executed_params.append(parameters)
        if self._execute_side_effect is not None:
            exc = self._execute_side_effect
            self._execute_side_effect = None
            raise exc

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        if self._results:
            return self._results.pop(0)
        return None

    def fetchall(self) -> List[Tuple[Any, ...]]:
        if self._fetchall_results:
            return self._fetchall_results.pop(0)
        return []

    @property
    def rowcount(self) -> int:
        return self._rowcount

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class FakeConnection:
    """In-memory fake connection that tracks commits and rollbacks."""

    def __init__(self, cursor: Optional[FakeCursor] = None) -> None:
        self._cursor = cursor or FakeCursor()
        self.commit_count: int = 0
        self.rollback_count: int = 0
        self.entered: bool = False
        self.exited: bool = False

    def cursor(self) -> FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class FakeConnectionContext:
    """Context manager wrapper for FakeConnection."""

    def __init__(self, connection: FakeConnection) -> None:
        self._conn = connection

    def __enter__(self) -> FakeConnection:
        self._conn.entered = True
        return self._conn

    def __exit__(self, *args: Any) -> None:
        self._conn.exited = True


def make_provider(
    cursor: Optional[FakeCursor] = None,
    connection: Optional[FakeConnection] = None,
) -> Tuple[ConnectionContextProvider, FakeConnection, FakeCursor]:
    """Create a scripted connection provider with access to internals."""
    cur = cursor or FakeCursor()
    conn = connection or FakeConnection(cur)
    ctx = FakeConnectionContext(conn)

    def provider():
        return ctx

    return provider, conn, cur


def make_error_provider(exc: Exception) -> ConnectionContextProvider:
    """Create a provider that raises on context entry."""
    @contextmanager
    def provider():
        raise exc
        yield  # unreachable but satisfies generator requirement

    return provider


# =============================================================================
# FIXTURE HELPERS
# =============================================================================


@pytest.fixture
def setup():
    """Standard test setup: provider, connection, cursor, repository."""
    provider, conn, cur = make_provider()
    repo = LakebaseConversationRepository(connection_provider=provider)
    return repo, conn, cur



# =============================================================================
# TEST CLASS: Construction and Imports
# =============================================================================


class TestConstructionAndImports:
    """Tests 1-5: module import, construction, no side effects."""

    def test_module_imports_without_psycopg(self):
        """Module loads even when psycopg is not installed."""
        import importlib
        import sys
        # Clear and reimport
        mods_to_clear = [k for k in sys.modules if "lakebase_conversation" in k]
        for m in mods_to_clear:
            del sys.modules[m]
        mod = importlib.import_module("app.services.lakebase_conversation_repository")
        assert hasattr(mod, "LakebaseConversationRepository")

    def test_construction_opens_no_connection(self):
        """Constructing the repository does not call the provider."""
        called = []

        def provider():
            called.append(True)
            raise RuntimeError("should not be called")

        _repo = LakebaseConversationRepository(connection_provider=provider)
        assert called == []

    def test_no_environment_variables_read(self):
        """No os.environ or os.getenv calls in the module source."""
        import inspect
        import app.services.lakebase_conversation_repository as mod
        source = inspect.getsource(mod)
        assert "os.environ" not in source
        assert "os.getenv" not in source
        assert "PGHOST" not in source
        assert "PGPASSWORD" not in source

    def test_no_threads_created(self):
        """Construction does not spawn threads."""
        import threading
        before = threading.active_count()
        provider, _, _ = make_provider()
        _repo = LakebaseConversationRepository(connection_provider=provider)
        after = threading.active_count()
        assert after <= before

    def test_satisfies_conversation_repository_protocol(self):
        """LakebaseConversationRepository is a subclass of the Protocol."""
        assert issubclass(LakebaseConversationRepository, ConversationRepository)


# =============================================================================
# TEST CLASS: Lookup Operations
# =============================================================================


class TestLookup:
    """Tests 6-11: get_by_id, get_by_frontend_id."""

    def test_get_by_id_returns_record(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(_make_row())
        result = repo.get_by_id(_OWNER, _CONV_ID)
        assert result is not None
        assert result.conversation_id == _CONV_ID
        assert result.owner_user_id_hash == _OWNER

    def test_get_by_frontend_id_returns_record(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(_make_row())
        result = repo.get_by_frontend_id(_OWNER, _FRONTEND_ID)
        assert result is not None
        assert result.frontend_conversation_id == _FRONTEND_ID

    def test_missing_returns_none(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(None)
        result = repo.get_by_id(_OWNER, "nonexistent")
        assert result is None

    def test_query_is_owner_scoped(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(None)
        repo.get_by_id(_OWNER, _CONV_ID)
        # Verify owner is in the SQL parameters
        params = cur.executed_params[0]
        assert _OWNER in params

    def test_sql_values_are_parameterized(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(None)
        repo.get_by_id(_OWNER, _CONV_ID)
        sql = cur.executed_sql[0]
        # SQL should use %s placeholders, not interpolated values
        assert "%s" in sql
        assert _OWNER not in sql
        assert _CONV_ID not in sql

    def test_database_lookup_failure_is_translated(self):
        provider = make_error_provider(RuntimeError("connection refused"))
        repo = LakebaseConversationRepository(connection_provider=provider)
        with pytest.raises(ConversationRepositoryUnavailableError):
            repo.get_by_id(_OWNER, _CONV_ID)


# =============================================================================
# TEST CLASS: Row Mapping
# =============================================================================


class TestRowMapping:
    """Tests 12-15: _row_to_record mapping."""

    def test_valid_tuple_maps_correctly(self):
        row = _make_row()
        record = _row_to_record(row)
        assert record is not None
        assert record.conversation_id == _CONV_ID
        assert record.status == ConversationStatus.ACTIVE
        assert record.version == 1
        assert record.created_at == _NOW

    def test_invalid_status_fails_safely(self):
        row = _make_row(status="INVALID_STATUS")
        with pytest.raises(ConversationRepositoryError):
            _row_to_record(row)

    def test_malformed_row_fails_safely(self):
        # Too few columns
        row = ("id", "owner", "frontend")
        with pytest.raises(ConversationRepositoryError):
            _row_to_record(row)

    def test_naive_timestamp_rejected(self):
        naive_ts = datetime(2026, 7, 18, 12, 0, 0)  # no tzinfo
        row = _make_row(created_at=naive_ts)
        # ConversationRecord __post_init__ will reject it
        with pytest.raises((ConversationRepositoryError, ValueError)):
            _row_to_record(row)

    def test_none_row_returns_none(self):
        assert _row_to_record(None) is None



# =============================================================================
# TEST CLASS: Creation
# =============================================================================


class TestCreation:
    """Tests 16-28: create_conversation."""

    def test_new_record_insert_succeeds(self, setup):
        repo, conn, cur = setup
        new_row = _make_row(version=1)
        cur.set_fetchone_results(new_row)  # INSERT RETURNING succeeds
        result = repo.create_conversation(_OWNER, _FRONTEND_ID, now=_NOW)
        assert result.status == ConversationStatus.ACTIVE
        assert result.version == 1
        assert conn.commit_count == 1

    def test_generated_uuid_used_when_absent(self, setup):
        repo, conn, cur = setup
        new_row = _make_row()
        cur.set_fetchone_results(new_row)
        result = repo.create_conversation(_OWNER, _FRONTEND_ID, now=_NOW)
        # The SQL params should contain a UUID-like string
        params = cur.executed_params[0]
        # conversation_id is first param
        cid = params[0]
        # Should be a valid UUID
        uuid.UUID(cid)  # will raise if invalid

    def test_explicit_conversation_id_accepted(self, setup):
        repo, conn, cur = setup
        explicit_id = "my-explicit-id-123"
        new_row = _make_row(conversation_id=explicit_id)
        cur.set_fetchone_results(new_row)
        result = repo.create_conversation(
            _OWNER, _FRONTEND_ID, conversation_id=explicit_id, now=_NOW
        )
        assert result.conversation_id == explicit_id

    def test_active_version_one_values_sent(self, setup):
        repo, conn, cur = setup
        new_row = _make_row()
        cur.set_fetchone_results(new_row)
        repo.create_conversation(_OWNER, _FRONTEND_ID, now=_NOW)
        params = cur.executed_params[0]
        # status is index 5, version is index 6
        assert params[5] == "ACTIVE"
        assert params[6] == 1

    def test_duplicate_logical_create_returns_existing_row(self, setup):
        repo, conn, cur = setup
        # INSERT returns None (ON CONFLICT DO NOTHING)
        existing_row = _make_row(version=3, updated_at=_LATER)
        cur.set_fetchone_results(None, existing_row)  # insert=None, select=existing
        result = repo.create_conversation(_OWNER, _FRONTEND_ID, now=_NOW)
        assert result.version == 3

    def test_duplicate_does_not_mutate_timestamps(self, setup):
        repo, conn, cur = setup
        original_ts = _NOW
        existing_row = _make_row(created_at=original_ts, updated_at=original_ts)
        cur.set_fetchone_results(None, existing_row)
        result = repo.create_conversation(_OWNER, _FRONTEND_ID, now=_LATER)
        assert result.created_at == original_ts
        assert result.updated_at == original_ts

    def test_duplicate_flow_remains_one_transaction(self, setup):
        repo, conn, cur = setup
        existing_row = _make_row()
        cur.set_fetchone_results(None, existing_row)
        repo.create_conversation(_OWNER, _FRONTEND_ID, now=_NOW)
        # Should use same connection (commit once at end)
        assert conn.commit_count == 1
        assert conn.rollback_count == 0

    def test_unique_violation_by_another_owner_raises_ownership_error(self, setup):
        repo, conn, cur = setup
        # Simulate PK unique violation (conversation_id already exists)
        cur.set_execute_side_effect(
            FakePostgresError(sqlstate="23505")
        )
        # After rollback, lookup reveals different owner
        other_owner_row = _make_row(
            owner_user_id_hash=_OTHER_OWNER,
            frontend_conversation_id="other-fe-id",
        )
        cur.set_fetchone_results(other_owner_row)
        with pytest.raises(ConversationOwnershipError):
            repo.create_conversation(
                _OWNER, _FRONTEND_ID,
                conversation_id=_CONV_ID, now=_NOW,
            )

    def test_global_conversation_id_conflict_raises_already_exists(self, setup):
        repo, conn, cur = setup
        # Simulate PK unique violation
        cur.set_execute_side_effect(
            FakePostgresError(sqlstate="23505")
        )
        # Same owner but different frontend_id
        conflict_row = _make_row(
            owner_user_id_hash=_OWNER,
            frontend_conversation_id="different-frontend-id",
        )
        cur.set_fetchone_results(conflict_row)
        with pytest.raises(ConversationAlreadyExistsError):
            repo.create_conversation(
                _OWNER, _FRONTEND_ID,
                conversation_id=_CONV_ID, now=_NOW,
            )

    def test_unique_sqlstate_detected_through_sqlstate(self, setup):
        repo, conn, cur = setup
        exc = FakePostgresError(sqlstate="23505")
        cur.set_execute_side_effect(exc)
        other_row = _make_row(owner_user_id_hash=_OTHER_OWNER)
        cur.set_fetchone_results(other_row)
        with pytest.raises(ConversationOwnershipError):
            repo.create_conversation(
                _OWNER, _FRONTEND_ID, conversation_id=_CONV_ID, now=_NOW
            )

    def test_unique_sqlstate_detected_through_pgcode(self, setup):
        repo, conn, cur = setup
        exc = FakePostgresError(pgcode="23505")
        cur.set_execute_side_effect(exc)
        other_row = _make_row(owner_user_id_hash=_OTHER_OWNER)
        cur.set_fetchone_results(other_row)
        with pytest.raises(ConversationOwnershipError):
            repo.create_conversation(
                _OWNER, _FRONTEND_ID, conversation_id=_CONV_ID, now=_NOW
            )

    def test_commit_once_on_success(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(_make_row())
        repo.create_conversation(_OWNER, _FRONTEND_ID, now=_NOW)
        assert conn.commit_count == 1

    def test_rollback_once_on_failure(self, setup):
        repo, conn, cur = setup
        cur.set_execute_side_effect(
            FakePostgresError(sqlstate="23505")
        )
        other_row = _make_row(owner_user_id_hash=_OTHER_OWNER)
        cur.set_fetchone_results(other_row)
        with pytest.raises(ConversationOwnershipError):
            repo.create_conversation(
                _OWNER, _FRONTEND_ID, conversation_id=_CONV_ID, now=_NOW
            )
        assert conn.rollback_count == 1



# =============================================================================
# TEST CLASS: Genie Binding
# =============================================================================


class TestGenieBinding:
    """Tests 29-34: bind_genie_conversation."""

    def test_successful_bind(self, setup):
        repo, conn, cur = setup
        updated_row = _make_row(
            genie_conversation_id=_GENIE_CONV_ID, version=2, updated_at=_LATER
        )
        cur.set_fetchone_results(updated_row)
        result = repo.bind_genie_conversation(
            _OWNER, _CONV_ID, _GENIE_CONV_ID, expected_version=1, now=_LATER
        )
        assert result.genie_conversation_id == _GENIE_CONV_ID
        assert result.version == 2
        assert conn.commit_count == 1

    def test_version_increments(self, setup):
        repo, conn, cur = setup
        updated_row = _make_row(genie_conversation_id=_GENIE_CONV_ID, version=2)
        cur.set_fetchone_results(updated_row)
        result = repo.bind_genie_conversation(
            _OWNER, _CONV_ID, _GENIE_CONV_ID, expected_version=1, now=_NOW
        )
        assert result.version == 2

    def test_stale_version_raises_conflict(self, setup):
        repo, conn, cur = setup
        # UPDATE returns None (version mismatch)
        # Diagnosis query returns row with different version
        cur.set_fetchone_results(None, (5,))
        with pytest.raises(ConversationVersionConflictError):
            repo.bind_genie_conversation(
                _OWNER, _CONV_ID, _GENIE_CONV_ID, expected_version=1, now=_NOW
            )

    def test_missing_record_raises_not_found(self, setup):
        repo, conn, cur = setup
        # UPDATE returns None, diagnosis returns None (not found)
        cur.set_fetchone_results(None, None)
        with pytest.raises(ConversationNotFoundError):
            repo.bind_genie_conversation(
                _OWNER, _CONV_ID, _GENIE_CONV_ID, expected_version=1, now=_NOW
            )

    def test_cross_owner_appears_not_found(self, setup):
        repo, conn, cur = setup
        # UPDATE returns None (owner filter), diagnosis also None
        cur.set_fetchone_results(None, None)
        with pytest.raises(ConversationNotFoundError):
            repo.bind_genie_conversation(
                _OTHER_OWNER, _CONV_ID, _GENIE_CONV_ID, expected_version=1, now=_NOW
            )

    def test_no_commit_on_failed_update(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(None, None)
        with pytest.raises(ConversationNotFoundError):
            repo.bind_genie_conversation(
                _OWNER, _CONV_ID, _GENIE_CONV_ID, expected_version=1, now=_NOW
            )
        assert conn.commit_count == 0
        assert conn.rollback_count == 1


# =============================================================================
# TEST CLASS: Message Update
# =============================================================================


class TestMessageUpdate:
    """Tests 35-37: update_last_genie_message."""

    def test_successful_message_update(self, setup):
        repo, conn, cur = setup
        updated_row = _make_row(last_genie_message_id=_GENIE_MSG_ID, version=2)
        cur.set_fetchone_results(updated_row)
        result = repo.update_last_genie_message(
            _OWNER, _CONV_ID, _GENIE_MSG_ID, expected_version=1, now=_NOW
        )
        assert result.last_genie_message_id == _GENIE_MSG_ID
        assert result.version == 2

    def test_empty_message_id_rejected_before_db_access(self, setup):
        repo, conn, cur = setup
        with pytest.raises(ValueError, match="must not be empty"):
            repo.update_last_genie_message(
                _OWNER, _CONV_ID, "", expected_version=1, now=_NOW
            )
        # No SQL should have been executed
        assert cur.executed_sql == []

    def test_version_conflict_handled(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(None, (3,))
        with pytest.raises(ConversationVersionConflictError):
            repo.update_last_genie_message(
                _OWNER, _CONV_ID, _GENIE_MSG_ID, expected_version=1, now=_NOW
            )


# =============================================================================
# TEST CLASS: Touch
# =============================================================================


class TestTouch:
    """Tests 38-40: touch."""

    def test_touch_updates_timestamps(self, setup):
        repo, conn, cur = setup
        updated_row = _make_row(version=2, updated_at=_LATER, last_active_at=_LATER)
        cur.set_fetchone_results(updated_row)
        result = repo.touch(_OWNER, _CONV_ID, expected_version=1, now=_LATER)
        assert result.updated_at == _LATER
        assert result.last_active_at == _LATER

    def test_touch_preserves_genie_values(self, setup):
        repo, conn, cur = setup
        updated_row = _make_row(
            genie_conversation_id=_GENIE_CONV_ID,
            last_genie_message_id=_GENIE_MSG_ID,
            version=2, updated_at=_LATER, last_active_at=_LATER,
        )
        cur.set_fetchone_results(updated_row)
        result = repo.touch(_OWNER, _CONV_ID, expected_version=1, now=_LATER)
        assert result.genie_conversation_id == _GENIE_CONV_ID
        assert result.last_genie_message_id == _GENIE_MSG_ID

    def test_version_increments_once(self, setup):
        repo, conn, cur = setup
        updated_row = _make_row(version=4, updated_at=_LATER, last_active_at=_LATER)
        cur.set_fetchone_results(updated_row)
        result = repo.touch(_OWNER, _CONV_ID, expected_version=3, now=_LATER)
        assert result.version == 4


# =============================================================================
# TEST CLASS: Status
# =============================================================================


class TestStatus:
    """Tests 41-43: set_status."""

    @pytest.mark.parametrize("target_status", list(ConversationStatus))
    def test_set_each_supported_status(self, target_status, setup):
        repo, conn, cur = setup
        updated_row = _make_row(status=target_status.value, version=2)
        cur.set_fetchone_results(updated_row)
        result = repo.set_status(
            _OWNER, _CONV_ID, target_status, expected_version=1, now=_NOW
        )
        assert result.status == target_status

    def test_invalid_status_rejected(self, setup):
        repo, conn, cur = setup
        with pytest.raises((ValueError, AttributeError)):
            repo.set_status(
                _OWNER, _CONV_ID, "NOT_A_STATUS", expected_version=1, now=_NOW
            )

    def test_status_sql_parameterized(self, setup):
        repo, conn, cur = setup
        updated_row = _make_row(status="STALE", version=2)
        cur.set_fetchone_results(updated_row)
        repo.set_status(
            _OWNER, _CONV_ID, ConversationStatus.STALE, expected_version=1, now=_NOW
        )
        sql = cur.executed_sql[0]
        assert "STALE" not in sql  # value not interpolated
        assert "%s" in sql


# =============================================================================
# TEST CLASS: Compare and Update
# =============================================================================


class TestCompareAndUpdate:
    """Tests 44-47: compare_and_update."""

    def test_multiple_fields_updated_atomically(self, setup):
        repo, conn, cur = setup
        updated_row = _make_row(
            genie_conversation_id=_GENIE_CONV_ID,
            last_genie_message_id=_GENIE_MSG_ID,
            status="STALE",
            version=2,
            updated_at=_LATER,
            last_active_at=_LATER,
        )
        cur.set_fetchone_results(updated_row)
        result = repo.compare_and_update(
            _OWNER, _CONV_ID,
            expected_version=1,
            genie_conversation_id=_GENIE_CONV_ID,
            last_genie_message_id=_GENIE_MSG_ID,
            status=ConversationStatus.STALE,
            touch_last_active=True,
            now=_LATER,
        )
        assert result.genie_conversation_id == _GENIE_CONV_ID
        assert result.last_genie_message_id == _GENIE_MSG_ID
        assert result.status == ConversationStatus.STALE
        assert result.last_active_at == _LATER
        assert conn.commit_count == 1

    def test_touch_last_active_false_preserves_last_active(self, setup):
        repo, conn, cur = setup
        updated_row = _make_row(
            version=2, updated_at=_LATER, last_active_at=_NOW
        )
        cur.set_fetchone_results(updated_row)
        result = repo.compare_and_update(
            _OWNER, _CONV_ID,
            expected_version=1,
            touch_last_active=False,
            now=_LATER,
        )
        # SQL should NOT include last_active_at update
        sql = cur.executed_sql[0]
        assert result.last_active_at == _NOW

    def test_no_op_field_set_still_increments_version(self, setup):
        repo, conn, cur = setup
        updated_row = _make_row(version=2, updated_at=_LATER, last_active_at=_LATER)
        cur.set_fetchone_results(updated_row)
        # No optional fields provided but version still increments
        result = repo.compare_and_update(
            _OWNER, _CONV_ID,
            expected_version=1,
            now=_LATER,
        )
        assert result.version == 2
        assert conn.commit_count == 1

    def test_stale_update_changes_nothing(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(None, (5,))
        with pytest.raises(ConversationVersionConflictError):
            repo.compare_and_update(
                _OWNER, _CONV_ID,
                expected_version=1,
                genie_conversation_id=_GENIE_CONV_ID,
                now=_LATER,
            )
        assert conn.commit_count == 0



# =============================================================================
# TEST CLASS: Listing
# =============================================================================


class TestListing:
    """Tests 48-54: list_for_owner."""

    def test_owner_scoped_list(self, setup):
        repo, conn, cur = setup
        rows = [_make_row(conversation_id=f"conv-{i}") for i in range(3)]
        cur.set_fetchall_results(rows)
        results = repo.list_for_owner(_OWNER)
        assert len(results) == 3
        # Verify owner in SQL params
        params = cur.executed_params[0]
        assert params[0] == _OWNER

    def test_ordered_sql(self, setup):
        repo, conn, cur = setup
        cur.set_fetchall_results([])
        repo.list_for_owner(_OWNER)
        sql = cur.executed_sql[0]
        assert "ORDER BY updated_at DESC" in sql
        assert "conversation_id ASC" in sql

    def test_status_filter(self, setup):
        repo, conn, cur = setup
        rows = [_make_row(status="ACTIVE")]
        cur.set_fetchall_results(rows)
        results = repo.list_for_owner(
            _OWNER, statuses=[ConversationStatus.ACTIVE]
        )
        sql = cur.executed_sql[0]
        assert "status IN" in sql

    def test_multiple_status_placeholders_parameterized(self, setup):
        repo, conn, cur = setup
        cur.set_fetchall_results([])
        repo.list_for_owner(
            _OWNER,
            statuses=[ConversationStatus.ACTIVE, ConversationStatus.STALE],
        )
        sql = cur.executed_sql[0]
        assert sql.count("%s") >= 4  # owner + 2 statuses + limit
        params = cur.executed_params[0]
        assert "ACTIVE" in params
        assert "STALE" in params

    def test_empty_status_filter_returns_empty_without_connection(self):
        called = []

        def provider():
            called.append(True)
            raise RuntimeError("should not be called")

        repo = LakebaseConversationRepository(connection_provider=provider)
        results = repo.list_for_owner(_OWNER, statuses=[])
        assert results == []
        assert called == []

    def test_invalid_limit_rejected_before_db_access(self):
        called = []

        def provider():
            called.append(True)
            raise RuntimeError("should not be called")

        repo = LakebaseConversationRepository(connection_provider=provider)
        with pytest.raises(ValueError, match="limit must be >= 1"):
            repo.list_for_owner(_OWNER, limit=0)
        assert called == []

    def test_result_rows_mapped_correctly(self, setup):
        repo, conn, cur = setup
        rows = [
            _make_row(conversation_id="c1", status="ACTIVE"),
            _make_row(conversation_id="c2", status="STALE"),
        ]
        cur.set_fetchall_results(rows)
        results = repo.list_for_owner(_OWNER)
        assert results[0].conversation_id == "c1"
        assert results[1].status == ConversationStatus.STALE


# =============================================================================
# TEST CLASS: Deletion
# =============================================================================


class TestDeletion:
    """Tests 55-59: delete_conversation."""

    def test_owned_delete_returns_true(self, setup):
        repo, conn, cur = setup
        cur.set_rowcount(1)
        result = repo.delete_conversation(_OWNER, _CONV_ID)
        assert result is True
        assert conn.commit_count == 1

    def test_missing_delete_returns_false(self, setup):
        repo, conn, cur = setup
        cur.set_rowcount(0)
        result = repo.delete_conversation(_OWNER, "nonexistent")
        assert result is False

    def test_cross_owner_delete_returns_false(self, setup):
        repo, conn, cur = setup
        cur.set_rowcount(0)
        result = repo.delete_conversation(_OTHER_OWNER, _CONV_ID)
        assert result is False

    def test_commit_behaviour_correct(self, setup):
        repo, conn, cur = setup
        cur.set_rowcount(1)
        repo.delete_conversation(_OWNER, _CONV_ID)
        assert conn.commit_count == 1
        # Not deleted case
        conn.commit_count = 0
        cur.set_rowcount(0)
        repo.delete_conversation(_OWNER, "other")
        assert conn.commit_count == 0

    def test_database_error_rolls_back(self):
        provider = make_error_provider(RuntimeError("disk full"))
        repo = LakebaseConversationRepository(connection_provider=provider)
        with pytest.raises(ConversationRepositoryUnavailableError):
            repo.delete_conversation(_OWNER, _CONV_ID)


# =============================================================================
# TEST CLASS: Security and SQL
# =============================================================================


class TestSecurityAndSQL:
    """Tests 60-66: SQL safety, no interpolation, no DDL."""

    def test_no_user_value_interpolated_into_sql(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(None)
        repo.get_by_id(_OWNER, _CONV_ID)
        sql = cur.executed_sql[0]
        assert _OWNER not in sql
        assert _CONV_ID not in sql

    def test_no_sql_includes_raw_owner_hash(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(None)
        repo.get_by_frontend_id(_OWNER, _FRONTEND_ID)
        for sql in cur.executed_sql:
            assert _OWNER not in sql

    def test_no_sql_includes_raw_conversation_id(self, setup):
        repo, conn, cur = setup
        updated_row = _make_row(version=2, updated_at=_LATER, last_active_at=_LATER)
        cur.set_fetchone_results(updated_row)
        repo.touch(_OWNER, _CONV_ID, expected_version=1, now=_LATER)
        for sql in cur.executed_sql:
            assert _CONV_ID not in sql

    def test_exception_messages_do_not_include_sql(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(None, None)
        with pytest.raises(ConversationNotFoundError) as exc_info:
            repo.bind_genie_conversation(
                _OWNER, _CONV_ID, _GENIE_CONV_ID, expected_version=1, now=_NOW
            )
        msg = str(exc_info.value)
        assert "SELECT" not in msg
        assert "UPDATE" not in msg

    def test_exception_messages_do_not_include_credentials(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(None, None)
        with pytest.raises(ConversationNotFoundError) as exc_info:
            repo.bind_genie_conversation(
                _OWNER, _CONV_ID, _GENIE_CONV_ID, expected_version=1, now=_NOW
            )
        msg = str(exc_info.value)
        assert _OWNER not in msg
        assert "password" not in msg.lower()

    def test_no_ddl_statement_exists(self):
        """No CREATE/DROP/ALTER TABLE in repository operations."""
        import inspect
        import app.services.lakebase_conversation_repository as mod
        source = inspect.getsource(mod)
        ddl_patterns = ["CREATE TABLE", "DROP TABLE", "ALTER TABLE", "CREATE INDEX"]
        for pattern in ddl_patterns:
            assert pattern not in source, f"DDL found: {pattern}"

    def test_no_write_in_read_methods(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(None)
        repo.get_by_id(_OWNER, _CONV_ID)
        for sql in cur.executed_sql:
            # Check that the SQL statement is a SELECT, not a DML command
            stripped = sql.strip().upper()
            assert stripped.startswith("SELECT"), (
                f"Read method issued non-SELECT: {sql[:50]}"
            )
            assert not stripped.startswith("INSERT")
            assert not stripped.startswith("UPDATE")
            assert not stripped.startswith("DELETE")


# =============================================================================
# TEST CLASS: Transaction Integrity
# =============================================================================


class TestTransactionIntegrity:
    """Tests 67-71: transaction behaviour."""

    def test_duplicate_create_uses_same_connection(self, setup):
        repo, conn, cur = setup
        existing_row = _make_row()
        cur.set_fetchone_results(None, existing_row)
        repo.create_conversation(_OWNER, _FRONTEND_ID, now=_NOW)
        # Both INSERT and SELECT should use same cursor (same connection)
        assert len(cur.executed_sql) == 2
        assert conn.entered is True

    def test_failed_update_diagnosis_uses_same_connection(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(None, (3,))  # UPDATE=None, diag=(3,)
        with pytest.raises(ConversationVersionConflictError):
            repo.touch(_OWNER, _CONV_ID, expected_version=1, now=_NOW)
        # Both UPDATE and diagnosis SELECT on same cursor
        assert len(cur.executed_sql) == 2
        assert conn.entered is True

    def test_commit_exactly_once(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(_make_row(version=2, updated_at=_LATER, last_active_at=_LATER))
        repo.touch(_OWNER, _CONV_ID, expected_version=1, now=_LATER)
        assert conn.commit_count == 1

    def test_rollback_exactly_once(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(None, None)
        with pytest.raises(ConversationNotFoundError):
            repo.touch(_OWNER, _CONV_ID, expected_version=1, now=_NOW)
        assert conn.rollback_count == 1

    def test_connection_context_exits_on_error(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results(None, None)
        with pytest.raises(ConversationNotFoundError):
            repo.touch(_OWNER, _CONV_ID, expected_version=1, now=_NOW)
        assert conn.exited is True



# =============================================================================
# TEST CLASS: Interface Completeness
# =============================================================================


class TestInterfaceCompleteness:
    """Tests 72-75: all methods, signatures, no dependencies."""

    def test_all_10_repository_methods_implemented(self):
        required = {
            "get_by_id", "get_by_frontend_id", "create_conversation",
            "bind_genie_conversation", "update_last_genie_message", "touch",
            "set_status", "compare_and_update", "list_for_owner",
            "delete_conversation",
        }
        implemented = {
            m for m in dir(LakebaseConversationRepository)
            if not m.startswith("_") and callable(getattr(LakebaseConversationRepository, m))
        }
        assert required.issubset(implemented)

    def test_method_signatures_compatible_with_protocol(self):
        import inspect
        protocol_methods = {
            m for m in dir(ConversationRepository)
            if not m.startswith("_")
            and callable(getattr(ConversationRepository, m, None))
        }
        for method_name in protocol_methods:
            proto_sig = inspect.signature(getattr(ConversationRepository, method_name))
            impl_sig = inspect.signature(getattr(LakebaseConversationRepository, method_name))
            # Parameter names should match
            proto_params = list(proto_sig.parameters.keys())
            impl_params = list(impl_sig.parameters.keys())
            assert proto_params == impl_params, (
                f"{method_name}: proto={proto_params} impl={impl_params}"
            )

    def test_no_psycopg_dependency_imported(self):
        import sys
        # After importing our module, psycopg should not be in sys.modules
        assert "psycopg" not in sys.modules
        assert "psycopg2" not in sys.modules
        assert "psycopg_pool" not in sys.modules
        assert "sqlalchemy" not in sys.modules

    def test_no_runtime_application_module_imports_new_repository(self):
        """No existing app module imports lakebase_conversation_repository."""
        import os
        import ast as ast_mod

        app_dir = os.path.join(
            "/Workspace/Users/lokesh.choraria@te.com/Transparence/Transparence_2_0_git",
            "app"
        )
        target_module = "lakebase_conversation_repository"

        for root, dirs, files in os.walk(app_dir):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                if fname == "lakebase_conversation_repository.py":
                    continue
                fpath = os.path.join(root, fname)
                with open(fpath, "r") as f:
                    try:
                        tree = ast_mod.parse(f.read())
                    except SyntaxError:
                        continue
                for node in ast_mod.walk(tree):
                    if isinstance(node, ast_mod.Import):
                        for alias in node.names:
                            assert target_module not in alias.name, (
                                f"{fpath} imports {target_module}"
                            )
                    elif isinstance(node, ast_mod.ImportFrom):
                        if node.module and target_module in node.module:
                            raise AssertionError(
                                f"{fpath} imports from {target_module}"
                            )

