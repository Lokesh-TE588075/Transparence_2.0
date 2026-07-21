"""Tests for LakebaseMessageRepository (app/services/lakebase_message_repository.py).

Uses in-memory fakes and scripted mocks — no real database connection required.
Mirrors the pattern established by test_lakebase_conversation_repository.py.

H1 -- TransparencE Conversation History Persistence
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

import pytest

from app.services.lakebase_message_repository import (
    LakebaseMessageRepository,
    _MSG_COLUMNS,
    _row_to_record,
    _TABLE_NAME,
    _UNIQUE_VIOLATION_SQLSTATE,
)
from app.services.message_repository import (
    MESSAGE_TEXT_MAX_LEN,
    PAYLOAD_JSON_MAX_BYTES,
    MessageRecord,
    MessageRepositoryUnavailableError,
    MessageSequenceConflictError,
    MessageValidationError,
)


# =============================================================================
# TEST CONSTANTS
# =============================================================================

_OWNER = "owner_hash_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_OTHER_OWNER = "owner_hash_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_CONV_1 = "conv-uuid-0001-0001"
_CONV_2 = "conv-uuid-0002-0002"
_NOW = datetime(2026, 7, 21, 10, 0, 0, tzinfo=timezone.utc)
_MSG_ID = "msg-uuid-aaaa-bbbb-cccc"


# =============================================================================
# ROW BUILDER (positional order must match _MSG_COLUMNS)
# =============================================================================


def _make_row(
    message_id: str = _MSG_ID,
    owner_user_id_hash: str = _OWNER,
    frontend_conversation_id: str = _CONV_1,
    message_sequence: int = 1,
    role: str = "user",
    message_text: str = "hello",
    response_payload_json: Optional[str] = None,
    is_active: bool = True,
    created_at: Optional[datetime] = None,
    updated_at: Optional[datetime] = None,
) -> Tuple[Any, ...]:
    """Build a row tuple in the canonical _MSG_COLUMNS order."""
    ts = created_at or _NOW
    return (
        message_id,           # 0
        owner_user_id_hash,   # 1
        frontend_conversation_id,  # 2
        message_sequence,     # 3
        role,                 # 4
        message_text,         # 5
        response_payload_json, # 6
        is_active,            # 7
        ts,                   # 8 created_at
        updated_at or ts,     # 9 updated_at
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
        self._fetchone_results: List[Optional[Tuple[Any, ...]]] = []
        self._fetchall_results: List[List[Tuple[Any, ...]]] = []
        self._rowcount: int = 0
        self._execute_side_effects: List[Optional[Exception]] = []

    def set_fetchone_results(self, *results: Optional[Tuple[Any, ...]]) -> None:
        """Script sequential fetchone results."""
        self._fetchone_results = list(results)

    def set_fetchall_results(self, results: List[Tuple[Any, ...]]) -> None:
        """Script fetchall result."""
        self._fetchall_results = [results]

    def set_execute_side_effect(self, exc: Exception) -> None:
        """Make the next execute call raise."""
        self._execute_side_effects.append(exc)

    def set_rowcount(self, count: int) -> None:
        self._rowcount = count

    def execute(self, sql: str, parameters: Any = None) -> None:
        self.executed_sql.append(sql)
        self.executed_params.append(parameters)
        if self._execute_side_effects:
            effect = self._execute_side_effects.pop(0)
            if effect is not None:
                raise effect

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        if self._fetchone_results:
            return self._fetchone_results.pop(0)
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
        return self._conn

    def __exit__(self, *args: Any) -> None:
        pass


def make_provider(
    cursor: Optional[FakeCursor] = None,
    connection: Optional[FakeConnection] = None,
):
    """Create a scripted connection provider with access to internals."""
    cur = cursor or FakeCursor()
    conn = connection or FakeConnection(cur)
    ctx = FakeConnectionContext(conn)

    def provider():
        return ctx

    return provider, conn, cur


def make_error_provider(exc: Exception):
    """Create a provider that raises on context entry."""
    @contextmanager
    def provider():
        raise exc
        yield  # unreachable but required for generator protocol

    return provider


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def setup():
    """Standard test setup: provider, connection, cursor, repository."""
    provider, conn, cur = make_provider()
    repo = LakebaseMessageRepository(connection_provider=provider)
    return repo, conn, cur


# =============================================================================
# TEST: Construction and Module Imports
# =============================================================================


class TestConstructionAndImports:
    """Tests 1-4: module import, construction, no side effects."""

    def test_module_imports_without_psycopg(self):
        """Module loads without psycopg being present."""
        import importlib
        import sys
        mods_to_clear = [k for k in sys.modules if "lakebase_message" in k]
        for m in mods_to_clear:
            del sys.modules[m]
        mod = importlib.import_module("app.services.lakebase_message_repository")
        assert hasattr(mod, "LakebaseMessageRepository")

    def test_construction_opens_no_connection(self):
        """Repository construction must NOT call the provider."""
        called = []

        def provider():
            called.append(True)
            raise RuntimeError("provider must not be called on construction")

        _repo = LakebaseMessageRepository(connection_provider=provider)
        assert called == []

    def test_no_environment_variables_in_module(self):
        """No os.environ reads in the production module source."""
        import inspect
        import app.services.lakebase_message_repository as mod
        src = inspect.getsource(mod)
        assert "os.environ" not in src
        assert "os.getenv" not in src

    def test_table_name_is_bare_not_schema_qualified(self):
        """_TABLE_NAME must be bare — search_path is set by the connection provider."""
        assert "." not in _TABLE_NAME
        assert _TABLE_NAME == "app_conversation_message"

    def test_column_count(self):
        """10 columns required for migration 002 contract."""
        assert len(_MSG_COLUMNS) == 10


# =============================================================================
# TEST: _row_to_record helper
# =============================================================================


class TestRowToRecord:
    """Tests 5-9: row mapping correctness and error handling."""

    def test_maps_full_row_correctly(self):
        row = _make_row()
        rec = _row_to_record(row)
        assert rec is not None
        assert rec.message_id == _MSG_ID
        assert rec.owner_user_id_hash == _OWNER
        assert rec.frontend_conversation_id == _CONV_1
        assert rec.message_sequence == 1
        assert rec.role == "user"
        assert rec.message_text == "hello"
        assert rec.response_payload_json is None
        assert rec.is_active is True
        assert rec.created_at == _NOW
        assert rec.updated_at == _NOW

    def test_maps_assistant_row_with_payload(self):
        row = _make_row(
            role="assistant",
            message_text="response",
            response_payload_json='{"status": "success"}',
        )
        rec = _row_to_record(row)
        assert rec is not None
        assert rec.role == "assistant"
        assert rec.response_payload_json == '{"status": "success"}'

    def test_returns_none_for_none_row(self):
        assert _row_to_record(None) is None

    def test_raises_on_wrong_column_count(self):
        from app.services.message_repository import MessageRepositoryError
        row = ("only", "three", "cols")
        with pytest.raises(MessageRepositoryError):
            _row_to_record(row)  # type: ignore

    def test_inactive_row_is_mapped(self):
        row = _make_row(is_active=False)
        rec = _row_to_record(row)
        assert rec is not None
        assert rec.is_active is False


# =============================================================================
# TEST: append_message
# =============================================================================


class TestAppendMessage:
    """Tests 10-22: SQL parameterization, sequence assignment, validation, errors."""

    def test_executes_two_sql_statements(self, setup):
        """append_message must issue: (1) SELECT MAX(seq)+1, (2) INSERT."""
        repo, conn, cur = setup
        # First query: SELECT MAX returns 0 → next_seq = 1
        cur.set_fetchone_results((0,), _make_row())
        repo.append_message(_OWNER, _CONV_1, "user", "hello", now=_NOW)
        assert len(cur.executed_sql) == 2

    def test_select_max_uses_owner_and_conv_params(self, setup):
        """The sequence-determination SELECT is parameterized by owner + conv."""
        repo, conn, cur = setup
        cur.set_fetchone_results((0,), _make_row())
        repo.append_message(_OWNER, _CONV_1, "user", "hello", now=_NOW)
        select_params = cur.executed_params[0]
        assert select_params == (_OWNER, _CONV_1)

    def test_insert_uses_all_required_params(self, setup):
        """INSERT is parameterized with all 10 column values."""
        repo, conn, cur = setup
        cur.set_fetchone_results((2,), _make_row(message_sequence=3))
        repo.append_message(_OWNER, _CONV_1, "assistant", "response",
                            response_payload_json='{"ok": true}', now=_NOW)
        insert_params = cur.executed_params[1]
        assert insert_params is not None
        assert len(insert_params) == 10
        # owner and conv must be present
        assert _OWNER in insert_params
        assert _CONV_1 in insert_params
        # role
        assert "assistant" in insert_params
        # is_active=True
        assert True in insert_params

    def test_sequence_is_max_plus_one(self, setup):
        """When COALESCE(MAX,0)+1 returns 5 (MAX=4), INSERT receives sequence=5.
        NOTE: The SELECT contains '+1' so the DB returns the already-incremented
        value; the fake cursor must return 5 (not 4).
        """
        repo, conn, cur = setup
        # The SQL: SELECT COALESCE(MAX(message_sequence), 0) + 1
        # returns 5 when MAX=4; the fake cursor simulates the DB result.
        cur.set_fetchone_results((5,), _make_row(message_sequence=5))
        repo.append_message(_OWNER, _CONV_1, "user", "hello", now=_NOW)
        insert_params = cur.executed_params[1]
        # sequence is position 3 in the INSERT tuple (0-indexed: message_id=0, owner=1, conv=2, seq=3)
        assert insert_params[3] == 5

    def test_commit_called_on_success(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results((0,), _make_row())
        repo.append_message(_OWNER, _CONV_1, "user", "hello", now=_NOW)
        assert conn.commit_count == 1

    def test_returns_message_record(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results((0,), _make_row(role="user", message_text="hi"))
        rec = repo.append_message(_OWNER, _CONV_1, "user", "hi", now=_NOW)
        assert isinstance(rec, MessageRecord)
        assert rec.role == "user"
        assert rec.message_text == "hi"

    def test_unique_violation_raises_sequence_conflict(self, setup):
        """When INSERT fails with SQLSTATE 23505, raise MessageSequenceConflictError."""
        repo, conn, cur = setup
        # SELECT returns 1 (COALESCE(MAX,0)+1 = 1 when table is empty).
        # None in side-effects means SELECT execute succeeds; FakePostgresError fires on INSERT.
        cur.set_fetchone_results((1,))
        cur._execute_side_effects = [
            None,  # let SELECT succeed
            FakePostgresError("duplicate key", sqlstate=_UNIQUE_VIOLATION_SQLSTATE),
        ]
        with pytest.raises(MessageSequenceConflictError):
            repo.append_message(_OWNER, _CONV_1, "user", "hello", now=_NOW)

    def test_unique_violation_via_pgcode(self, setup):
        """pgcode attribute also triggers MessageSequenceConflictError."""
        repo, conn, cur = setup
        cur.set_fetchone_results((1,))
        cur._execute_side_effects = [
            None,  # let SELECT succeed
            FakePostgresError("duplicate key", pgcode=_UNIQUE_VIOLATION_SQLSTATE),
        ]
        with pytest.raises(MessageSequenceConflictError):
            repo.append_message(_OWNER, _CONV_1, "user", "hello", now=_NOW)

    def test_connection_error_raises_unavailable(self):
        """Non-unique-violation DB errors become MessageRepositoryUnavailableError."""
        provider = make_error_provider(ConnectionError("refused"))
        repo = LakebaseMessageRepository(connection_provider=provider)
        with pytest.raises(MessageRepositoryUnavailableError):
            repo.append_message(_OWNER, _CONV_1, "user", "hello")

    def test_validation_empty_owner_raises(self, setup):
        repo, _, _ = setup
        with pytest.raises(MessageValidationError):
            repo.append_message("", _CONV_1, "user", "hello")

    def test_validation_empty_conv_raises(self, setup):
        repo, _, _ = setup
        with pytest.raises(MessageValidationError):
            repo.append_message(_OWNER, "", "user", "hello")

    def test_validation_invalid_role_raises(self, setup):
        repo, _, _ = setup
        with pytest.raises(MessageValidationError):
            repo.append_message(_OWNER, _CONV_1, "system", "hello")

    def test_validation_empty_text_raises(self, setup):
        repo, _, _ = setup
        with pytest.raises(MessageValidationError):
            repo.append_message(_OWNER, _CONV_1, "user", "")

    def test_validation_text_too_long_raises(self, setup):
        repo, _, _ = setup
        with pytest.raises(MessageValidationError):
            repo.append_message(_OWNER, _CONV_1, "user", "x" * (MESSAGE_TEXT_MAX_LEN + 1))

    def test_validation_payload_too_large_raises(self, setup):
        repo, _, _ = setup
        with pytest.raises(MessageValidationError):
            repo.append_message(
                _OWNER, _CONV_1, "assistant", "text",
                response_payload_json="x" * (PAYLOAD_JSON_MAX_BYTES + 1),
            )

    def test_predefined_message_id_is_used(self, setup):
        repo, conn, cur = setup
        mid = str(uuid.uuid4())
        row = _make_row(message_id=mid)
        cur.set_fetchone_results((0,), row)
        rec = repo.append_message(_OWNER, _CONV_1, "user", "hello",
                                  message_id=mid, now=_NOW)
        insert_params = cur.executed_params[1]
        assert insert_params[0] == mid


# =============================================================================
# TEST: list_messages
# =============================================================================


class TestListMessages:
    """Tests 23-32: SQL parameterization, pagination, owner isolation, errors."""

    def test_executes_count_and_select_queries(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results((2,))  # COUNT(*) = 2
        cur.set_fetchall_results([
            _make_row(message_sequence=1),
            _make_row(message_sequence=2, role="assistant"),
        ])
        records, total = repo.list_messages(_OWNER, _CONV_1, page=1, page_size=10)
        assert len(cur.executed_sql) == 2
        assert total == 2
        assert len(records) == 2

    def test_count_query_filters_by_owner_and_conv_and_active(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results((1,))
        cur.set_fetchall_results([_make_row()])
        repo.list_messages(_OWNER, _CONV_1)
        count_params = cur.executed_params[0]
        assert count_params == (_OWNER, _CONV_1)
        # is_active=TRUE is baked into the SQL, not a parameter

    def test_select_query_uses_owner_conv_limit_offset(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results((3,))
        cur.set_fetchall_results([_make_row()])
        repo.list_messages(_OWNER, _CONV_1, page=2, page_size=5)
        select_params = cur.executed_params[1]
        # (owner, conv, limit, offset)
        assert select_params[0] == _OWNER
        assert select_params[1] == _CONV_1
        assert select_params[2] == 5   # limit = page_size
        assert select_params[3] == 5   # offset = (page-1) * page_size = 1 * 5

    def test_returns_empty_when_count_is_zero(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results((0,))
        records, total = repo.list_messages(_OWNER, _CONV_1)
        assert records == []
        assert total == 0
        # Only one query (COUNT); SELECT must not be executed when empty
        assert len(cur.executed_sql) == 1

    def test_records_returned_in_correct_type(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results((1,))
        cur.set_fetchall_results([_make_row()])
        records, _ = repo.list_messages(_OWNER, _CONV_1)
        assert len(records) == 1
        assert isinstance(records[0], MessageRecord)

    def test_page_size_capped_at_max(self, setup):
        from app.services.message_repository import MAX_PAGE_SIZE
        repo, conn, cur = setup
        cur.set_fetchone_results((1,))
        cur.set_fetchall_results([_make_row()])
        repo.list_messages(_OWNER, _CONV_1, page_size=MAX_PAGE_SIZE + 999)
        select_params = cur.executed_params[1]
        assert select_params[2] == MAX_PAGE_SIZE  # limit capped

    def test_owner_isolation_different_owner_returns_empty(self, setup):
        """Only messages matching owner_user_id_hash are returned."""
        repo, conn, cur = setup
        # Simulate COUNT for different owner returning 0
        cur.set_fetchone_results((0,))
        records, total = repo.list_messages(_OTHER_OWNER, _CONV_1)
        count_params = cur.executed_params[0]
        assert count_params[0] == _OTHER_OWNER  # query is scoped to this owner
        assert total == 0

    def test_connection_error_raises_unavailable(self):
        provider = make_error_provider(OSError("network error"))
        repo = LakebaseMessageRepository(connection_provider=provider)
        with pytest.raises(MessageRepositoryUnavailableError):
            repo.list_messages(_OWNER, _CONV_1)

    def test_select_sql_contains_order_by_sequence(self, setup):
        """Stable ordering is required: ORDER BY message_sequence ASC."""
        repo, conn, cur = setup
        cur.set_fetchone_results((1,))
        cur.set_fetchall_results([_make_row()])
        repo.list_messages(_OWNER, _CONV_1)
        select_sql = cur.executed_sql[1]
        assert "ORDER BY message_sequence ASC" in select_sql.replace("\n", " ")

    def test_select_sql_filters_is_active_true(self, setup):
        """Only active messages are returned by the query."""
        repo, conn, cur = setup
        cur.set_fetchone_results((1,))
        cur.set_fetchall_results([_make_row()])
        repo.list_messages(_OWNER, _CONV_1)
        select_sql = cur.executed_sql[1]
        assert "is_active = TRUE" in select_sql.replace("\n", " ")


# =============================================================================
# TEST: deactivate_messages
# =============================================================================


class TestDeactivateMessages:
    """Tests 33-40: UPDATE SQL correctness, commit, error handling."""

    def test_executes_update_query(self, setup):
        repo, conn, cur = setup
        cur.set_rowcount(2)
        count = repo.deactivate_messages(_OWNER, _CONV_1, now=_NOW)
        assert len(cur.executed_sql) == 1
        assert count == 2

    def test_update_params_include_owner_and_conv(self, setup):
        repo, conn, cur = setup
        cur.set_rowcount(1)
        repo.deactivate_messages(_OWNER, _CONV_1, now=_NOW)
        params = cur.executed_params[0]
        # params order: (ts, owner, conv)
        assert params[1] == _OWNER
        assert params[2] == _CONV_1

    def test_update_uses_provided_timestamp(self, setup):
        repo, conn, cur = setup
        cur.set_rowcount(0)
        repo.deactivate_messages(_OWNER, _CONV_1, now=_NOW)
        params = cur.executed_params[0]
        assert params[0] == _NOW

    def test_update_sql_sets_is_active_false(self, setup):
        repo, conn, cur = setup
        cur.set_rowcount(0)
        repo.deactivate_messages(_OWNER, _CONV_1, now=_NOW)
        sql = cur.executed_sql[0]
        assert "is_active = FALSE" in sql.replace("\n", " ")

    def test_update_sql_only_affects_active_rows(self, setup):
        repo, conn, cur = setup
        cur.set_rowcount(0)
        repo.deactivate_messages(_OWNER, _CONV_1, now=_NOW)
        sql = cur.executed_sql[0]
        assert "is_active = TRUE" in sql.replace("\n", " ")

    def test_commit_called_after_update(self, setup):
        repo, conn, cur = setup
        cur.set_rowcount(1)
        repo.deactivate_messages(_OWNER, _CONV_1, now=_NOW)
        assert conn.commit_count == 1

    def test_returns_zero_when_nothing_to_deactivate(self, setup):
        repo, conn, cur = setup
        cur.set_rowcount(0)
        count = repo.deactivate_messages(_OWNER, _CONV_1, now=_NOW)
        assert count == 0

    def test_connection_error_raises_unavailable(self):
        provider = make_error_provider(RuntimeError("connection refused"))
        repo = LakebaseMessageRepository(connection_provider=provider)
        with pytest.raises(MessageRepositoryUnavailableError):
            repo.deactivate_messages(_OWNER, _CONV_1)


# =============================================================================
# TEST: Owner isolation at SQL level
# =============================================================================


class TestOwnerIsolationAtSqlLevel:
    """Tests 41-43: owner_user_id_hash is always a SQL parameter, never interpolated."""

    def test_append_message_owner_is_parameterized(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results((0,), _make_row())
        repo.append_message(_OWNER, _CONV_1, "user", "hello", now=_NOW)
        # Owner must appear in params, not interpolated into SQL
        all_params_flat = [str(p) for params in cur.executed_params if params for p in params]
        assert _OWNER in all_params_flat
        # SQL itself must not contain the owner hash literally
        combined_sql = " ".join(cur.executed_sql)
        assert _OWNER not in combined_sql

    def test_list_messages_owner_is_parameterized(self, setup):
        repo, conn, cur = setup
        cur.set_fetchone_results((1,))
        cur.set_fetchall_results([_make_row()])
        repo.list_messages(_OWNER, _CONV_1)
        all_params_flat = [str(p) for params in cur.executed_params if params for p in params]
        assert _OWNER in all_params_flat
        combined_sql = " ".join(cur.executed_sql)
        assert _OWNER not in combined_sql

    def test_deactivate_owner_is_parameterized(self, setup):
        repo, conn, cur = setup
        cur.set_rowcount(1)
        repo.deactivate_messages(_OWNER, _CONV_1, now=_NOW)
        all_params_flat = [str(p) for params in cur.executed_params if params for p in params]
        assert _OWNER in all_params_flat
        combined_sql = " ".join(cur.executed_sql)
        assert _OWNER not in combined_sql
