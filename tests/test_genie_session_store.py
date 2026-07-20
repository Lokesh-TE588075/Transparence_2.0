"""Unit tests for app/services/genie_session_store.py.

All tests are pure in-memory. No Databricks SDK, no HTTP, no Delta.

Test cases:
    1.  set/get Genie conversation ID.
    2.  set/get last Genie message ID.
    3.  get_session returns full GenieSession with all fields.
    4.  reset_session clears Genie conversation and message IDs.
    5.  Expired session is not returned by get_session.
    6.  cleanup_expired_sessions removes expired sessions and returns count.
    7.  serialize / deserialize round-trip preserves all fields.
    8.  Sessions are independent per app_conversation_id.
    9.  set_last_message_id refreshes updated_at.
    10. get on missing session returns None.
    11. cleanup returns 0 when no sessions are expired.
    12. active_session_count excludes expired sessions.
"""

import logging
import logging.handlers
import sys
import time
from datetime import datetime, timedelta, timezone

import os as _os
_REPO_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
del _os, _REPO_ROOT

from app.services.genie_session_store import GenieSessionStore, GenieSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

APP_CONV_A = "app-conv-aaaa-1111"
APP_CONV_B = "app-conv-bbbb-2222"
GENIE_CONV_A = "genie-conv-aaaa-1111"
GENIE_CONV_B = "genie-conv-bbbb-2222"
MSG_1 = "genie-msg-0001"
MSG_2 = "genie-msg-0002"


def _store(ttl_hours: int = 24) -> GenieSessionStore:
    return GenieSessionStore(ttl_hours=ttl_hours)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _past(seconds: int) -> datetime:
    return _now() - timedelta(seconds=seconds)


def _future(seconds: int) -> datetime:
    return _now() + timedelta(seconds=seconds)


# =============================================================================
# TEST 1: set/get Genie conversation ID
# =============================================================================


class TestSetGetGenieConversationId:
    def test_set_and_get_returns_correct_id(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        result = store.get_genie_conversation_id(APP_CONV_A)
        assert result == GENIE_CONV_A

    def test_update_overwrites_previous_id(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_B)
        result = store.get_genie_conversation_id(APP_CONV_A)
        assert result == GENIE_CONV_B

    def test_missing_key_returns_none(self):
        store = _store()
        assert store.get_genie_conversation_id("non-existent-id") is None


# =============================================================================
# TEST 2: set/get last Genie message ID
# =============================================================================


class TestSetGetLastMessageId:
    def test_set_and_get_returns_message_id(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_last_message_id(APP_CONV_A, MSG_1)
        assert store.get_last_message_id(APP_CONV_A) == MSG_1

    def test_update_message_id_returns_new_value(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_last_message_id(APP_CONV_A, MSG_1)
        store.set_last_message_id(APP_CONV_A, MSG_2)
        assert store.get_last_message_id(APP_CONV_A) == MSG_2

    def test_missing_key_returns_none(self):
        store = _store()
        assert store.get_last_message_id("non-existent-id") is None


# =============================================================================
# TEST 3: get_session returns full GenieSession
# =============================================================================


class TestGetSession:
    def test_returns_genesession_with_all_fields(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_last_message_id(APP_CONV_A, MSG_1)

        session = store.get_session(APP_CONV_A)
        assert session is not None
        assert isinstance(session, GenieSession)
        assert session.app_conversation_id   == APP_CONV_A
        assert session.genie_conversation_id == GENIE_CONV_A
        assert session.last_genie_message_id == MSG_1
        assert session.is_active is True

    def test_session_has_created_at_and_updated_at(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)

        session = store.get_session(APP_CONV_A)
        assert session.created_at is not None
        assert session.updated_at is not None
        assert isinstance(session.created_at, datetime)

    def test_session_has_expires_at(self):
        store = _store(ttl_hours=2)
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)

        session = store.get_session(APP_CONV_A)
        now = _now()
        # expires_at should be ~2h from now
        assert session.expires_at > now
        assert session.expires_at < now + timedelta(hours=3)


# =============================================================================
# TEST 4: reset_session clears conversation and message IDs
# =============================================================================


class TestResetSession:
    def test_reset_clears_genie_conversation_id(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_last_message_id(APP_CONV_A, MSG_1)

        store.reset_session(APP_CONV_A)

        assert store.get_genie_conversation_id(APP_CONV_A) is None

    def test_reset_clears_last_message_id(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_last_message_id(APP_CONV_A, MSG_1)

        store.reset_session(APP_CONV_A)

        assert store.get_last_message_id(APP_CONV_A) is None

    def test_reset_marks_session_inactive(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.reset_session(APP_CONV_A)

        session = store.get_session(APP_CONV_A)
        assert session is None  # inactive sessions return None

    def test_can_set_new_conversation_after_reset(self):
        """After reset, the next set_genie_conversation_id should create a fresh session."""
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.reset_session(APP_CONV_A)
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_B)

        assert store.get_genie_conversation_id(APP_CONV_A) == GENIE_CONV_B

    def test_reset_nonexistent_does_not_raise(self):
        store = _store()
        store.reset_session("non-existent-id")  # must not raise


# =============================================================================
# TEST 5: Expired session is not returned
# =============================================================================


class TestExpiredSession:
    def test_expired_session_returns_none(self):
        store = _store(ttl_hours=0)  # 0-hour TTL — expires immediately
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)

        # Force expiry by manipulating expires_at directly
        with store._lock:
            session = store._sessions[APP_CONV_A]
            session.expires_at = _past(10)  # expired 10 seconds ago

        result = store.get_session(APP_CONV_A)
        assert result is None

    def test_expired_conversation_id_returns_none(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)

        with store._lock:
            store._sessions[APP_CONV_A].expires_at = _past(1)

        assert store.get_genie_conversation_id(APP_CONV_A) is None

    def test_expired_message_id_returns_none(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_last_message_id(APP_CONV_A, MSG_1)

        with store._lock:
            store._sessions[APP_CONV_A].expires_at = _past(1)

        assert store.get_last_message_id(APP_CONV_A) is None


# =============================================================================
# TEST 6: cleanup_expired_sessions removes expired sessions
# =============================================================================


class TestCleanup:
    def test_cleanup_removes_expired_and_returns_count(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_genie_conversation_id(APP_CONV_B, GENIE_CONV_B)

        # Expire session A
        with store._lock:
            store._sessions[APP_CONV_A].expires_at = _past(1)

        removed = store.cleanup_expired_sessions()

        assert removed == 1
        assert store.session_count() == 1  # B remains

    def test_cleanup_zero_when_no_expired(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)

        removed = store.cleanup_expired_sessions()
        assert removed == 0
        assert store.session_count() == 1

    def test_cleanup_removes_inactive_sessions(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.reset_session(APP_CONV_A)  # marks as inactive

        removed = store.cleanup_expired_sessions()
        assert removed == 1
        assert store.session_count() == 0

    def test_cleanup_leaves_active_sessions(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_genie_conversation_id(APP_CONV_B, GENIE_CONV_B)

        removed = store.cleanup_expired_sessions()
        assert removed == 0
        assert store.active_session_count() == 2


# =============================================================================
# TEST 7: serialize / deserialize round-trip
# =============================================================================


class TestSerializeDeserialize:
    def test_round_trip_preserves_all_fields(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_last_message_id(APP_CONV_A, MSG_1)

        session = store.get_session(APP_CONV_A)
        serialized = store.serialize_session(session)
        restored  = store.deserialize_session(serialized)

        assert restored.app_conversation_id   == session.app_conversation_id
        assert restored.genie_conversation_id == session.genie_conversation_id
        assert restored.last_genie_message_id == session.last_genie_message_id
        assert restored.is_active             == session.is_active

    def test_serialized_form_is_plain_dict(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        session    = store.get_session(APP_CONV_A)
        serialized = store.serialize_session(session)

        assert isinstance(serialized, dict)
        # All datetime values are ISO strings, not datetime objects
        for key in ("created_at", "updated_at", "expires_at"):
            assert isinstance(serialized[key], str), (
                f"{key} should be ISO string, got {type(serialized[key])}"
            )

    def test_deserialized_datetimes_are_utc_aware(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        session    = store.get_session(APP_CONV_A)
        serialized = store.serialize_session(session)
        restored   = store.deserialize_session(serialized)

        assert restored.created_at.tzinfo  is not None
        assert restored.updated_at.tzinfo  is not None
        assert restored.expires_at.tzinfo  is not None

    def test_round_trip_from_expired_session(self):
        """Serialization/deserialization must work even for expired sessions."""
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)

        with store._lock:
            store._sessions[APP_CONV_A].expires_at = _past(100)

        # Get raw session (bypassing expiry check)
        with store._lock:
            session = store._sessions[APP_CONV_A]

        serialized = store.serialize_session(session)
        restored   = store.deserialize_session(serialized)
        assert restored.app_conversation_id == APP_CONV_A


# =============================================================================
# TEST 8: Sessions are independent per app_conversation_id
# =============================================================================


class TestSessionIsolation:
    def test_two_app_convs_have_independent_sessions(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_genie_conversation_id(APP_CONV_B, GENIE_CONV_B)

        assert store.get_genie_conversation_id(APP_CONV_A) == GENIE_CONV_A
        assert store.get_genie_conversation_id(APP_CONV_B) == GENIE_CONV_B

    def test_resetting_one_does_not_affect_other(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_genie_conversation_id(APP_CONV_B, GENIE_CONV_B)

        store.reset_session(APP_CONV_A)

        assert store.get_genie_conversation_id(APP_CONV_A) is None
        assert store.get_genie_conversation_id(APP_CONV_B) == GENIE_CONV_B

    def test_message_ids_are_per_conversation(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_genie_conversation_id(APP_CONV_B, GENIE_CONV_B)
        store.set_last_message_id(APP_CONV_A, MSG_1)
        store.set_last_message_id(APP_CONV_B, MSG_2)

        assert store.get_last_message_id(APP_CONV_A) == MSG_1
        assert store.get_last_message_id(APP_CONV_B) == MSG_2


# =============================================================================
# TEST 9: set_last_message_id refreshes updated_at
# =============================================================================


class TestUpdatedAtRefreshes:
    def test_set_message_id_updates_updated_at(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)

        session_before = store.get_session(APP_CONV_A)
        before = session_before.updated_at

        # Small sleep to ensure timestamp differs
        time.sleep(0.02)
        store.set_last_message_id(APP_CONV_A, MSG_1)

        session_after = store.get_session(APP_CONV_A)
        after = session_after.updated_at

        assert after >= before

    def test_set_conv_id_refreshes_ttl(self):
        """Calling set_genie_conversation_id on an existing session should
        refresh the TTL (expires_at moves forward).
        """
        store = _store(ttl_hours=1)
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)

        session_before = store.get_session(APP_CONV_A)
        exp_before = session_before.expires_at

        time.sleep(0.05)
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_B)

        session_after = store.get_session(APP_CONV_A)
        exp_after = session_after.expires_at

        assert exp_after >= exp_before


# =============================================================================
# TEST 10: get on missing session returns None
# =============================================================================


class TestMissingSession:
    def test_get_session_missing_returns_none(self):
        store = _store()
        assert store.get_session("totally-unknown-conv-id") is None

    def test_get_conv_id_missing_returns_none(self):
        store = _store()
        assert store.get_genie_conversation_id("totally-unknown-conv-id") is None

    def test_get_message_id_missing_returns_none(self):
        store = _store()
        assert store.get_last_message_id("totally-unknown-conv-id") is None


# =============================================================================
# TEST 11: cleanup returns 0 when no sessions expired
# =============================================================================


class TestCleanupZero:
    def test_empty_store_cleanup_returns_zero(self):
        store = _store()
        assert store.cleanup_expired_sessions() == 0

    def test_all_active_sessions_cleanup_returns_zero(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_genie_conversation_id(APP_CONV_B, GENIE_CONV_B)
        assert store.cleanup_expired_sessions() == 0


# =============================================================================
# TEST 12: active_session_count excludes expired sessions
# =============================================================================


class TestActiveSessionCount:
    def test_active_count_excludes_expired(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_genie_conversation_id(APP_CONV_B, GENIE_CONV_B)

        # Expire session A
        with store._lock:
            store._sessions[APP_CONV_A].expires_at = _past(1)

        assert store.active_session_count() == 1
        assert store.session_count()        == 2  # raw count includes expired

    def test_active_count_zero_after_all_reset(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.reset_session(APP_CONV_A)

        assert store.active_session_count() == 0


# =============================================================================
# STANDALONE RUNNER
# =============================================================================

# =============================================================================
# TEST 13: remove_session physically removes the session object
# =============================================================================


class TestRemoveSession:
    def test_remove_existing_session_returns_true(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.update_context(
            APP_CONV_A,
            last_intent="AGGREGATION",
            last_entities=["US", "CN"],
            last_user_prompt="show delayed shipments",
        )

        result = store.remove_session(APP_CONV_A)
        assert result is True

    def test_remove_makes_session_unreachable(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_last_message_id(APP_CONV_A, MSG_1)
        store.update_context(
            APP_CONV_A,
            last_intent="AGGREGATION",
            last_entities=["US"],
            last_filters={"country": "US"},
            last_user_prompt="delays by country",
            last_enriched_prompt="Show aggregated delays",
            last_download_key="dk-123",
        )

        store.remove_session(APP_CONV_A)

        # All accessors return None/empty for a removed session
        assert store.get_session(APP_CONV_A) is None
        assert store.get_genie_conversation_id(APP_CONV_A) is None
        assert store.get_last_message_id(APP_CONV_A) is None
        assert store.get_last_download_key(APP_CONV_A) is None
        assert store.get_context_snapshot(APP_CONV_A) == {}

    def test_remove_physically_deletes_from_internal_mapping(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)

        store.remove_session(APP_CONV_A)

        with store._lock:
            assert APP_CONV_A not in store._sessions

    def test_remove_missing_session_returns_false(self):
        store = _store()
        result = store.remove_session("non-existent-id")
        assert result is False

    def test_remove_missing_is_idempotent(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)

        first = store.remove_session(APP_CONV_A)
        second = store.remove_session(APP_CONV_A)

        assert first is True
        assert second is False

    def test_remove_does_not_affect_other_sessions(self):
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_genie_conversation_id(APP_CONV_B, GENIE_CONV_B)

        store.remove_session(APP_CONV_A)

        # B remains intact
        assert store.get_genie_conversation_id(APP_CONV_B) == GENIE_CONV_B
        session_b = store.get_session(APP_CONV_B)
        assert session_b is not None
        assert session_b.genie_conversation_id == GENIE_CONV_B

    def test_concurrent_remove_is_safe(self):
        import threading

        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)

        results = []
        barrier = threading.Barrier(4)

        def _remove():
            barrier.wait()
            r = store.remove_session(APP_CONV_A)
            results.append(r)

        threads = [threading.Thread(target=_remove) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one True (the winner), rest False
        assert results.count(True) == 1
        assert results.count(False) == 3
        assert store.get_session(APP_CONV_A) is None

    def test_remove_context_heavy_session_is_complete(self):
        """Context-heavy session is completely removed — no residual data."""
        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)
        store.set_last_message_id(APP_CONV_A, MSG_1)
        from app.services.genie_session_store import TableExportRecord
        store.update_context(
            APP_CONV_A,
            last_intent="BROAD_LISTING",
            last_entities=["US", "CN", "DE"],
            last_entity_type="country",
            last_filters={"country": "US", "bu": "ION"},
            last_user_prompt="show all shipments",
            last_enriched_prompt="Show all active shipments from US and CN",
            last_download_key="dk-heavy",
            last_export_id="exp-001",
            last_export_status="ready",
            last_export_mode="returned_rows_only",
            last_export_row_count=500,
            last_table_headers=["tracking_id", "origin", "destination"],
            last_row_count=50,
            last_total_row_count=500,
            last_returned_row_count=50,
            latest_table_result=TableExportRecord(
                assistant_message_id="msg-heavy",
                download_key="dk-heavy",
                export_id="exp-001",
                export_status="ready",
                export_mode="returned_rows_only",
                export_row_count=500,
                query_description="All shipments",
                created_at=_now(),
            ),
        )

        store.remove_session(APP_CONV_A)

        # Verify complete removal
        assert store.get_session(APP_CONV_A) is None
        with store._lock:
            assert APP_CONV_A not in store._sessions
        assert store.session_count() == 0

    def test_remove_empty_id_returns_false(self):
        store = _store()
        assert store.remove_session("") is False
        assert store.remove_session("   ") is False

    def test_no_identifier_logged_on_remove(self):
        """Ensure remove_session does not log the conversation ID."""
        import logging

        store = _store()
        store.set_genie_conversation_id(APP_CONV_A, GENIE_CONV_A)

        handler = logging.handlers.MemoryHandler(capacity=100)
        store_logger = logging.getLogger("app.services.genie_session_store")
        store_logger.addHandler(handler)
        store_logger.setLevel(logging.DEBUG)

        try:
            store.remove_session(APP_CONV_A)
            handler.flush()
            for record in handler.buffer:
                msg = record.getMessage()
                assert APP_CONV_A not in msg, f"ID leaked in log: {msg}"
        finally:
            store_logger.removeHandler(handler)


# =============================================================================
# TEST 14: Process-local key log sanitization (caplog-based)
# =============================================================================


class TestProcessLocalKeyLogSanitization:
    """Validates that plc_v1_ process-local conversation keys do not appear
    in any log output from GenieSessionStore operations.

    Uses pytest caplog fixture for reliable log capture.
    """

    @staticmethod
    def _make_plc_key(suffix: str = "001") -> str:
        from app.services.process_local_conversation_key import (
            build_process_local_conversation_key,
        )
        return build_process_local_conversation_key(
            owner_user_id_hash="e" * 64,
            session_id=f"session-sanitize-{suffix}",
            frontend_conversation_id=f"frontend-sanitize-{suffix}",
        )

    def test_set_genie_conversation_id_no_plc_key_in_logs(self, caplog):
        import logging
        store = _store()
        key = self._make_plc_key("set")
        with caplog.at_level(logging.DEBUG):
            store.set_genie_conversation_id(key, "genie-conv-set")
        assert key not in caplog.text
        assert "plc_v1_" not in caplog.text

    def test_remove_session_no_plc_key_in_logs(self, caplog):
        import logging
        store = _store()
        key = self._make_plc_key("rm")
        store.set_genie_conversation_id(key, "genie-conv-rm")
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            store.remove_session(key)
        assert key not in caplog.text
        assert "plc_v1_" not in caplog.text

    def test_reset_genie_mapping_no_plc_key_in_logs(self, caplog):
        import logging
        store = _store()
        key = self._make_plc_key("reset")
        store.set_genie_conversation_id(key, "genie-conv-reset")
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            store.reset_genie_mapping(key)
        assert key not in caplog.text
        assert "plc_v1_" not in caplog.text

    def test_remove_nonexistent_no_plc_key_in_logs(self, caplog):
        import logging
        store = _store()
        key = self._make_plc_key("gone")
        with caplog.at_level(logging.DEBUG):
            store.remove_session(key)
        assert key not in caplog.text
        assert "plc_v1_" not in caplog.text

    def test_multiple_operations_no_plc_key_leakage(self, caplog):
        import logging
        store = _store()
        keys = [self._make_plc_key(f"{i:03d}") for i in range(5)]
        with caplog.at_level(logging.DEBUG):
            for k in keys:
                store.set_genie_conversation_id(k, f"gc-{k[-4:]}")
            for k in keys:
                store.remove_session(k)
        log_text = caplog.text
        for k in keys:
            assert k not in log_text
        assert "plc_v1_" not in log_text


if __name__ == "__main__":
    import traceback

    test_classes = [
        TestSetGetGenieConversationId,
        TestSetGetLastMessageId,
        TestGetSession,
        TestResetSession,
        TestExpiredSession,
        TestCleanup,
        TestSerializeDeserialize,
        TestSessionIsolation,
        TestUpdatedAtRefreshes,
        TestMissingSession,
        TestCleanupZero,
        TestActiveSessionCount,
        TestRemoveSession,
    ]

    passed = failed = total = 0
    print("=" * 70)
    print("PHASE G2: genie_session_store.py unit tests")
    print("=" * 70)

    for cls in test_classes:
        instance = cls()
        for method_name in [m for m in dir(instance) if m.startswith("test_")]:
            total += 1
            label = f"{cls.__name__}.{method_name}"
            try:
                getattr(instance, method_name)()
                print(f"  PASS  {label}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {label}: {e}")
                traceback.print_exc()
                failed += 1

    print()
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if failed:
        import sys
        sys.exit(1)
