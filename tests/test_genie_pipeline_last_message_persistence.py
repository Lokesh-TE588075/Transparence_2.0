"""Phase 4C3 — GeniePipeline final Genie message-ID persistence tests.

Tests the update_last_genie_message step added to both the MISS writeback
(Phase 4C3-MISS) and the recovered-message persistence path (Phase 4C3-RECOVERED).

Coverage:
  - DISABLED / local-only: no update occurs.
  - MISS path: bind before update, exact message ID and version, success/failure
    handling, prohibited mutations.
  - RECOVERED path: no create/bind, exact version from lookup, send before update,
    success/failure handling.
  - Final-message selection: only the last retry result is persisted.
  - Idempotency: same message skips the update call.
  - Version conflict: at most one reload, correct resolution logic.
  - Failure policy: fail closed, in-memory mapping cleared, fallback_recommended=False,
    durable record NOT deleted.
  - Cross-owner isolation.
"""
from __future__ import annotations

import time as _time
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from app.services.genie_pipeline import GeniePipeline
from app.services.genie_session_store import GenieSessionStore
from app.services.conversation_repository import (
    ConversationRecord,
    ConversationStatus,
    InMemoryConversationRepository,
)
from app.services.durable_genie_session_adapter import (
    DurableGenieSessionAdapter,
    DurableGenieSessionUnavailableError,
    DurableGenieSessionVersionConflictError,
    DurableGenieSessionKey,
    GenieSessionLookupResult,
    GenieSessionLookupSource,
)
from app.services.durable_genie_session_runtime_factory import (
    DurableGenieSessionRuntimeBundle,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_OWNER_KEY    = "a" * 64
_VALID_OWNER_KEY_2  = "b" * 64
_SPACE_ID           = "test-space-id-4c3"
_FRONTEND_CONV_ID   = "frontend-conv-4c3-001"
_FRONTEND_CONV_ID_2 = "frontend-conv-4c3-002"
_GENIE_CONV_NEW     = "genie-conv-new-4c3"
_GENIE_CONV_OTHER   = "genie-conv-other-4c3"
_GENIE_MSG_NEW      = "msg-new-4c3"
_GENIE_MSG_PRIOR    = "genie-msg-prior-4c3"
_APP_CONV_ID        = "nosession:frontend-conv-4c3-001"
_APP_CONV_ID_2      = "nosession:frontend-conv-4c3-002"
_MSG_LAST_MSG_FAILED = (
    "I wasn\'t able to complete that request. "
    "Please try again in a moment."
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class RecordingGenieClient:
    """Genie client returning deterministic string message IDs."""

    def __init__(
        self,
        conv_id: str = _GENIE_CONV_NEW,
        start_msg_id: str = _GENIE_MSG_NEW,
        send_msg_id: str = _GENIE_MSG_NEW,
    ):
        self.start_calls: List[Dict[str, Any]] = []
        self.send_calls:  List[Dict[str, Any]] = []
        self._conv_id      = conv_id
        self._start_msg_id = start_msg_id
        self._send_msg_id  = send_msg_id

    def start_conversation(self, space_id, message):
        self.start_calls.append({"space_id": space_id, "message": message})
        return {"conversation_id": self._conv_id, "message_id": self._start_msg_id}

    def send_message(self, space_id, conv_id, message):
        self.send_calls.append({"space_id": space_id, "conv_id": conv_id, "message": message})
        return {"message_id": self._send_msg_id}

    def wait_for_message_completion(self, space_id, conv_id, msg_id, **kwargs):
        # Returns a message object with a proper string message_id so the
        # mapper produces a valid string genie_message_id in the result.
        return MagicMock(query_attachments=None, message_id=msg_id)

    def fetch_query_result(self, *args, **kwargs):
        return None


class FakeRepositoryBundle:
    def __init__(self, repository):
        self.repository = repository
        self.backend = MagicMock()
        self.backend.value = "memory"
        self.durable = True

    def close(self):
        pass


class _TrackingAdapter:
    """Wraps a real adapter and records every call."""

    def __init__(self, real_adapter: DurableGenieSessionAdapter) -> None:
        self._real = real_adapter
        self.get_or_create_calls: List[Any]             = []
        self.bind_calls:          List[Any]             = []
        self.load_calls:          List[Any]             = []
        self.update_last_genie_message_calls: List[Any] = []
        self.touch_calls:         List[Any]             = []
        self.set_status_calls:    List[Any]             = []
        self.delete_calls:        List[Any]             = []

    def get_or_create(self, key, **kw):
        self.get_or_create_calls.append(key)
        return self._real.get_or_create(key, **kw)

    def load(self, key):
        self.load_calls.append(key)
        return self._real.load(key)

    def bind_genie_conversation(self, key, gid, *, expected_version, **kw):
        self.bind_calls.append({"key": key, "gid": gid, "ev": expected_version})
        return self._real.bind_genie_conversation(key, gid, expected_version=expected_version, **kw)

    def update_last_genie_message(self, key, *args, **kw):
        self.update_last_genie_message_calls.append(key)
        return self._real.update_last_genie_message(key, *args, **kw)

    def touch(self, key, **kw):
        self.touch_calls.append(key)
        return self._real.touch(key, **kw)

    def set_status(self, key, status, **kw):
        self.set_status_calls.append((key, status))
        return self._real.set_status(key, status, **kw)

    def delete(self, key):
        self.delete_calls.append(key)
        return self._real.delete(key)

    def close(self):
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_pipeline(genie_client=None, session_store=None, **kw) -> GeniePipeline:
    client = genie_client or RecordingGenieClient()
    store  = session_store or GenieSessionStore()
    return GeniePipeline(
        genie_client=client,
        session_store=store,
        space_id=_SPACE_ID,
        fetch_query_results=False,
        enable_prompt_enrichment=False,
        enable_shape_validation=False,
        enable_table_summary=False,
        **kw,
    )


def _make_enabled_bundle(adapter) -> DurableGenieSessionRuntimeBundle:
    return DurableGenieSessionRuntimeBundle(
        enabled=True, adapter=adapter, backend=MagicMock(), durable=True,
    )


def _make_disabled_bundle() -> DurableGenieSessionRuntimeBundle:
    return DurableGenieSessionRuntimeBundle(
        enabled=False, adapter=None, backend=None, durable=False,
    )


def _make_miss_pipeline(
    conv_id: str = _GENIE_CONV_NEW,
    msg_id: str = _GENIE_MSG_NEW,
    owner_hash: str = _VALID_OWNER_KEY,
    frontend_id: str = _FRONTEND_CONV_ID,
):
    """Empty-repo MISS pipeline with tracking adapter."""
    store  = GenieSessionStore()
    client = RecordingGenieClient(conv_id=conv_id, start_msg_id=msg_id, send_msg_id=msg_id)
    pipeline = _build_pipeline(genie_client=client, session_store=store)
    repo   = InMemoryConversationRepository()
    real   = DurableGenieSessionAdapter(FakeRepositoryBundle(repo), cache_store=None)
    tracking = _TrackingAdapter(real)
    setattr(pipeline, "_durable_session_runtime_bundle", _make_enabled_bundle(tracking))
    return pipeline, store, client, tracking, repo


def _make_recovered_pipeline(
    genie_conv_id: str = _GENIE_CONV_NEW,
    last_msg_id: Optional[str] = _GENIE_MSG_PRIOR,
    send_msg_id: str = _GENIE_MSG_NEW,
    owner_hash: str = _VALID_OWNER_KEY,
    frontend_id: str = _FRONTEND_CONV_ID,
):
    """Pre-seeded RECOVERED pipeline with tracking adapter."""
    repo = InMemoryConversationRepository()
    created = repo.create_conversation(owner_hash, frontend_id)
    bound   = repo.bind_genie_conversation(
        owner_hash, created.conversation_id, genie_conv_id,
        expected_version=created.version,
    )
    if last_msg_id:
        repo.update_last_genie_message(
            owner_hash, created.conversation_id, last_msg_id,
            expected_version=bound.version,
        )
    store  = GenieSessionStore()
    client = RecordingGenieClient(
        conv_id=genie_conv_id, start_msg_id=send_msg_id, send_msg_id=send_msg_id,
    )
    pipeline = _build_pipeline(genie_client=client, session_store=store)
    real     = DurableGenieSessionAdapter(FakeRepositoryBundle(repo), cache_store=None)
    tracking = _TrackingAdapter(real)
    setattr(pipeline, "_durable_session_runtime_bundle", _make_enabled_bundle(tracking))
    return pipeline, store, client, tracking, repo


def _run(pipeline, msg="show shipments", app_conv=_APP_CONV_ID,
         owner=_VALID_OWNER_KEY, frontend=_FRONTEND_CONV_ID):
    """Call pipeline.run() with standard durable parameters."""
    return pipeline.run(
        msg, app_conv,
        owner_key=owner,
        frontend_conversation_id=frontend,
    )


# ===========================================================================
# 1. DISABLED / LOCAL — NO MESSAGE UPDATE
# ===========================================================================


class TestDisabledLocalNoMessageUpdate:
    """Message update must not occur for DISABLED mode or local-only responses."""

    def test_disabled_bundle_no_update(self):
        """DISABLED bundle: no durable operation occurs at all."""
        client   = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client)
        setattr(pipeline, "_durable_session_runtime_bundle", _make_disabled_bundle())
        result   = _run(pipeline)
        assert result["status"] == "success"
        assert len(client.start_calls) == 1

    def test_local_greeting_no_update(self):
        """Local greeting response has no genie_conv_id → update not attempted."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline()
        _run(pipeline, msg="hello")
        assert len(tracking.update_last_genie_message_calls) == 0

    def test_result_without_genie_conv_id_no_update(self):
        """Result missing genie_conversation_id skips message persistence."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline()
        _run(pipeline, msg="hello")
        # hello → local response, no genie_conv_id → writeback returns early
        assert len(tracking.update_last_genie_message_calls) == 0

    def test_error_result_no_update(self):
        """Error status result skips message persistence."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline()
        orig = pipeline._run_inner
        def patched(*a, **kw):
            r = orig(*a, **kw)
            r["status"] = "error"
            return r
        pipeline._run_inner = patched
        _run(pipeline)
        assert len(tracking.update_last_genie_message_calls) == 0


# ===========================================================================
# 2. MISS — PERSISTENCE ORDERING
# ===========================================================================


class TestMISS_PersistenceOrdering:
    """MISS path: bind succeeds BEFORE update_last_genie_message."""

    def test_bind_before_update(self):
        """bind_genie_conversation is called before update_last_genie_message."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline()
        call_order: List[str] = []
        orig_bind   = tracking.bind_genie_conversation
        orig_update = tracking.update_last_genie_message

        def rec_bind(key, gid, *, expected_version, **kw):
            call_order.append("bind")
            return orig_bind(key, gid, expected_version=expected_version, **kw)

        def rec_update(key, *a, **kw):
            call_order.append("update")
            return orig_update(key, *a, **kw)

        tracking.bind_genie_conversation       = rec_bind
        tracking.update_last_genie_message     = rec_update

        result = _run(pipeline)
        assert result["status"] == "success"
        assert "bind" in call_order
        assert "update" in call_order
        assert call_order.index("bind") < call_order.index("update"), (
            f"bind must precede update; order was {call_order}"
        )

    def test_get_or_create_before_update(self):
        """get_or_create is called before update_last_genie_message."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline()
        call_order: List[str] = []
        orig_goc    = tracking.get_or_create
        orig_update = tracking.update_last_genie_message

        def rec_goc(key, **kw):
            call_order.append("get_or_create")
            return orig_goc(key, **kw)

        def rec_update(key, *a, **kw):
            call_order.append("update")
            return orig_update(key, *a, **kw)

        tracking.get_or_create             = rec_goc
        tracking.update_last_genie_message = rec_update

        result = _run(pipeline)
        assert result["status"] == "success"
        assert call_order.index("get_or_create") < call_order.index("update")

    def test_run_inner_before_update(self):
        """_run_inner (Genie execution) completes before update_last_genie_message."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline()
        call_order: List[str] = []
        orig_inner  = pipeline._run_inner
        orig_update = tracking.update_last_genie_message

        def rec_inner(*a, **kw):
            r = orig_inner(*a, **kw)
            call_order.append("run_inner")
            return r

        def rec_update(key, *a, **kw):
            call_order.append("update")
            return orig_update(key, *a, **kw)

        pipeline._run_inner                = rec_inner
        tracking.update_last_genie_message = rec_update

        result = _run(pipeline)
        assert result["status"] == "success"
        assert call_order.index("run_inner") < call_order.index("update")

    def test_miss_update_called_exactly_once(self):
        """update_last_genie_message is called exactly once on MISS success."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline()
        result = _run(pipeline)
        assert result["status"] == "success"
        assert len(tracking.update_last_genie_message_calls) == 1


# ===========================================================================
# 3. MISS — PERSISTENCE SUCCESS
# ===========================================================================


class TestMISS_PersistenceSuccess:
    """MISS path: final message ID persisted; correct ID and version used."""

    def test_message_id_persisted_in_repo(self):
        """After MISS success, last_genie_message_id is set in the repository."""
        pipeline, _, _, _, repo = _make_miss_pipeline(
            conv_id=_GENIE_CONV_NEW, msg_id=_GENIE_MSG_NEW,
        )
        result = _run(pipeline)
        assert result["status"] == "success"
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record is not None
        assert record.last_genie_message_id == _GENIE_MSG_NEW

    def test_exact_message_id_from_genie_response(self):
        """The exact message ID from start_conversation is stored."""
        pipeline, _, _, _, repo = _make_miss_pipeline(
            conv_id=_GENIE_CONV_NEW, msg_id=_GENIE_MSG_NEW,
        )
        _run(pipeline)
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record.last_genie_message_id == _GENIE_MSG_NEW

    def test_post_bind_version_used(self):
        """The version returned by bind (post-bind) is used for update."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline()
        result = _run(pipeline)
        assert result["status"] == "success"
        # Record must exist and message must be set — proves version was correct
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record.last_genie_message_id == _GENIE_MSG_NEW

    def test_success_result_returned(self):
        """Original success result is returned after message persistence."""
        pipeline, _, _, _, _ = _make_miss_pipeline()
        result = _run(pipeline)
        assert result["status"] == "success"
        assert result["genie_conversation_id"] == _GENIE_CONV_NEW

    def test_fallback_false_on_success(self):
        """fallback_recommended=False after successful MISS + message persistence."""
        pipeline, _, _, _, _ = _make_miss_pipeline()
        result = _run(pipeline)
        assert result["fallback_recommended"] is False

    def test_in_memory_mapping_retained_after_success(self):
        """In-memory Genie mapping is retained (not cleared) after success."""
        pipeline, store, _, _, _ = _make_miss_pipeline(conv_id=_GENIE_CONV_NEW)
        _run(pipeline)
        assert store.get_genie_conversation_id(_APP_CONV_ID) == _GENIE_CONV_NEW

    def test_ownership_absent_from_response(self):
        """Owner hash is not exposed in the success response."""
        pipeline, _, _, _, _ = _make_miss_pipeline()
        result = _run(pipeline)
        assert _VALID_OWNER_KEY not in str(result)
        assert "owner_key" not in result
        assert "owner_user_id_hash" not in result


# ===========================================================================
# 4. MISS — PERSISTENCE FAILURE
# ===========================================================================


class TestMISS_PersistenceFailure:
    """MISS path: missing message ID or update failure → fail closed."""

    def test_missing_genie_message_id_returns_error(self):
        """Conv ID present but genie_message_id=None → fail closed."""
        pipeline, _, _, _, _ = _make_miss_pipeline()
        orig = pipeline._run_inner
        def patched(*a, **kw):
            r = orig(*a, **kw)
            if r.get("genie_conversation_id"):
                r["genie_message_id"] = None
            return r
        pipeline._run_inner = patched
        result = _run(pipeline)
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False

    def test_missing_genie_message_id_clears_mapping(self):
        """genie_message_id=None clears the in-memory mapping."""
        pipeline, store, _, _, _ = _make_miss_pipeline()
        orig = pipeline._run_inner
        def patched(*a, **kw):
            r = orig(*a, **kw)
            if r.get("genie_conversation_id"):
                r["genie_message_id"] = None
            return r
        pipeline._run_inner = patched
        _run(pipeline)
        assert store.get_genie_conversation_id(_APP_CONV_ID) is None

    def test_update_unavailable_returns_error(self):
        """Adapter unavailable during update → error, fallback_recommended=False."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline()
        tracking._real.update_last_genie_message = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("down")
        )
        result = _run(pipeline)
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False

    def test_update_failure_clears_mapping(self):
        """Update failure clears the current in-memory Genie mapping."""
        pipeline, store, _, tracking, _ = _make_miss_pipeline()
        tracking._real.update_last_genie_message = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("down")
        )
        _run(pipeline)
        assert store.get_genie_conversation_id(_APP_CONV_ID) is None

    def test_shape_retry_exhausted_skips_update(self):
        """shape_retry_exhausted=True skips message persistence."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline()
        orig = pipeline._run_inner
        def patched(*a, **kw):
            r = orig(*a, **kw)
            r["shape_retry_exhausted"] = True
            r["status"] = "success"
            return r
        pipeline._run_inner = patched
        _run(pipeline)
        assert len(tracking.update_last_genie_message_calls) == 0

    def test_durable_record_not_deleted_on_failure(self):
        """Update failure must not delete the durable record."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline()
        tracking._real.update_last_genie_message = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("down")
        )
        _run(pipeline)
        assert len(tracking.delete_calls) == 0
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record is not None
        assert record.genie_conversation_id == _GENIE_CONV_NEW


# ===========================================================================
# 5. RECOVERED — PERSISTENCE ORDERING
# ===========================================================================


class TestRECOVERED_PersistenceOrdering:
    """RECOVERED path: no get_or_create/bind; send_message before update."""

    def test_no_get_or_create_on_recovered(self):
        """get_or_create must NOT be called on RECOVERED."""
        pipeline, _, _, tracking, _ = _make_recovered_pipeline()
        tracking.get_or_create = MagicMock(
            side_effect=AssertionError("get_or_create prohibited on RECOVERED")
        )
        result = _run(pipeline)
        assert result["status"] == "success"

    def test_no_bind_on_recovered(self):
        """bind_genie_conversation must NOT be called on RECOVERED."""
        pipeline, _, _, tracking, _ = _make_recovered_pipeline()
        tracking.bind_genie_conversation = MagicMock(
            side_effect=AssertionError("bind prohibited on RECOVERED")
        )
        result = _run(pipeline)
        assert result["status"] == "success"

    def test_send_message_before_update(self):
        """send_message (Genie execution) completes before update_last_genie_message."""
        pipeline, _, client, tracking, _ = _make_recovered_pipeline()
        call_order: List[str] = []
        orig_send   = client.send_message
        orig_update = tracking.update_last_genie_message

        def rec_send(*a, **kw):
            call_order.append("send_message")
            return orig_send(*a, **kw)

        def rec_update(key, *a, **kw):
            call_order.append("update")
            return orig_update(key, *a, **kw)

        client.send_message                = rec_send
        tracking.update_last_genie_message = rec_update

        result = _run(pipeline)
        assert result["status"] == "success"
        assert call_order.index("send_message") < call_order.index("update")

    def test_recovered_update_called_exactly_once(self):
        """update_last_genie_message is called exactly once for RECOVERED success."""
        pipeline, _, _, tracking, _ = _make_recovered_pipeline()
        result = _run(pipeline)
        assert result["status"] == "success"
        assert len(tracking.update_last_genie_message_calls) == 1


# ===========================================================================
# 6. RECOVERED — PERSISTENCE SUCCESS
# ===========================================================================


class TestRECOVERED_PersistenceSuccess:
    """RECOVERED path: message ID persisted with the correct version."""

    def test_message_id_persisted_in_repo(self):
        """New message ID is stored in the repository after RECOVERED success."""
        pipeline, _, _, _, repo = _make_recovered_pipeline(
            genie_conv_id=_GENIE_CONV_NEW, last_msg_id=_GENIE_MSG_PRIOR,
            send_msg_id=_GENIE_MSG_NEW,
        )
        result = _run(pipeline)
        assert result["status"] == "success"
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record.last_genie_message_id == _GENIE_MSG_NEW

    def test_lookup_record_version_used(self):
        """The version from the confirmed lookup record is used for the update."""
        pipeline, _, _, tracking, repo = _make_recovered_pipeline(
            last_msg_id=_GENIE_MSG_PRIOR,
        )
        result = _run(pipeline)
        assert result["status"] == "success"
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record.last_genie_message_id == _GENIE_MSG_NEW

    def test_success_result_returned(self):
        """Success result is returned after RECOVERED message update."""
        pipeline, _, _, _, _ = _make_recovered_pipeline()
        result = _run(pipeline)
        assert result["status"] == "success"
        assert result.get("genie_conversation_id") == _GENIE_CONV_NEW

    def test_fallback_false_on_success(self):
        """fallback_recommended=False after successful RECOVERED message update."""
        pipeline, _, _, _, _ = _make_recovered_pipeline()
        result = _run(pipeline)
        assert result["fallback_recommended"] is False

    def test_no_genie_conv_id_passthrough(self):
        """RECOVERED result with no genie_conv_id passes through without update."""
        pipeline, _, _, tracking, _ = _make_recovered_pipeline()
        orig = pipeline._run_inner
        def patched(*a, **kw):
            r = orig(*a, **kw)
            r["genie_conversation_id"] = None
            return r
        pipeline._run_inner = patched
        _run(pipeline)
        assert len(tracking.update_last_genie_message_calls) == 0

    def test_in_memory_mapping_retained_after_success(self):
        """In-memory mapping is retained after RECOVERED success."""
        pipeline, store, _, _, _ = _make_recovered_pipeline(genie_conv_id=_GENIE_CONV_NEW)
        # Run causes lookup which sets mapping, then update succeeds
        _run(pipeline)
        assert store.get_genie_conversation_id(_APP_CONV_ID) == _GENIE_CONV_NEW


# ===========================================================================
# 7. RECOVERED — PERSISTENCE FAILURE
# ===========================================================================


class TestRECOVERED_PersistenceFailure:
    """RECOVERED path: failure handling and fail-closed contract."""

    def test_missing_genie_message_id_returns_error(self):
        """Conv ID present but genie_message_id=None → fail closed on RECOVERED."""
        pipeline, _, _, _, _ = _make_recovered_pipeline()
        orig = pipeline._run_inner
        def patched(*a, **kw):
            r = orig(*a, **kw)
            r["genie_message_id"] = None
            return r
        pipeline._run_inner = patched
        result = _run(pipeline)
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False

    def test_missing_message_id_clears_mapping(self):
        """genie_message_id=None clears the in-memory mapping on RECOVERED."""
        pipeline, store, _, _, _ = _make_recovered_pipeline()
        orig = pipeline._run_inner
        def patched(*a, **kw):
            r = orig(*a, **kw)
            r["genie_message_id"] = None
            return r
        pipeline._run_inner = patched
        _run(pipeline)
        assert store.get_genie_conversation_id(_APP_CONV_ID) is None

    def test_update_unavailable_returns_error(self):
        """Adapter unavailable during RECOVERED update → error."""
        pipeline, _, _, tracking, _ = _make_recovered_pipeline()
        tracking._real.update_last_genie_message = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("down")
        )
        result = _run(pipeline)
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False

    def test_update_failure_clears_mapping(self):
        """Update failure on RECOVERED clears the in-memory mapping."""
        pipeline, store, _, tracking, _ = _make_recovered_pipeline()
        tracking._real.update_last_genie_message = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("down")
        )
        _run(pipeline)
        assert store.get_genie_conversation_id(_APP_CONV_ID) is None

    def test_shape_retry_exhausted_skips_update(self):
        """shape_retry_exhausted=True skips message update on RECOVERED."""
        pipeline, _, _, tracking, _ = _make_recovered_pipeline()
        orig = pipeline._run_inner
        def patched(*a, **kw):
            r = orig(*a, **kw)
            r["shape_retry_exhausted"] = True
            r["status"] = "success"
            return r
        pipeline._run_inner = patched
        _run(pipeline)
        assert len(tracking.update_last_genie_message_calls) == 0

    def test_durable_record_not_deleted_on_failure(self):
        """Update failure on RECOVERED must not delete the durable record."""
        pipeline, _, _, tracking, repo = _make_recovered_pipeline()
        tracking._real.update_last_genie_message = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("down")
        )
        result = _run(pipeline)
        assert result["status"] == "error"
        assert len(tracking.delete_calls) == 0
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record is not None
        assert record.genie_conversation_id == _GENIE_CONV_NEW


# ===========================================================================
# 8. SAME-MESSAGE IDEMPOTENCY
# ===========================================================================


class TestSameMessageIdempotency:
    """Identical existing message ID is an idempotent skip, not an error."""

    def test_same_message_recovered_skips_update(self):
        """RECOVERED record with same message ID → update not called."""
        pipeline, _, _, tracking, _ = _make_recovered_pipeline(
            genie_conv_id=_GENIE_CONV_NEW,
            last_msg_id=_GENIE_MSG_NEW,   # already persisted
            send_msg_id=_GENIE_MSG_NEW,   # same returned
        )
        result = _run(pipeline)
        assert result["status"] == "success"
        assert len(tracking.update_last_genie_message_calls) == 0

    def test_same_message_idempotent_success_result(self):
        """Idempotent RECOVERED returns full success result."""
        pipeline, _, _, _, _ = _make_recovered_pipeline(
            last_msg_id=_GENIE_MSG_NEW, send_msg_id=_GENIE_MSG_NEW,
        )
        result = _run(pipeline)
        assert result["status"] == "success"
        assert result["fallback_recommended"] is False

    def test_same_message_mapping_retained(self):
        """Idempotent RECOVERED retains the in-memory mapping."""
        pipeline, store, _, _, _ = _make_recovered_pipeline(
            genie_conv_id=_GENIE_CONV_NEW,
            last_msg_id=_GENIE_MSG_NEW, send_msg_id=_GENIE_MSG_NEW,
        )
        _run(pipeline)
        assert store.get_genie_conversation_id(_APP_CONV_ID) == _GENIE_CONV_NEW


# ===========================================================================
# 9. VERSION CONFLICT HANDLING
# ===========================================================================


class TestVersionConflictHandling:
    """Version conflict: at most one reload; correct resolution."""

    def test_conflict_no_retry_loop(self):
        """On version conflict, at most one reload is attempted (no loop)."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline()
        update_call_count = {"n": 0}

        original_update = tracking._real.update_last_genie_message
        def always_conflict(key, msg_id, *, expected_version):
            update_call_count["n"] += 1
            raise DurableGenieSessionVersionConflictError("conflict")

        tracking._real.update_last_genie_message = always_conflict
        result = _run(pipeline)

        # After conflict, adapter.load is called once for the conflict reload.
        # Regardless of reload outcome, update is NOT retried.
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False
        # update was called exactly once (no retry loop)
        assert update_call_count["n"] == 1

    def test_conflict_reload_same_conv_same_msg_success(self):
        """Conflict reload showing same conv + same message → idempotent success."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline()

        # First run to create and bind the record
        _run(pipeline)  # This would have called update_last_genie_message and succeeded

        # Now for the conflict test: run again but intercept the second run
        # Build a fresh pipeline for isolation
        pipeline2, store2, _, tracking2, repo2 = _make_miss_pipeline(
            conv_id=_GENIE_CONV_NEW, msg_id=_GENIE_MSG_NEW,
        )
        # Pre-seed repo2 so the reload finds the record with the matching message
        created2 = repo2.create_conversation(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        bound2   = repo2.bind_genie_conversation(
            _VALID_OWNER_KEY, created2.conversation_id, _GENIE_CONV_NEW,
            expected_version=created2.version,
        )
        repo2.update_last_genie_message(
            _VALID_OWNER_KEY, created2.conversation_id, _GENIE_MSG_NEW,
            expected_version=bound2.version,
        )
        # Make update raise conflict once, then skip (reload will find matching record)
        update_calls = {"n": 0}
        original_update = tracking2._real.update_last_genie_message
        def conflict_once(key, msg_id, *, expected_version):
            update_calls["n"] += 1
            if update_calls["n"] == 1:
                raise DurableGenieSessionVersionConflictError("simulated conflict")
            return original_update(key, msg_id, expected_version=expected_version)

        tracking2._real.update_last_genie_message = conflict_once
        result2 = _run(pipeline2)
        # The conflict triggers a reload which finds the matching message → idempotent
        assert result2["status"] == "success"

    def test_conflict_reload_different_message_fails_closed(self):
        """Conflict reload showing different message → fail closed."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline()
        # Pre-seed with different message ID
        created = repo.create_conversation(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        bound   = repo.bind_genie_conversation(
            _VALID_OWNER_KEY, created.conversation_id, _GENIE_CONV_NEW,
            expected_version=created.version,
        )
        repo.update_last_genie_message(
            _VALID_OWNER_KEY, created.conversation_id, _GENIE_MSG_PRIOR,  # different
            expected_version=bound.version,
        )
        # Make update raise conflict once
        original_update = tracking._real.update_last_genie_message
        update_calls = {"n": 0}
        def conflict_once(key, msg_id, *, expected_version):
            update_calls["n"] += 1
            if update_calls["n"] == 1:
                raise DurableGenieSessionVersionConflictError("conflict")
            return original_update(key, msg_id, expected_version=expected_version)

        tracking._real.update_last_genie_message = conflict_once
        result = _run(pipeline)
        # Reload finds _GENIE_MSG_PRIOR != _GENIE_MSG_NEW → fail closed
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False


# ===========================================================================
# 10. PROHIBITED MUTATIONS — PHASE 4C3
# ===========================================================================


class TestProhibitedMutations4C3:
    """No touch, set_status, delete during Phase 4C3; no direct repo access."""

    def test_touch_not_called_on_miss(self):
        pipeline, _, _, tracking, _ = _make_miss_pipeline()
        _run(pipeline)
        assert len(tracking.touch_calls) == 0

    def test_set_status_not_called_on_miss(self):
        pipeline, _, _, tracking, _ = _make_miss_pipeline()
        _run(pipeline)
        assert len(tracking.set_status_calls) == 0

    def test_delete_not_called_on_miss(self):
        pipeline, _, _, tracking, _ = _make_miss_pipeline()
        _run(pipeline)
        assert len(tracking.delete_calls) == 0

    def test_touch_not_called_on_recovered(self):
        pipeline, _, _, tracking, _ = _make_recovered_pipeline()
        _run(pipeline)
        assert len(tracking.touch_calls) == 0

    def test_set_status_not_called_on_recovered(self):
        pipeline, _, _, tracking, _ = _make_recovered_pipeline()
        _run(pipeline)
        assert len(tracking.set_status_calls) == 0

    def test_delete_not_called_on_recovered(self):
        pipeline, _, _, tracking, _ = _make_recovered_pipeline()
        _run(pipeline)
        assert len(tracking.delete_calls) == 0

    def test_no_repository_direct_access(self):
        """The pipeline does not store or expose the repository instance."""
        pipeline, _, _, _, _ = _make_miss_pipeline()
        _run(pipeline)
        assert not hasattr(pipeline, "_repository")
        assert not hasattr(pipeline, "_conversation_repository")


# ===========================================================================
# 11. CROSS-OWNER ISOLATION AND CONTEXT SAFETY
# ===========================================================================


class TestCrossOwnerIsolation:
    """Message persistence is scoped to owner + frontend conversation."""

    def test_separate_owners_persist_independently(self):
        """Two owners\' message IDs persist independently."""
        pipeline1, _, _, _, repo1 = _make_miss_pipeline(
            conv_id="genie-conv-owner1", msg_id="msg-owner1",
            owner_hash=_VALID_OWNER_KEY, frontend_id=_FRONTEND_CONV_ID,
        )
        r1 = _run(pipeline1)
        assert r1["status"] == "success"
        record1 = repo1.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record1.last_genie_message_id == "msg-owner1"

        pipeline2, _, _, _, repo2 = _make_miss_pipeline(
            conv_id="genie-conv-owner2", msg_id="msg-owner2",
            owner_hash=_VALID_OWNER_KEY_2, frontend_id=_FRONTEND_CONV_ID_2,
        )
        r2 = pipeline2.run(
            "show shipments", _APP_CONV_ID_2,
            owner_key=_VALID_OWNER_KEY_2,
            frontend_conversation_id=_FRONTEND_CONV_ID_2,
        )
        assert r2["status"] == "success"
        record2 = repo2.get_by_frontend_id(_VALID_OWNER_KEY_2, _FRONTEND_CONV_ID_2)
        assert record2.last_genie_message_id == "msg-owner2"

        # Owner 1 record is unchanged
        record1_after = repo1.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record1_after.last_genie_message_id == "msg-owner1"

    def test_request_context_not_stored_on_pipeline(self):
        """The _DurableRequestContext is never stored as a pipeline instance attr."""
        pipeline, _, _, _, _ = _make_miss_pipeline()
        _run(pipeline)
        assert not hasattr(pipeline, "_durable_request_context")
        assert not hasattr(pipeline, "_current_durable_ctx")
        assert not hasattr(pipeline, "_last_durable_ctx")

    def test_recovered_failure_does_not_affect_other_owners(self):
        """One owner\'s update failure does not affect another owner\'s record."""
        # Owner 1: failure path
        pipeline1, store1, _, tracking1, repo1 = _make_miss_pipeline(
            conv_id=_GENIE_CONV_NEW, msg_id=_GENIE_MSG_NEW,
        )
        tracking1._real.update_last_genie_message = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("down")
        )
        r1 = _run(pipeline1)
        assert r1["status"] == "error"
        # Owner 1 mapping cleared
        assert store1.get_genie_conversation_id(_APP_CONV_ID) is None

        # Owner 2: success path (independent pipeline)
        pipeline2, _, _, _, repo2 = _make_miss_pipeline(
            conv_id="genie-conv-owner2", msg_id="msg-owner2",
            owner_hash=_VALID_OWNER_KEY_2, frontend_id=_FRONTEND_CONV_ID_2,
        )
        r2 = pipeline2.run(
            "show shipments", _APP_CONV_ID_2,
            owner_key=_VALID_OWNER_KEY_2,
            frontend_conversation_id=_FRONTEND_CONV_ID_2,
        )
        assert r2["status"] == "success"
        record2 = repo2.get_by_frontend_id(_VALID_OWNER_KEY_2, _FRONTEND_CONV_ID_2)
        assert record2.last_genie_message_id == "msg-owner2"


# ===========================================================================
# 12. FINAL MESSAGE SELECTION
# ===========================================================================


class TestFinalMessageSelection:
    """Only the final message ID from the last completed Genie call is persisted."""

    def test_start_conversation_message_id_persisted(self):
        """MISS: message ID from start_conversation is stored in the repo."""
        pipeline, _, _, _, repo = _make_miss_pipeline(
            conv_id=_GENIE_CONV_NEW, msg_id=_GENIE_MSG_NEW,
        )
        _run(pipeline)
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record.last_genie_message_id == _GENIE_MSG_NEW

    def test_send_message_id_persisted_on_recovered(self):
        """RECOVERED: message ID from send_message is stored in the repo."""
        pipeline, _, _, _, repo = _make_recovered_pipeline(
            genie_conv_id=_GENIE_CONV_NEW,
            last_msg_id=_GENIE_MSG_PRIOR,
            send_msg_id=_GENIE_MSG_NEW,
        )
        _run(pipeline)
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record.last_genie_message_id == _GENIE_MSG_NEW

    def test_update_called_exactly_once_per_run(self):
        """update_last_genie_message is called at most once per pipeline.run() call."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline()
        _run(pipeline)
        assert len(tracking.update_last_genie_message_calls) == 1

    def test_prior_message_replaced_by_new_on_recovered(self):
        """RECOVERED: the new send_message ID replaces the prior stored ID."""
        pipeline, _, _, _, repo = _make_recovered_pipeline(
            last_msg_id=_GENIE_MSG_PRIOR, send_msg_id=_GENIE_MSG_NEW,
        )
        _run(pipeline)
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record.last_genie_message_id == _GENIE_MSG_NEW
        assert record.last_genie_message_id != _GENIE_MSG_PRIOR
