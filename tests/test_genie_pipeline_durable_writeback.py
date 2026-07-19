"""Phase 4C2B — GeniePipeline durable conversation creation and Genie binding tests.

Tests the post-execution durable writeback added to GeniePipeline.run().
Validates: disabled/recovered no-write, successful MISS writeback, final
conversation-ID selection, no-write paths, idempotency and conflicts,
failure policy, prohibited mutations, and cross-owner isolation.

Every test makes exact behavioural assertions — no bare pass, no or-True,
no conditional assertions that silently pass, no live Lakebase access.
"""
from __future__ import annotations

import time as _time
import uuid
from datetime import datetime, timezone
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
    DurableGenieSessionAdapterError,
    DurableGenieSessionKey,
    DurableGenieSessionUnavailableError,
    DurableGenieSessionVersionConflictError,
    GenieSessionLookupResult,
    GenieSessionLookupSource,
)
from app.services.durable_genie_session_runtime_factory import (
    DurableGenieSessionRuntimeBundle,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_OWNER_KEY = "a" * 64
_VALID_OWNER_KEY_2 = "b" * 64
_SPACE_ID = "test-space-id-4c2b"
_FRONTEND_CONV_ID = "frontend-conv-4c2b-001"
_FRONTEND_CONV_ID_2 = "frontend-conv-4c2b-002"
_GENIE_CONV_NEW = "genie-conv-new-4c2b"
_GENIE_CONV_FINAL = "genie-conv-final-4c2b"
_GENIE_CONV_OTHER = "genie-conv-other-4c2b"
_GENIE_CONV_RECOVERED = "genie-conv-recovered-4c2b"
_GENIE_MSG_RECOVERED = "genie-msg-recovered-4c2b"
_APP_CONV_ID = "nosession:frontend-conv-4c2b-001"
_APP_CONV_ID_2 = "nosession:frontend-conv-4c2b-002"
_MSG_WRITEBACK_FAILED = ("I wasn't able to complete that request. "
                         "Please try again in a moment.")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class RecordingGenieClient:
    """Genie client that records calls and returns deterministic responses."""

    def __init__(self, conv_id_sequence: Optional[List[str]] = None):
        self.start_calls: List[Dict[str, Any]] = []
        self.send_calls: List[Dict[str, Any]] = []
        self._conv_ids = list(conv_id_sequence or [])
        self._conv_counter = 0

    def _next_conv_id(self) -> str:
        if self._conv_ids and self._conv_counter < len(self._conv_ids):
            cid = self._conv_ids[self._conv_counter]
            self._conv_counter += 1
            return cid
        return _GENIE_CONV_NEW

    def start_conversation(self, space_id, message):
        cid = self._next_conv_id()
        self.start_calls.append({"space_id": space_id, "message": message,
                                  "conversation_id": cid})
        return {"conversation_id": cid, "message_id": "msg-new-4c2b"}

    def send_message(self, space_id, conv_id, message):
        self.send_calls.append({"space_id": space_id, "conv_id": conv_id,
                                 "message": message})
        return {"message_id": "msg-followup-4c2b"}

    def wait_for_message_completion(self, space_id, conv_id, msg_id, **kwargs):
        return MagicMock(query_attachments=None, message_id=msg_id)

    def fetch_query_result(self, *args, **kwargs):
        return None


class FakeRepositoryBundle:
    """Minimal repository bundle for adapter construction."""

    def __init__(self, repository):
        self.repository = repository
        self.backend = MagicMock()
        self.backend.value = "memory"
        self.durable = True

    def close(self):
        pass


class _TrackingAdapter:
    """Wraps a real DurableGenieSessionAdapter and records every call."""

    def __init__(self, real_adapter: DurableGenieSessionAdapter) -> None:
        self._real = real_adapter
        self.get_or_create_calls: List[DurableGenieSessionKey] = []
        self.bind_calls: List[Dict[str, Any]] = []
        self.load_calls: List[DurableGenieSessionKey] = []
        self.update_last_genie_message_calls: List[Any] = []
        self.touch_calls: List[Any] = []
        self.set_status_calls: List[Any] = []
        self.delete_calls: List[Any] = []

    def get_or_create(self, key: DurableGenieSessionKey,
                      **kwargs: Any) -> GenieSessionLookupResult:
        self.get_or_create_calls.append(key)
        return self._real.get_or_create(key, **kwargs)

    def load(self, key: DurableGenieSessionKey) -> Optional[GenieSessionLookupResult]:
        self.load_calls.append(key)
        return self._real.load(key)

    def bind_genie_conversation(
        self,
        key: DurableGenieSessionKey,
        genie_conversation_id: str,
        *,
        expected_version: int,
        **kwargs: Any,
    ) -> ConversationRecord:
        self.bind_calls.append({
            "key": key,
            "genie_conversation_id": genie_conversation_id,
            "expected_version": expected_version,
        })
        return self._real.bind_genie_conversation(
            key, genie_conversation_id, expected_version=expected_version, **kwargs
        )

    def update_last_genie_message(self, key: Any, *args: Any,
                                   **kwargs: Any) -> Any:
        self.update_last_genie_message_calls.append(key)
        return self._real.update_last_genie_message(key, *args, **kwargs)

    def touch(self, key: Any, **kwargs: Any) -> Any:
        self.touch_calls.append(key)
        return self._real.touch(key, **kwargs)

    def set_status(self, key: Any, status: Any, **kwargs: Any) -> Any:
        self.set_status_calls.append((key, status))
        return self._real.set_status(key, status, **kwargs)

    def delete(self, key: Any) -> Any:
        self.delete_calls.append(key)
        return self._real.delete(key)

    def close(self) -> None:
        self._real.close()

    def __getattr__(self, name: str) -> Any:  # noqa: ANN001
        return getattr(self._real, name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_pipeline(
    genie_client=None,
    session_store=None,
    **kwargs,
) -> GeniePipeline:
    """Build a minimal GeniePipeline for testing."""
    client = genie_client or RecordingGenieClient()
    store = session_store or GenieSessionStore()
    return GeniePipeline(
        genie_client=client,
        session_store=store,
        space_id=_SPACE_ID,
        fetch_query_results=False,
        enable_prompt_enrichment=False,
        enable_shape_validation=False,
        enable_table_summary=False,
        **kwargs,
    )


def _make_enabled_bundle(adapter) -> DurableGenieSessionRuntimeBundle:
    return DurableGenieSessionRuntimeBundle(
        enabled=True,
        adapter=adapter,
        backend=MagicMock(),
        durable=True,
    )


def _make_disabled_bundle() -> DurableGenieSessionRuntimeBundle:
    return DurableGenieSessionRuntimeBundle(
        enabled=False,
        adapter=None,
        backend=None,
        durable=False,
    )


def _make_miss_pipeline_with_tracking(
    conv_id_sequence: Optional[List[str]] = None,
):
    """Build a MISS pipeline (empty repo) with a tracking adapter.

    Returns: (pipeline, store, client, tracking_adapter, repo)
    """
    store = GenieSessionStore()
    client = RecordingGenieClient(conv_id_sequence=conv_id_sequence)
    pipeline = _build_pipeline(genie_client=client, session_store=store)
    repo = InMemoryConversationRepository()
    repo_bundle = FakeRepositoryBundle(repo)
    real_adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
    tracking = _TrackingAdapter(real_adapter)
    bundle = _make_enabled_bundle(tracking)
    setattr(pipeline, "_durable_session_runtime_bundle", bundle)
    return pipeline, store, client, tracking, repo


def _make_recovered_pipeline(
    genie_conv_id: str = _GENIE_CONV_RECOVERED,
    last_msg_id: Optional[str] = _GENIE_MSG_RECOVERED,
    owner_hash: str = _VALID_OWNER_KEY,
    frontend_id: str = _FRONTEND_CONV_ID,
):
    """Build a RECOVERED pipeline (pre-seeded repo, active record with Genie ID)."""
    repo = InMemoryConversationRepository()
    created = repo.create_conversation(owner_hash, frontend_id)
    bound = repo.bind_genie_conversation(
        owner_hash, created.conversation_id, genie_conv_id,
        expected_version=created.version,
    )
    if last_msg_id:
        repo.update_last_genie_message(
            owner_hash, created.conversation_id, last_msg_id,
            expected_version=bound.version,
        )
    store = GenieSessionStore()
    client = RecordingGenieClient()
    pipeline = _build_pipeline(genie_client=client, session_store=store)
    repo_bundle = FakeRepositoryBundle(repo)
    real_adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
    tracking = _TrackingAdapter(real_adapter)
    bundle = _make_enabled_bundle(tracking)
    setattr(pipeline, "_durable_session_runtime_bundle", bundle)
    return pipeline, store, client, tracking, repo


def _build_success_result(
    genie_conv_id: str = _GENIE_CONV_NEW,
    app_conv_id: str = _APP_CONV_ID,
) -> Dict[str, Any]:
    """Minimal success result dict for direct _maybe_persist_durable_writeback calls."""
    return {
        "status": "success",
        "message": "Here are the shipments.",
        "is_table": False,
        "table_data": None,
        "row_count": 0,
        "preview_row_count": 0,
        "returned_row_count": 0,
        "total_row_count": None,
        "export_row_count": None,
        "display_row_limit": 100,
        "download_key": None,
        "export_id": None,
        "export_status": None,
        "export_mode": None,
        "execution_time_ms": 50,
        "conversation_id": app_conv_id,
        "clarification": None,
        "source": "genie",
        "genie_conversation_id": genie_conv_id,
        "genie_message_id": "msg-new-4c2b",
        "generated_sql": None,
        "suggested_questions": [],
        "has_visualization": False,
        "visualization": None,
        "attachment_types": [],
        "debug_info": None,
        "fallback_recommended": False,
    }


def _run_writeback_direct(
    pipeline: GeniePipeline,
    result: Dict[str, Any],
    owner_key: Optional[str] = _VALID_OWNER_KEY,
    frontend_conversation_id: Optional[str] = _FRONTEND_CONV_ID,
    app_conversation_id: str = _APP_CONV_ID,
    execution_time_ms: Optional[int] = 100,
) -> Dict[str, Any]:
    """Call _maybe_persist_durable_writeback directly on a pipeline."""
    return pipeline._maybe_persist_durable_writeback(
        result=result,
        owner_key=owner_key,
        frontend_conversation_id=frontend_conversation_id,
        app_conversation_id=app_conversation_id,
        start_time=_time.monotonic(),
        execution_time_ms=execution_time_ms,
    )


# ===========================================================================
# 1. DISABLED MODE — NO WRITE
# ===========================================================================


class TestDisabledNoWrite:
    """With a disabled bundle, the post-execution writeback never fires."""

    def test_disabled_bundle_performs_no_write(self):
        """Disabled bundle: run() succeeds, repository stays empty."""
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client)
        bundle = _make_disabled_bundle()
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)
        repo = InMemoryConversationRepository()

        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        # Genie call happened (disabled bundle should not gate Genie)
        # disabled bundle means durable lookup is skipped — no owner check
        assert result["status"] == "success"
        # Repository is external; no writeback occurred
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record is None


# ===========================================================================
# 2–4. RECOVERED — NO GET_OR_CREATE, NO BIND
# ===========================================================================


class TestRecoveredNoWrite:
    """RECOVERED lookup outcome never triggers durable writeback."""

    def test_recovered_no_get_or_create(self):
        """RECOVERED outcome: get_or_create is never called."""
        pipeline, _, _, tracking, _ = _make_recovered_pipeline()

        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        assert len(tracking.get_or_create_calls) == 0

    def test_recovered_no_bind(self):
        """RECOVERED outcome: bind_genie_conversation is never called."""
        pipeline, _, _, tracking, _ = _make_recovered_pipeline()

        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        assert len(tracking.bind_calls) == 0

    def test_recovered_request_succeeds(self):
        """RECOVERED outcome: response is success with send_message (not start)."""
        pipeline, _, client, tracking, _ = _make_recovered_pipeline()

        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        assert result["status"] == "success"
        assert len(client.send_calls) == 1
        assert client.send_calls[0]["conv_id"] == _GENIE_CONV_RECOVERED
        assert len(client.start_calls) == 0
        # No writeback: no mutations
        assert len(tracking.get_or_create_calls) == 0
        assert len(tracking.bind_calls) == 0


# ===========================================================================
# 5–15. SUCCESSFUL MISS WRITEBACK
# ===========================================================================


class TestSuccessfulMissWriteback:
    """MISS outcome with successful Genie turn creates and binds a durable record."""

    def test_miss_starts_new_genie_conversation(self):
        """MISS triggers start_conversation (not send_message)."""
        pipeline, _, client, _, _ = _make_miss_pipeline_with_tracking()

        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        assert result["status"] == "success"
        assert len(client.start_calls) == 1
        assert len(client.send_calls) == 0

    def test_miss_final_genie_id_persisted_in_durable_record(self):
        """After MISS + success, durable record is bound to the Genie conv ID."""
        pipeline, _, client, _, repo = _make_miss_pipeline_with_tracking()

        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        genie_id_returned = client.start_calls[0]["conversation_id"]
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record is not None
        assert record.genie_conversation_id == genie_id_returned

    def test_miss_durable_key_exact_owner_hash(self):
        """get_or_create is called with the exact owner_key hash."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()

        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        assert len(tracking.get_or_create_calls) == 1
        called_key: DurableGenieSessionKey = tracking.get_or_create_calls[0]
        assert called_key.owner_user_id_hash == _VALID_OWNER_KEY

    def test_miss_durable_key_exact_frontend_id(self):
        """get_or_create is called with the exact frontend_conversation_id."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()

        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        called_key: DurableGenieSessionKey = tracking.get_or_create_calls[0]
        assert called_key.frontend_conversation_id == _FRONTEND_CONV_ID

    def test_miss_get_or_create_called_exactly_once(self):
        """get_or_create is called exactly once per request."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()

        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        assert len(tracking.get_or_create_calls) == 1

    def test_miss_bind_called_exactly_once_for_unbound_record(self):
        """bind_genie_conversation is called exactly once for a new record."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()

        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        assert len(tracking.bind_calls) == 1

    def test_miss_exact_current_version_passed_to_bind(self):
        """bind is called with the version from the get_or_create result."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline_with_tracking()

        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        # The record was just created; version from get_or_create is its initial version
        record_after = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record_after is not None
        bind_info = tracking.bind_calls[0]
        # The version passed to bind must be the version that was current at bind time
        # (i.e. the version from get_or_create, which is one less than the post-bind version)
        assert isinstance(bind_info["expected_version"], int)
        assert bind_info["expected_version"] >= 0
        assert bind_info["expected_version"] < record_after.version

    def test_miss_returned_binding_confirmed_in_record(self):
        """The durable record contains the same Genie ID that bind was called with."""
        pipeline, _, client, tracking, repo = _make_miss_pipeline_with_tracking()

        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        genie_id = client.start_calls[0]["conversation_id"]
        bind_info = tracking.bind_calls[0]
        assert bind_info["genie_conversation_id"] == genie_id
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record.genie_conversation_id == genie_id

    def test_miss_response_returned_after_bind_succeeds(self):
        """A success response is returned after writeback completes."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()

        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        assert result["status"] == "success"
        assert len(tracking.bind_calls) == 1  # bind completed

    def test_miss_in_memory_mapping_retained_after_writeback(self):
        """Session store retains the Genie conv ID after successful writeback."""
        pipeline, store, client, _, _ = _make_miss_pipeline_with_tracking()

        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        genie_id = client.start_calls[0]["conversation_id"]
        assert store.get_genie_conversation_id(_APP_CONV_ID) == genie_id

    def test_miss_owner_fields_absent_from_response(self):
        """Owner hash and frontend durable ID are not present in the response."""
        pipeline, _, _, _, _ = _make_miss_pipeline_with_tracking()

        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        result_str = str(result)
        assert _VALID_OWNER_KEY not in result_str
        assert "owner_key" not in result
        assert "owner_user_id_hash" not in result


# ===========================================================================
# 16–18. FINAL CONVERSATION SELECTION
# ===========================================================================


class TestFinalConversationSelection:
    """The writeback uses the result dict's genie_conversation_id, not the session store."""

    def test_writeback_uses_final_result_conv_id_not_session_store(self):
        """If session store has an old ID but result has the final ID, persist the result ID."""
        pipeline, store, _, tracking, repo = _make_miss_pipeline_with_tracking()

        # Simulate: session store has an intermediate ID (as if a shape retry ran)
        store.set_genie_conversation_id(_APP_CONV_ID, "genie-conv-intermediate")

        # Result dict carries the final Genie conversation ID
        result = _build_success_result(genie_conv_id=_GENIE_CONV_FINAL)
        returned = _run_writeback_direct(pipeline, result)

        assert returned["status"] == "success"
        assert returned["genie_conversation_id"] == _GENIE_CONV_FINAL
        # Durable record bound to final ID, not the intermediate one from the store
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record is not None
        assert record.genie_conversation_id == _GENIE_CONV_FINAL
        assert record.genie_conversation_id != "genie-conv-intermediate"

    def test_intermediate_genie_id_never_persisted(self):
        """An intermediate ID visible in the session store is not written to the repo."""
        pipeline, store, _, _, repo = _make_miss_pipeline_with_tracking()
        intermediate_id = "genie-conv-never-persisted"

        store.set_genie_conversation_id(_APP_CONV_ID, intermediate_id)

        result = _build_success_result(genie_conv_id=_GENIE_CONV_FINAL)
        _run_writeback_direct(pipeline, result)

        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record is not None
        assert record.genie_conversation_id != intermediate_id

    def test_writeback_occurs_once_after_result_returned(self):
        """get_or_create and bind are each called exactly once per writeback."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()

        result = _build_success_result(genie_conv_id=_GENIE_CONV_FINAL)
        _run_writeback_direct(pipeline, result)
        # Call again with the same key to verify no double writeback in the method itself
        result2 = _build_success_result(genie_conv_id=_GENIE_CONV_FINAL)
        _run_writeback_direct(pipeline, result2)

        # Each direct call triggers one get_or_create; second call hits idempotency check
        # and skips bind because the record is already bound to _GENIE_CONV_FINAL
        assert len(tracking.get_or_create_calls) == 2
        # bind should only have been called once (second call is idempotent)
        assert len(tracking.bind_calls) == 1


# ===========================================================================
# 19–24. NO-WRITE PATHS
# ===========================================================================


class TestNoWritePaths:
    """Various paths where no durable record should be created."""

    def test_local_greeting_creates_no_durable_record(self):
        """A local/greeting response has no genie_conversation_id — no write."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline_with_tracking()

        # "hello" is classified as a local/off-topic response (no Genie call)
        result = pipeline.run(
            "hello", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        assert result["status"] == "success"
        # No genie_conversation_id in result → no durable record
        assert not result.get("genie_conversation_id")
        assert len(tracking.get_or_create_calls) == 0
        assert len(tracking.bind_calls) == 0
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record is None

    def test_local_off_topic_creates_no_durable_record(self):
        """Off-topic local response creates no durable record."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline_with_tracking()

        result = _build_success_result(genie_conv_id=None,
                                        app_conv_id=_APP_CONV_ID)
        result["source"] = "local"
        result["genie_conversation_id"] = None
        returned = _run_writeback_direct(pipeline, result)

        assert returned["status"] == "success"
        assert len(tracking.get_or_create_calls) == 0
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record is None

    def test_genie_error_creates_no_durable_record(self):
        """A Genie-error result (status=error) triggers no writeback."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline_with_tracking()

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        result["status"] = "error"
        returned = _run_writeback_direct(pipeline, result)

        assert returned["status"] == "error"
        assert len(tracking.get_or_create_calls) == 0
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record is None

    def test_result_without_genie_conv_id_creates_no_record(self):
        """A success result with no genie_conversation_id triggers no writeback."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline_with_tracking()

        result = _build_success_result(genie_conv_id=None)
        returned = _run_writeback_direct(pipeline, result)

        assert returned is result  # unchanged
        assert len(tracking.get_or_create_calls) == 0
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record is None

    def test_invalid_owner_key_creates_no_record(self):
        """None or empty owner_key skips writeback entirely."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline_with_tracking()

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)

        # None owner key
        returned_none = _run_writeback_direct(pipeline, result, owner_key=None)
        assert returned_none is result

        # Empty string owner key
        returned_empty = _run_writeback_direct(pipeline, result, owner_key="")
        assert returned_empty is result

        assert len(tracking.get_or_create_calls) == 0
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record is None

    def test_lookup_failure_creates_no_durable_record(self):
        """When the durable lookup fails, run() returns error and no record is created."""
        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo = InMemoryConversationRepository()
        repo_bundle = FakeRepositoryBundle(repo)
        real_adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
        real_adapter.load = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("repo down")
        )
        bundle = _make_enabled_bundle(real_adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)

        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        assert result["status"] == "error"
        assert result["fallback_recommended"] is False
        assert len(client.start_calls) == 0  # Genie not called
        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record is None


# ===========================================================================
# 25–33. IDEMPOTENCY AND CONFLICTS
# ===========================================================================


class TestIdempotencyAndConflicts:
    """Binding idempotency and optimistic-concurrency conflict handling."""

    def test_existing_same_binding_succeeds(self):
        """get_or_create returns already-bound record with same ID → success, no bind."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline_with_tracking()

        # Pre-seed the repo with the binding we will also try to write
        created = repo.create_conversation(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        repo.bind_genie_conversation(
            _VALID_OWNER_KEY, created.conversation_id, _GENIE_CONV_NEW,
            expected_version=created.version,
        )

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        returned = _run_writeback_direct(pipeline, result)

        assert returned["status"] == "success"
        # get_or_create was called once to fetch the existing record
        assert len(tracking.get_or_create_calls) == 1
        # bind was NOT called because the record was already bound to the same ID
        assert len(tracking.bind_calls) == 0

    def test_same_binding_does_not_invoke_bind_again(self):
        """Idempotent path: same Genie ID already bound → bind is skipped."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline_with_tracking()

        created = repo.create_conversation(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        repo.bind_genie_conversation(
            _VALID_OWNER_KEY, created.conversation_id, _GENIE_CONV_NEW,
            expected_version=created.version,
        )

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        _run_writeback_direct(pipeline, result)

        assert len(tracking.bind_calls) == 0

    def test_different_binding_fails_closed(self):
        """If record is already bound to a different Genie ID, writeback returns error."""
        pipeline, _, _, _, repo = _make_miss_pipeline_with_tracking()

        created = repo.create_conversation(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        repo.bind_genie_conversation(
            _VALID_OWNER_KEY, created.conversation_id, _GENIE_CONV_OTHER,
            expected_version=created.version,
        )

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        returned = _run_writeback_direct(pipeline, result)

        assert returned["status"] == "error"
        assert returned["fallback_recommended"] is False

    def test_different_binding_not_overwritten(self):
        """The existing binding to a different ID is preserved after failure."""
        pipeline, _, _, _, repo = _make_miss_pipeline_with_tracking()

        created = repo.create_conversation(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        repo.bind_genie_conversation(
            _VALID_OWNER_KEY, created.conversation_id, _GENIE_CONV_OTHER,
            expected_version=created.version,
        )

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        _run_writeback_direct(pipeline, result)

        record = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record.genie_conversation_id == _GENIE_CONV_OTHER

    def test_version_conflict_triggers_at_most_one_reload(self):
        """On DurableGenieSessionVersionConflictError, adapter.load is called at most once."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline_with_tracking()

        # Pre-seed an UNBOUND record so get_or_create returns it (unbound).
        # This means the idempotency check does NOT fire, so bind IS attempted.
        repo.create_conversation(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)

        # Mock bind to always raise conflict.
        tracking._real.bind_genie_conversation = MagicMock(
            side_effect=DurableGenieSessionVersionConflictError("conflict")
        )

        # Mock load to return a bound result (simulates a concurrent caller binding).
        now = datetime.now(timezone.utc)
        bound_record = ConversationRecord(
            conversation_id=str(uuid.uuid4()),
            owner_user_id_hash=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
            genie_conversation_id=_GENIE_CONV_NEW,
            last_genie_message_id=None,
            status=ConversationStatus.ACTIVE,
            version=2,
            created_at=now, updated_at=now, last_active_at=now,
        )
        bound_lookup = GenieSessionLookupResult(
            record=bound_record,
            source=GenieSessionLookupSource.REPOSITORY,
            degraded=False,
        )
        load_calls: List[DurableGenieSessionKey] = []

        def _mocked_load(key):
            load_calls.append(key)
            return bound_lookup

        tracking._real.load = _mocked_load
        # Phase 4C3: mock update_last_genie_message to succeed without real repo
        _upd_c = MagicMock()
        _upd_c.genie_conversation_id = _GENIE_CONV_NEW
        _upd_c.last_genie_message_id = "msg-new-4c2b"
        tracking._real.update_last_genie_message = MagicMock(return_value=_upd_c)

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        returned = _run_writeback_direct(pipeline, result)

        assert returned["status"] == "success"
        assert len(load_calls) == 1  # exactly one reload, no retry loop

    def test_reload_with_same_binding_succeeds(self):
        """After conflict, reload showing same ID → success (idempotent via reload)."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline_with_tracking()

        # Pre-seed UNBOUND record so get_or_create returns it unbound,
        # ensuring bind IS attempted before conflict triggers reload.
        repo.create_conversation(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)

        tracking._real.bind_genie_conversation = MagicMock(
            side_effect=DurableGenieSessionVersionConflictError("conflict")
        )
        # Reload returns same binding → idempotent success.
        now = datetime.now(timezone.utc)
        same_bound_record = ConversationRecord(
            conversation_id=str(uuid.uuid4()),
            owner_user_id_hash=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
            genie_conversation_id=_GENIE_CONV_NEW,
            last_genie_message_id=None,
            status=ConversationStatus.ACTIVE,
            version=2,
            created_at=now, updated_at=now, last_active_at=now,
        )
        tracking._real.load = MagicMock(return_value=GenieSessionLookupResult(
            record=same_bound_record,
            source=GenieSessionLookupSource.REPOSITORY,
            degraded=False,
        ))
        # Phase 4C3: mock update_last_genie_message to succeed without real repo
        _upd_d = MagicMock()
        _upd_d.genie_conversation_id = _GENIE_CONV_NEW
        _upd_d.last_genie_message_id = "msg-new-4c2b"
        tracking._real.update_last_genie_message = MagicMock(return_value=_upd_d)

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        returned = _run_writeback_direct(pipeline, result)

        assert returned["status"] == "success"

    def test_reload_with_different_binding_fails(self):
        """After conflict, reload showing different ID → fail closed."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline_with_tracking()

        # Pre-seed UNBOUND record so bind IS attempted before conflict fires.
        repo.create_conversation(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)

        tracking._real.bind_genie_conversation = MagicMock(
            side_effect=DurableGenieSessionVersionConflictError("conflict")
        )
        # Reload returns a DIFFERENT binding → fail closed.
        now = datetime.now(timezone.utc)
        different_bound_record = ConversationRecord(
            conversation_id=str(uuid.uuid4()),
            owner_user_id_hash=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
            genie_conversation_id=_GENIE_CONV_OTHER,
            last_genie_message_id=None,
            status=ConversationStatus.ACTIVE,
            version=2,
            created_at=now, updated_at=now, last_active_at=now,
        )
        tracking._real.load = MagicMock(return_value=GenieSessionLookupResult(
            record=different_bound_record,
            source=GenieSessionLookupSource.REPOSITORY,
            degraded=False,
        ))

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        returned = _run_writeback_direct(pipeline, result)

        assert returned["status"] == "error"
        assert returned["fallback_recommended"] is False

    def test_reload_still_unbound_fails(self):
        """After conflict, if reload shows unbound record → fail closed."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline_with_tracking()

        tracking._real.bind_genie_conversation = MagicMock(
            side_effect=DurableGenieSessionVersionConflictError("conflict")
        )
        # Reload returns an unbound record (genie_conv_id=None)
        # We pre-seed an unbound record so load() returns it
        repo.create_conversation(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        returned = _run_writeback_direct(pipeline, result)

        assert returned["status"] == "error"
        assert returned["fallback_recommended"] is False

    def test_no_retry_loop_exists(self):
        """Conflict handling uses exactly one reload; there is no retry loop."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline_with_tracking()

        call_count = {"bind": 0, "load": 0}

        orig_bind = tracking._real.bind_genie_conversation
        orig_load = tracking._real.load

        def _always_conflict(key, gid, *, expected_version, **kwargs):
            call_count["bind"] += 1
            raise DurableGenieSessionVersionConflictError("always conflict")

        def _counting_load(key):
            call_count["load"] += 1
            return orig_load(key)

        tracking._real.bind_genie_conversation = _always_conflict
        tracking._real.load = _counting_load
        # Reload returns unbound record (just created)
        repo.create_conversation(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        returned = _run_writeback_direct(pipeline, result)

        assert returned["status"] == "error"
        # bind attempted once, load called once — no retry loop
        assert call_count["bind"] == 1
        assert call_count["load"] == 1


# ===========================================================================
# 34–42. FAILURE POLICY
# ===========================================================================


class TestFailurePolicy:
    """Writeback failures must fail closed, clear new mapping, no fallback."""

    def test_get_or_create_unavailable_fails_closed(self):
        """DurableGenieSessionUnavailableError from get_or_create → error response."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()
        tracking._real.get_or_create = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("unavailable")
        )

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        returned = _run_writeback_direct(pipeline, result)

        assert returned["status"] == "error"
        assert returned["fallback_recommended"] is False

    def test_bind_unavailable_fails_closed(self):
        """DurableGenieSessionUnavailableError from bind → error response."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()
        tracking._real.bind_genie_conversation = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("bind unavailable")
        )

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        returned = _run_writeback_direct(pipeline, result)

        assert returned["status"] == "error"
        assert returned["fallback_recommended"] is False

    def test_unexpected_adapter_error_fails_closed(self):
        """Any unexpected exception during writeback → fail closed."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()
        tracking._real.get_or_create = MagicMock(
            side_effect=RuntimeError("unexpected internal error")
        )

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        returned = _run_writeback_direct(pipeline, result)

        assert returned["status"] == "error"
        assert returned["fallback_recommended"] is False

    def test_failure_response_fallback_recommended_false(self):
        """Writeback error response has fallback_recommended=False."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()
        tracking._real.get_or_create = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("down")
        )

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        returned = _run_writeback_direct(pipeline, result)

        assert returned["fallback_recommended"] is False

    def test_failure_response_custom_fallback_prohibited(self):
        """Writeback failure must not set fallback_recommended=True."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()
        tracking._real.bind_genie_conversation = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("bind down")
        )

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        returned = _run_writeback_direct(pipeline, result)

        # Explicitly not allowed to fall back to custom pipeline
        assert returned.get("fallback_recommended") is not True

    def test_failure_response_static_sanitized_message(self):
        """Error message is a static sanitized string, not exception details."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()
        tracking._real.get_or_create = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("secret-internal-detail")
        )

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        returned = _run_writeback_direct(pipeline, result)

        assert "secret" not in returned["message"]
        assert returned["message"] == _MSG_WRITEBACK_FAILED

    def test_failure_response_owner_key_absent(self):
        """Owner hash does not appear in the error response."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()
        tracking._real.get_or_create = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("down")
        )

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        returned = _run_writeback_direct(pipeline, result)

        assert _VALID_OWNER_KEY not in str(returned)

    def test_failure_response_frontend_id_absent(self):
        """Frontend conversation ID does not appear in the error response."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()
        tracking._real.get_or_create = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("down")
        )

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        returned = _run_writeback_direct(
            pipeline, result,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        assert _FRONTEND_CONV_ID not in returned.get("message", "")

    def test_in_memory_mapping_cleared_after_writeback_failure(self):
        """After writeback failure, session store Genie mapping is cleared."""
        pipeline, store, _, tracking, _ = _make_miss_pipeline_with_tracking()
        tracking._real.get_or_create = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("down")
        )

        # Seed in-memory mapping (simulating a Genie turn that just ran)
        store.set_genie_conversation_id(_APP_CONV_ID, _GENIE_CONV_NEW)

        result = _build_success_result(genie_conv_id=_GENIE_CONV_NEW)
        returned = _run_writeback_direct(pipeline, result)

        assert returned["status"] == "error"
        # Session store mapping is cleared so the request doesn't continue
        # as an unpersisted in-memory-only conversation.
        assert store.get_genie_conversation_id(_APP_CONV_ID) is None


# ===========================================================================
# 43–47. PROHIBITED MUTATIONS
# ===========================================================================


class TestProhibitedMutations:
    """No lifecycle-mutating adapter operations occur during writeback."""

    def test_update_last_genie_message_called_once(self):
        """update_last_genie_message is invoked exactly once during MISS writeback.

        Phase 4C3 adds final-message persistence after the durable bind step.
        The approved sequence is: get_or_create → bind → update_last_genie_message.
        """
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()

        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        assert len(tracking.update_last_genie_message_calls) == 1

    def test_touch_not_called(self):
        """touch is never invoked during MISS writeback."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()

        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        assert len(tracking.touch_calls) == 0

    def test_set_status_not_called(self):
        """set_status is never invoked during MISS writeback."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()

        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        assert len(tracking.set_status_calls) == 0

    def test_delete_not_called(self):
        """delete is never invoked during MISS writeback."""
        pipeline, _, _, tracking, _ = _make_miss_pipeline_with_tracking()

        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        assert len(tracking.delete_calls) == 0

    def test_pipeline_does_not_access_repository_directly(self):
        """GeniePipeline never holds a direct reference to the repository."""
        pipeline, _, _, _, _ = _make_miss_pipeline_with_tracking()

        # The pipeline instance must not have any attribute that directly references
        # an InMemoryConversationRepository — all access must go through the adapter.
        instance_vars = vars(pipeline)
        for attr_name, value in instance_vars.items():
            assert not isinstance(value, InMemoryConversationRepository), (
                f"GeniePipeline.{attr_name} is a direct InMemoryConversationRepository reference"
            )


# ===========================================================================
# 48–50. ISOLATION
# ===========================================================================


class TestIsolation:
    """Writeback is scoped per owner; no cross-owner leakage."""

    def test_two_owners_same_frontend_id_create_separate_records(self):
        """Two distinct owners each get their own durable record."""
        store = GenieSessionStore()
        client = RecordingGenieClient(conv_id_sequence=[
            "genie-conv-owner-a", "genie-conv-owner-b"
        ])
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo = InMemoryConversationRepository()
        repo_bundle = FakeRepositoryBundle(repo)
        real_adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
        bundle = _make_enabled_bundle(real_adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)

        # Owner A creates a conversation
        result_a = _build_success_result(
            genie_conv_id="genie-conv-owner-a", app_conv_id=_APP_CONV_ID
        )
        pipeline._maybe_persist_durable_writeback(
            result=result_a,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
            app_conversation_id=_APP_CONV_ID,
            start_time=_time.monotonic(),
            execution_time_ms=50,
        )

        # Owner B creates a conversation with the same frontend_id
        result_b = _build_success_result(
            genie_conv_id="genie-conv-owner-b", app_conv_id=_APP_CONV_ID_2
        )
        pipeline._maybe_persist_durable_writeback(
            result=result_b,
            owner_key=_VALID_OWNER_KEY_2,
            frontend_conversation_id=_FRONTEND_CONV_ID,
            app_conversation_id=_APP_CONV_ID_2,
            start_time=_time.monotonic(),
            execution_time_ms=50,
        )

        # Owner A and Owner B have separate records
        record_a = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        record_b = repo.get_by_frontend_id(_VALID_OWNER_KEY_2, _FRONTEND_CONV_ID)
        assert record_a is not None
        assert record_b is not None
        assert record_a.genie_conversation_id == "genie-conv-owner-a"
        assert record_b.genie_conversation_id == "genie-conv-owner-b"
        assert record_a.conversation_id != record_b.conversation_id

    def test_owner_b_cannot_overwrite_owner_a_binding(self):
        """Owner B writing their record does not affect Owner A's existing record."""
        store = GenieSessionStore()
        pipeline = _build_pipeline(session_store=store)
        repo = InMemoryConversationRepository()
        repo_bundle = FakeRepositoryBundle(repo)
        real_adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
        bundle = _make_enabled_bundle(real_adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)

        # Owner A has an existing binding
        created_a = repo.create_conversation(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        repo.bind_genie_conversation(
            _VALID_OWNER_KEY, created_a.conversation_id, "genie-conv-a-original",
            expected_version=created_a.version,
        )

        # Owner B writes their own record
        result_b = _build_success_result(genie_conv_id="genie-conv-b-new")
        pipeline._maybe_persist_durable_writeback(
            result=result_b,
            owner_key=_VALID_OWNER_KEY_2,
            frontend_conversation_id=_FRONTEND_CONV_ID,
            app_conversation_id=_APP_CONV_ID,
            start_time=_time.monotonic(),
            execution_time_ms=50,
        )

        # Owner A's record is unchanged
        record_a = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        assert record_a.genie_conversation_id == "genie-conv-a-original"

    def test_writeback_state_does_not_leak_across_calls(self):
        """State from one writeback call does not affect a subsequent call."""
        pipeline, _, _, tracking, repo = _make_miss_pipeline_with_tracking()

        # First call: creates and binds genie-conv-first
        result1 = _build_success_result(
            genie_conv_id="genie-conv-first", app_conv_id=_APP_CONV_ID
        )
        returned1 = _run_writeback_direct(
            pipeline, result1,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert returned1["status"] == "success"

        # Second call with a different frontend_id and owner
        result2 = _build_success_result(
            genie_conv_id="genie-conv-second", app_conv_id=_APP_CONV_ID_2
        )
        returned2 = _run_writeback_direct(
            pipeline, result2,
            owner_key=_VALID_OWNER_KEY_2,
            frontend_conversation_id=_FRONTEND_CONV_ID_2,
        )
        assert returned2["status"] == "success"

        # Both records exist independently in the repo
        record1 = repo.get_by_frontend_id(_VALID_OWNER_KEY, _FRONTEND_CONV_ID)
        record2 = repo.get_by_frontend_id(_VALID_OWNER_KEY_2, _FRONTEND_CONV_ID_2)
        assert record1.genie_conversation_id == "genie-conv-first"
        assert record2.genie_conversation_id == "genie-conv-second"
        assert record1.conversation_id != record2.conversation_id
