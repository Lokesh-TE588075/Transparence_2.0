"""Tests for conversation state cleanup job.

Validates soft/hard cleanup behavior without requiring real Delta.
"""

import sys
import time
from unittest.mock import patch

sys.path.insert(0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app")

from app.services.conversation_state import BusinessContext
from app.services.delta_conversation_state import DeltaConversationStateManager
from app.services.chat_pipeline import SQLExecutionResult


# =============================================================================
# HELPERS
# =============================================================================


class FakeSQLExecutor:
    """Mock executor that tracks SQL calls."""
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def execute(self, sql):
        self.calls.append(sql)
        if self.fail:
            return _FakeResult(error="Simulated failure")
        return _FakeResult()


class _FakeResult:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error


# =============================================================================
# TEST 1: Expired session is marked inactive
# =============================================================================


class TestExpiredSessionCleanup:
    def test_expired_marked_inactive_in_memory(self):
        """Expired sessions are removed from in-memory state."""
        executor = FakeSQLExecutor()
        manager = DeltaConversationStateManager(
            table_name="test.schema.state",
            ttl_hours=1,
            sql_executor=executor,
        )
        # Inject expired context (2 hours old, TTL is 1 hour)
        ctx = BusinessContext(last_updated_at=time.time() - 7200)
        manager._sessions["conv-expired"] = ctx

        # get_context should return None for expired
        result = manager.get_context("conv-expired")
        assert result is None

    def test_cleanup_expired_sessions_runs_update(self):
        """cleanup_expired_sessions() issues UPDATE SQL."""
        executor = FakeSQLExecutor()
        manager = DeltaConversationStateManager(
            table_name="test.schema.state",
            ttl_hours=1,
            sql_executor=executor,
        )
        manager._delta_available = True

        manager.cleanup_expired_sessions()

        # Should have issued UPDATE with is_active = false
        update_calls = [c for c in executor.calls if "UPDATE" in c and "is_active = false" in c]
        assert len(update_calls) >= 1
        # Should check expires_at < CURRENT_TIMESTAMP()
        assert "expires_at < CURRENT_TIMESTAMP()" in update_calls[0]


# =============================================================================
# TEST 2: Active session is not modified
# =============================================================================


class TestActiveSessionPreserved:
    def test_active_session_not_expired(self):
        """Active (recent) sessions are returned normally."""
        executor = FakeSQLExecutor()
        manager = DeltaConversationStateManager(
            table_name="test.schema.state",
            ttl_hours=24,
            sql_executor=executor,
        )
        # Inject fresh context (5 minutes old)
        ctx = BusinessContext(last_updated_at=time.time() - 300, last_user_input="recent query")
        manager._sessions["conv-active"] = ctx

        result = manager.get_context("conv-active")
        assert result is not None
        assert result.last_user_input == "recent query"

    def test_cleanup_sql_only_targets_expired(self):
        """Cleanup SQL only affects expired rows."""
        executor = FakeSQLExecutor()
        manager = DeltaConversationStateManager(
            table_name="test.schema.state",
            ttl_hours=24,
            sql_executor=executor,
        )
        manager._delta_available = True
        manager.cleanup_expired_sessions()

        # Verify the SQL targets only expired rows
        update_calls = [c for c in executor.calls if "UPDATE" in c]
        for sql in update_calls:
            assert "expires_at < CURRENT_TIMESTAMP()" in sql
            # Should NOT unconditionally update all rows
            assert "WHERE" in sql


# =============================================================================
# TEST 3: Cleanup failure does not crash
# =============================================================================


class TestCleanupFailureSafe:
    def test_cleanup_failure_returns_zero(self):
        """If cleanup SQL fails, returns 0 without raising."""
        executor = FakeSQLExecutor(fail=True)
        manager = DeltaConversationStateManager(
            table_name="test.schema.state",
            ttl_hours=24,
            sql_executor=executor,
        )
        manager._delta_available = True

        # Should not raise
        count = manager.cleanup_expired_sessions()
        assert count == 0

    def test_cleanup_job_function_handles_error(self):
        """The run_cleanup function catches all errors."""
        from app.jobs.cleanup_expired_conversation_state import run_cleanup

        # Patch _execute_sql to raise
        with patch(
            "app.jobs.cleanup_expired_conversation_state._execute_sql",
            side_effect=Exception("Connection refused"),
        ):
            result = run_cleanup()
            # Should not crash, should report error
            assert result["status"] == "error"
            assert "Connection refused" in result["error"]


# =============================================================================
# TEST 4: Hard delete disabled by default
# =============================================================================


class TestHardDeleteDefault:
    def test_hard_delete_disabled_by_default(self):
        """CONVERSATION_STATE_CLEANUP_HARD_DELETE defaults to False."""
        from app.config import settings
        assert settings.CONVERSATION_STATE_CLEANUP_HARD_DELETE is False

    def test_cleanup_job_skips_delete_by_default(self):
        """With hard_delete=False, no DELETE SQL is issued."""
        import app.jobs.cleanup_expired_conversation_state as cleanup_mod

        calls = []

        def recording_execute(sql):
            calls.append(sql)
            if "COUNT" in sql:
                return {"rows": [{"cnt": "5"}], "error": None}
            return {"rows": [], "error": None}

        # Ensure HARD_DELETE is False for this test
        original_hd = cleanup_mod.HARD_DELETE
        try:
            cleanup_mod.HARD_DELETE = False
            with patch(
                "app.jobs.cleanup_expired_conversation_state._execute_sql",
                side_effect=recording_execute,
            ):
                result = cleanup_mod.run_cleanup()
                assert result["status"] == "success"
                # No DELETE statement should have been issued
                delete_calls = [c for c in calls if "DELETE" in c]
                assert len(delete_calls) == 0
        finally:
            cleanup_mod.HARD_DELETE = original_hd
