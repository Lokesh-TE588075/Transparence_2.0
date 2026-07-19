"""Phase 4C4B2 — GeniePipeline inactive-durable-state protection tests.

BOUNDARY 1 (initial lookup): Authoritative RESET, STALE, EXPIRED records
classified as INACTIVE and blocked before any Genie execution.

BOUNDARY 2 (post-miss writeback): get_or_create returning a non-ACTIVE record
or a degraded result after Genie execution is completed — reset tombstone TOCTOU
protection.

All core tests use real InMemoryConversationRepository + DurableGenieSessionAdapter
+ GenieSessionStore.  Narrow tracking fakes guard prohibited operations.

Every test makes exact behavioural assertions.  No bare pass, no or-True.
"""
from __future__ import annotations

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
    DurableGenieSessionKey,
    DurableGenieSessionUnavailableError,
    GenieSessionLookupResult,
    GenieSessionLookupSource,
)
from app.services.durable_genie_session_runtime_factory import (
    DurableGenieSessionRuntimeBundle,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_OWNER_A   = "a" * 64
_VALID_OWNER_B   = "b" * 64
_SPACE_ID        = "test-space-4c4b2"
_FRONTEND_ID     = "frontend-4c4b2-001"
_FRONTEND_ID_2   = "frontend-4c4b2-002"
_GENIE_CONV_ID   = "genie-conv-4c4b2-001"
_GENIE_MSG_ID    = "genie-msg-4c4b2-001"
_APP_CONV_ID     = "session-abc:frontend-4c4b2-001"
_APP_CONV_ID_2   = "session-xyz:frontend-4c4b2-001"  # different session, same frontend
_APP_CONV_ID_3   = "session-abc:frontend-4c4b2-002"  # same session, different frontend
_MSG_INACTIVE    = "This conversation is no longer active. Start a new chat."

_INACTIVE_STATUSES = [
    ConversationStatus.RESET,
    ConversationStatus.STALE,
    ConversationStatus.EXPIRED,
]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class RecordingGenieClient:
    """Records all Genie method calls; raises on unauthorized calls when guarded."""

    def __init__(self, raise_on_start: bool = False, raise_on_send: bool = False):
        self.start_calls: List[Dict[str, Any]] = []
        self.send_calls:  List[Dict[str, Any]] = []
        self._raise_on_start = raise_on_start
        self._raise_on_send  = raise_on_send

    def start_conversation(self, space_id: str, message: str) -> Dict[str, Any]:
        self.start_calls.append({"space_id": space_id, "message": message})
        if self._raise_on_start:
            raise AssertionError("start_conversation must not be called for INACTIVE")
        return {"conversation_id": _GENIE_CONV_ID, "message_id": _GENIE_MSG_ID}

    def send_message(self, space_id: str, conv_id: str, message: str) -> Dict[str, Any]:
        self.send_calls.append({"space_id": space_id, "conv_id": conv_id, "message": message})
        if self._raise_on_send:
            raise AssertionError("send_message must not be called for INACTIVE")
        return {"message_id": _GENIE_MSG_ID}

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_pipeline(
    genie_client=None,
    session_store=None,
    **kwargs,
) -> GeniePipeline:
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
        **kwargs,
    )


def _make_enabled_bundle(adapter) -> DurableGenieSessionRuntimeBundle:
    return DurableGenieSessionRuntimeBundle(
        enabled=True,
        adapter=adapter,
        backend=MagicMock(),
        durable=True,
    )


def _make_record(
    status: ConversationStatus,
    owner_hash: str = _VALID_OWNER_A,
    frontend_id: str = _FRONTEND_ID,
    genie_conv_id: Optional[str] = _GENIE_CONV_ID,
    last_msg_id: Optional[str] = _GENIE_MSG_ID,
    version: int = 2,
) -> ConversationRecord:
    """Build a ConversationRecord with the given status."""
    now = datetime.now(timezone.utc)
    return ConversationRecord(
        conversation_id=str(uuid.uuid4()),
        owner_user_id_hash=owner_hash,
        frontend_conversation_id=frontend_id,
        genie_conversation_id=genie_conv_id,
        last_genie_message_id=last_msg_id,
        status=status,
        version=version,
        created_at=now,
        updated_at=now,
        last_active_at=now,
    )


def _authoritative_lookup_result(record: ConversationRecord) -> GenieSessionLookupResult:
    """Wrap a record in a non-degraded (authoritative) lookup result."""
    return GenieSessionLookupResult(
        record=record,
        source=GenieSessionLookupSource.REPOSITORY,
        degraded=False,
    )


def _degraded_lookup_result(record: ConversationRecord) -> GenieSessionLookupResult:
    """Wrap a record in a degraded (cache-only) lookup result."""
    return GenieSessionLookupResult(
        record=record,
        source=GenieSessionLookupSource.CACHE,
        degraded=True,
    )


def _build_inactive_pipeline(
    status: ConversationStatus,
    owner_hash: str = _VALID_OWNER_A,
    frontend_id: str = _FRONTEND_ID,
    app_conv_id: str = _APP_CONV_ID,
):
    """Build a pipeline whose durable load returns an authoritative record
    with the given inactive status.

    Returns (pipeline, store, client, adapter).
    """
    store  = GenieSessionStore()
    client = RecordingGenieClient(raise_on_start=True, raise_on_send=True)
    pipeline = _build_pipeline(genie_client=client, session_store=store)

    repo = InMemoryConversationRepository()
    repo_bundle = FakeRepositoryBundle(repo)
    adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)

    record = _make_record(status, owner_hash=owner_hash, frontend_id=frontend_id)
    adapter.load = MagicMock(return_value=_authoritative_lookup_result(record))

    bundle = _make_enabled_bundle(adapter)
    setattr(pipeline, "_durable_session_runtime_bundle", bundle)
    return pipeline, store, client, adapter


def _build_active_pipeline(
    owner_hash: str = _VALID_OWNER_A,
    frontend_id: str = _FRONTEND_ID,
    app_conv_id: str = _APP_CONV_ID,
):
    """Build a pipeline whose durable load returns an authoritative ACTIVE record.

    Returns (pipeline, store, client, adapter, repo).
    """
    repo = InMemoryConversationRepository()
    created = repo.create_conversation(owner_hash, frontend_id)
    bound = repo.bind_genie_conversation(
        owner_hash, created.conversation_id, _GENIE_CONV_ID,
        expected_version=created.version,
    )
    repo.update_last_genie_message(
        owner_hash, created.conversation_id, _GENIE_MSG_ID,
        expected_version=bound.version,
    )
    store  = GenieSessionStore()
    client = RecordingGenieClient()
    pipeline = _build_pipeline(genie_client=client, session_store=store)
    repo_bundle = FakeRepositoryBundle(repo)
    adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
    bundle = _make_enabled_bundle(adapter)
    setattr(pipeline, "_durable_session_runtime_bundle", bundle)
    return pipeline, store, client, adapter, repo


# ===========================================================================
# BOUNDARY 1: INITIAL LOOKUP — INACTIVE CLASSIFICATION
# ===========================================================================


class TestInactiveLookupOutcome:
    """Steps 8.1-8.5 — Authoritative records with inactive statuses."""

    def test_active_record_is_recovered(self):
        """Test 1: Authoritative ACTIVE + bound record → RECOVERED outcome."""
        pipeline, store, client, adapter, _ = _build_active_pipeline()
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["status"] == "success"
        assert len(client.start_calls) == 0
        assert len(client.send_calls) == 1
        assert result["fallback_recommended"] is False

    def test_no_record_is_miss(self):
        """Test 2: Authoritative no record → MISS → start_conversation."""
        store  = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo = InMemoryConversationRepository()
        repo_bundle = FakeRepositoryBundle(repo)
        adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
        bundle = _make_enabled_bundle(adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["status"] == "success"
        assert len(client.start_calls) == 1
        assert len(client.send_calls) == 0

    @pytest.mark.parametrize("status", _INACTIVE_STATUSES)
    def test_inactive_status_is_blocked(self, status):
        """Tests 3-5: Authoritative RESET/STALE/EXPIRED → INACTIVE → blocked."""
        pipeline, store, client, adapter = _build_inactive_pipeline(status)
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["status"] == "inactive"
        assert result["fallback_recommended"] is False


# ===========================================================================
# BOUNDARY 1: INACTIVE DOES NOT CALL _run_inner
# ===========================================================================


class TestInactiveNoGenieExecution:
    """Steps 8.6-8.11 — INACTIVE must not trigger any Genie call."""

    @pytest.mark.parametrize("status", _INACTIVE_STATUSES)
    def test_inactive_does_not_call_start_conversation(self, status):
        """Tests 6-8: start_conversation must never be called for INACTIVE."""
        pipeline, _, client, _ = _build_inactive_pipeline(status)
        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert len(client.start_calls) == 0

    @pytest.mark.parametrize("status", _INACTIVE_STATUSES)
    def test_inactive_does_not_call_send_message(self, status):
        """Tests (same as 6-8 but via send path): send_message must never be called."""
        pipeline, store, _, _ = _build_inactive_pipeline(status)
        # Pre-seed a mapping so _run_inner would normally use send_message
        store.set_genie_conversation_id(_APP_CONV_ID, "genie-conv-pre-existing")

        client_guard = RecordingGenieClient(raise_on_start=True, raise_on_send=True)
        pipeline._client = client_guard
        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert len(client_guard.send_calls) == 0
        assert len(client_guard.start_calls) == 0

    @pytest.mark.parametrize("status", _INACTIVE_STATUSES)
    def test_inactive_does_not_call_get_or_create(self, status):
        """Test 12: get_or_create must never be called for INACTIVE."""
        pipeline, _, _, adapter = _build_inactive_pipeline(status)
        adapter.get_or_create = MagicMock(side_effect=AssertionError("get_or_create prohibited"))
        # Must not raise
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["status"] == "inactive"
        adapter.get_or_create.assert_not_called()

    @pytest.mark.parametrize("status", _INACTIVE_STATUSES)
    def test_inactive_does_not_call_custom_fallback(self, status):
        """Test 11: fallback_recommended=False ensures no custom fallback."""
        pipeline, _, _, _ = _build_inactive_pipeline(status)
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["fallback_recommended"] is False


# ===========================================================================
# BOUNDARY 1: PROHIBITED ADAPTER OPERATIONS
# ===========================================================================


class TestInactiveProhibitedAdapterOperations:
    """Steps 8.12-8.17 — All durable mutations are prohibited for INACTIVE."""

    @pytest.mark.parametrize("status", _INACTIVE_STATUSES)
    def test_inactive_does_not_call_bind(self, status):
        """Test 13: bind_genie_conversation must never be called."""
        pipeline, _, _, adapter = _build_inactive_pipeline(status)
        adapter.bind_genie_conversation = MagicMock(
            side_effect=AssertionError("bind prohibited for INACTIVE")
        )
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["status"] == "inactive"
        adapter.bind_genie_conversation.assert_not_called()

    @pytest.mark.parametrize("status", _INACTIVE_STATUSES)
    def test_inactive_does_not_update_last_message(self, status):
        """Test 14: update_last_genie_message must never be called."""
        pipeline, _, _, adapter = _build_inactive_pipeline(status)
        adapter.update_last_genie_message = MagicMock(
            side_effect=AssertionError("update_last_genie_message prohibited for INACTIVE")
        )
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["status"] == "inactive"
        adapter.update_last_genie_message.assert_not_called()

    @pytest.mark.parametrize("status", _INACTIVE_STATUSES)
    def test_inactive_does_not_touch(self, status):
        """Test 15: touch must never be called."""
        pipeline, _, _, adapter = _build_inactive_pipeline(status)
        adapter.touch = MagicMock(
            side_effect=AssertionError("touch prohibited for INACTIVE")
        )
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["status"] == "inactive"
        adapter.touch.assert_not_called()

    @pytest.mark.parametrize("status", _INACTIVE_STATUSES)
    def test_inactive_does_not_set_status(self, status):
        """Test 16: set_status must never be called."""
        pipeline, _, _, adapter = _build_inactive_pipeline(status)
        adapter.set_status = MagicMock(
            side_effect=AssertionError("set_status prohibited for INACTIVE")
        )
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["status"] == "inactive"
        adapter.set_status.assert_not_called()

    @pytest.mark.parametrize("status", _INACTIVE_STATUSES)
    def test_inactive_does_not_delete(self, status):
        """Test 17: delete must never be called."""
        pipeline, _, _, adapter = _build_inactive_pipeline(status)
        adapter.delete = MagicMock(
            side_effect=AssertionError("delete prohibited for INACTIVE")
        )
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["status"] == "inactive"
        adapter.delete.assert_not_called()


# ===========================================================================
# BOUNDARY 1: STATIC RESPONSE CONTRACT
# ===========================================================================


class TestInactiveStaticResponse:
    """Steps 8.18-8.19 — Response is static, sanitized, fallback_recommended=False."""

    @pytest.mark.parametrize("status", _INACTIVE_STATUSES)
    def test_inactive_response_is_static_and_sanitized(self, status):
        """Test 18: Response message is static; no identifier is exposed."""
        pipeline, _, _, _ = _build_inactive_pipeline(
            status, owner_hash=_VALID_OWNER_A, frontend_id=_FRONTEND_ID
        )
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["status"] == "inactive"
        assert result["message"] == _MSG_INACTIVE
        assert result["is_table"] is False
        assert result["genie_conversation_id"] is None
        assert result["genie_message_id"] is None
        assert result["generated_sql"] is None
        # No identifiers leaked
        result_str = str(result)
        assert _VALID_OWNER_A not in result_str
        assert _FRONTEND_ID not in result_str

    @pytest.mark.parametrize("status", _INACTIVE_STATUSES)
    def test_inactive_fallback_recommended_is_false(self, status):
        """Test 19: fallback_recommended must be False for INACTIVE."""
        pipeline, _, _, _ = _build_inactive_pipeline(status)
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["fallback_recommended"] is False

    @pytest.mark.parametrize("status", _INACTIVE_STATUSES)
    def test_inactive_response_has_required_fields(self, status):
        """Test 18b: Response includes all ChatResponse-compatible fields."""
        pipeline, _, _, _ = _build_inactive_pipeline(status)
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        for field in (
            "status", "message", "is_table", "fallback_recommended",
            "conversation_id", "source", "execution_time_ms",
        ):
            assert field in result


# ===========================================================================
# BOUNDARY 1: PROCESS-LOCAL SESSION CLEANUP
# ===========================================================================


class TestInactiveProcessLocalCleanup:
    """Steps 8.20-8.22 — remove_session called for current conv; others untouched."""

    @pytest.mark.parametrize("status", _INACTIVE_STATUSES)
    def test_current_session_removed(self, status):
        """Test 20: remove_session clears the current process-local session."""
        pipeline, store, _, _ = _build_inactive_pipeline(status)
        # Pre-seed a process-local session entry
        store.set_genie_conversation_id(_APP_CONV_ID, "genie-conv-stale")
        assert store.get_genie_conversation_id(_APP_CONV_ID) is not None

        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        # Session must be removed
        assert store.get_genie_conversation_id(_APP_CONV_ID) is None

    @pytest.mark.parametrize("status", _INACTIVE_STATUSES)
    def test_other_sessions_untouched(self, status):
        """Test 21: remove_session does NOT affect other conversations."""
        pipeline, store, _, _ = _build_inactive_pipeline(status)
        # Seed a different conversation in the same store
        store.set_genie_conversation_id(_APP_CONV_ID_3, "genie-conv-other")

        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        # Other conversation is unaffected
        assert store.get_genie_conversation_id(_APP_CONV_ID_3) == "genie-conv-other"

    def test_different_owners_isolated(self):
        """Test 22: Owner A INACTIVE does not affect Owner B's process-local session."""
        # Owner A has a RESET record
        pipeline_a, store_a, _, _ = _build_inactive_pipeline(
            ConversationStatus.RESET,
            owner_hash=_VALID_OWNER_A,
            frontend_id=_FRONTEND_ID,
            app_conv_id=_APP_CONV_ID,
        )
        # Simulate Owner B's entry in the same store (shared process)
        # Owner B uses a different app_conversation_id (different session cookie)
        store_a.set_genie_conversation_id(_APP_CONV_ID_2, "genie-conv-owner-b")

        pipeline_a.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        # Owner A's session is gone; Owner B's is untouched
        assert store_a.get_genie_conversation_id(_APP_CONV_ID) is None
        assert store_a.get_genie_conversation_id(_APP_CONV_ID_2) == "genie-conv-owner-b"

    def test_remove_session_no_entry_is_safe(self):
        """Test 20b: remove_session when no entry exists is idempotent."""
        pipeline, store, _, _ = _build_inactive_pipeline(ConversationStatus.RESET)
        # No session pre-seeded for this conversation
        assert store.get_session(_APP_CONV_ID) is None
        # Must not raise
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["status"] == "inactive"


# ===========================================================================
# BOUNDARY 1: DEGRADED LOOKUP FAILS CLOSED
# ===========================================================================


class TestDegradedLookupFailsClosed:
    """Steps 8.23-8.26 — Degraded results from load() fail closed."""

    def _make_degraded_pipeline(
        self,
        status: ConversationStatus,
        genie_conv_id: Optional[str] = _GENIE_CONV_ID,
    ):
        """Build a pipeline whose load() returns a degraded result."""
        store  = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo = InMemoryConversationRepository()
        repo_bundle = FakeRepositoryBundle(repo)
        adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
        record = _make_record(status, genie_conv_id=genie_conv_id)
        adapter.load = MagicMock(return_value=_degraded_lookup_result(record))
        bundle = _make_enabled_bundle(adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)
        return pipeline, store, client, adapter

    def test_degraded_active_fails_closed(self):
        """Test 23: Degraded ACTIVE lookup → fail closed, no Genie execution."""
        pipeline, _, client, _ = self._make_degraded_pipeline(ConversationStatus.ACTIVE)
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False
        assert len(client.start_calls) == 0
        assert len(client.send_calls) == 0

    def test_degraded_inactive_fails_closed(self):
        """Test 24: Degraded RESET lookup → fail closed, no Genie execution."""
        pipeline, _, client, _ = self._make_degraded_pipeline(ConversationStatus.RESET)
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False
        assert len(client.start_calls) == 0
        assert len(client.send_calls) == 0

    @pytest.mark.parametrize("status", [ConversationStatus.STALE, ConversationStatus.EXPIRED])
    def test_degraded_stale_expired_fails_closed(self, status):
        """Test 24b: Degraded STALE/EXPIRED → fail closed, no Genie execution."""
        pipeline, _, client, _ = self._make_degraded_pipeline(status)
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False
        assert len(client.start_calls) == 0

    def test_degraded_fails_closed_sanitized_response(self):
        """Test 27: Degraded result error message is static, no identifiers."""
        pipeline, _, _, _ = self._make_degraded_pipeline(ConversationStatus.ACTIVE)
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert _VALID_OWNER_A not in result["message"]
        assert _FRONTEND_ID not in result["message"]
        assert _GENIE_CONV_ID not in result["message"]

    def test_adapter_unavailable_fails_closed(self):
        """Test 26: Adapter unavailable (no confirmed snapshot) → fail closed."""
        store  = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo = InMemoryConversationRepository()
        repo_bundle = FakeRepositoryBundle(repo)
        adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
        adapter.load = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("repo down")
        )
        bundle = _make_enabled_bundle(adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False
        assert len(client.start_calls) == 0


# ===========================================================================
# BOUNDARY 2: POST-MISS GET_OR_CREATE TOMBSTONE RACE (TOCTOU)
# ===========================================================================


class TestTombstoneRaceWriteback:
    """Step 9 — Concurrent reset between MISS lookup and durable writeback."""

    def _make_miss_pipeline_with_tombstone(
        self,
        tombstone_status: ConversationStatus,
        genie_conv_id: str = _GENIE_CONV_ID,
    ):
        """Build a pipeline that:
        - load() returns None (MISS)
        - get_or_create() returns a non-ACTIVE record (tombstone race)
        Returns (pipeline, store, client, adapter).
        """
        store  = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo = InMemoryConversationRepository()
        repo_bundle = FakeRepositoryBundle(repo)
        adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
        # load() returns None → MISS
        adapter.load = MagicMock(return_value=None)
        # get_or_create() returns the tombstone record
        tombstone_record = _make_record(
            tombstone_status, genie_conv_id=genie_conv_id, version=1
        )
        adapter.get_or_create = MagicMock(
            return_value=_authoritative_lookup_result(tombstone_record)
        )
        adapter.bind_genie_conversation = MagicMock(
            side_effect=AssertionError("bind must not be called after tombstone")
        )
        adapter.update_last_genie_message = MagicMock(
            side_effect=AssertionError("update must not be called after tombstone")
        )
        bundle = _make_enabled_bundle(adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)
        return pipeline, store, client, adapter

    def test_race_reset_tombstone_blocks_bind(self):
        """Step 9.1-9.6: MISS → Genie executes → get_or_create returns RESET → bind never called."""
        pipeline, store, client, adapter = self._make_miss_pipeline_with_tombstone(
            ConversationStatus.RESET
        )
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        # Genie DID execute (MISS → start_conversation)
        assert len(client.start_calls) == 1
        # But bind must never have been called
        adapter.bind_genie_conversation.assert_not_called()
        adapter.update_last_genie_message.assert_not_called()
        # Response is inactive
        assert result["status"] == "inactive"
        assert result["fallback_recommended"] is False

    def test_race_stale_tombstone_blocks_bind(self):
        """get_or_create returning STALE also blocks bind."""
        pipeline, _, client, adapter = self._make_miss_pipeline_with_tombstone(
            ConversationStatus.STALE
        )
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert len(client.start_calls) == 1
        adapter.bind_genie_conversation.assert_not_called()
        assert result["status"] == "inactive"
        assert result["fallback_recommended"] is False

    def test_race_expired_tombstone_blocks_bind(self):
        """get_or_create returning EXPIRED also blocks bind."""
        pipeline, _, client, adapter = self._make_miss_pipeline_with_tombstone(
            ConversationStatus.EXPIRED
        )
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert len(client.start_calls) == 1
        adapter.bind_genie_conversation.assert_not_called()
        assert result["status"] == "inactive"

    def test_race_no_custom_fallback(self):
        """Step 9.8: No custom fallback after tombstone race."""
        pipeline, _, _, _ = self._make_miss_pipeline_with_tombstone(
            ConversationStatus.RESET
        )
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["fallback_recommended"] is False

    def test_race_static_response_sanitized(self):
        """Step 9.9: Response is static and sanitized after tombstone race."""
        pipeline, _, _, _ = self._make_miss_pipeline_with_tombstone(
            ConversationStatus.RESET
        )
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["message"] == _MSG_INACTIVE
        assert result["genie_conversation_id"] is None
        assert result["genie_message_id"] is None
        result_str = str(result)
        assert _VALID_OWNER_A not in result_str
        assert _FRONTEND_ID not in result_str

    def test_race_session_cleared(self):
        """Step 9.5-equivalent: process-local session is cleared after tombstone race."""
        pipeline, store, _, _ = self._make_miss_pipeline_with_tombstone(
            ConversationStatus.RESET
        )
        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        # Genie mapping must be cleared
        assert store.get_genie_conversation_id(_APP_CONV_ID) is None

    def test_race_tombstone_record_status_preserved_in_repo(self):
        """Step 9.10: The RESET tombstone in the durable record is not overwritten."""
        store  = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo = InMemoryConversationRepository()
        # Pre-create the record, then reset it (simulates concurrent reset)
        created = repo.create_conversation(_VALID_OWNER_A, _FRONTEND_ID)
        reset_record = repo.set_status(
            _VALID_OWNER_A, created.conversation_id,
            ConversationStatus.RESET, expected_version=created.version,
        )
        repo_bundle = FakeRepositoryBundle(repo)
        real_adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
        # load() sees no ACTIVE record: returns None (pre-reset, it hadn't been loaded yet)
        real_adapter.load = MagicMock(return_value=None)
        bundle = _make_enabled_bundle(real_adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)
        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        # The record in the repo should still be RESET (not overwritten to ACTIVE)
        final = repo.get_by_frontend_id(_VALID_OWNER_A, _FRONTEND_ID)
        assert final is not None
        assert final.status == ConversationStatus.RESET
        # No Genie conversation was bound
        assert final.genie_conversation_id is None


# ===========================================================================
# BOUNDARY 2: GET_OR_CREATE — DEGRADED AND UNAVAILABLE
# ===========================================================================


class TestGetOrCreateDegradedAndUnavailable:
    """Step 9 (continued) — get_or_create returning degraded or unavailable."""

    def _make_miss_pipeline_goc_returns(
        self,
        get_or_create_side_effect=None,
        get_or_create_return=None,
    ):
        store  = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo = InMemoryConversationRepository()
        repo_bundle = FakeRepositoryBundle(repo)
        adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
        adapter.load = MagicMock(return_value=None)
        if get_or_create_side_effect is not None:
            adapter.get_or_create = MagicMock(side_effect=get_or_create_side_effect)
        elif get_or_create_return is not None:
            adapter.get_or_create = MagicMock(return_value=get_or_create_return)
        adapter.bind_genie_conversation = MagicMock(
            side_effect=AssertionError("bind must not be called")
        )
        bundle = _make_enabled_bundle(adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)
        return pipeline, store, client, adapter

    def test_get_or_create_degraded_active_fails_closed(self):
        """Degraded ACTIVE get_or_create → writeback fails, no bind."""
        degraded_active = _degraded_lookup_result(
            _make_record(ConversationStatus.ACTIVE)
        )
        pipeline, _, client, adapter = self._make_miss_pipeline_goc_returns(
            get_or_create_return=degraded_active
        )
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        # Genie DID execute (MISS)
        assert len(client.start_calls) == 1
        # Bind must not have been called
        adapter.bind_genie_conversation.assert_not_called()
        # Writeback fails closed (ordinary error, not inactive)
        assert result["fallback_recommended"] is False

    def test_get_or_create_degraded_reset_fails_closed(self):
        """Degraded RESET get_or_create → writeback fails, no bind."""
        degraded_reset = _degraded_lookup_result(
            _make_record(ConversationStatus.RESET)
        )
        pipeline, _, client, adapter = self._make_miss_pipeline_goc_returns(
            get_or_create_return=degraded_reset
        )
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert len(client.start_calls) == 1
        adapter.bind_genie_conversation.assert_not_called()
        assert result["fallback_recommended"] is False

    def test_get_or_create_unavailable_fails_closed(self):
        """get_or_create raising unavailable → fail closed, no bind."""
        pipeline, _, client, adapter = self._make_miss_pipeline_goc_returns(
            get_or_create_side_effect=DurableGenieSessionUnavailableError("unavailable")
        )
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert len(client.start_calls) == 1
        adapter.bind_genie_conversation.assert_not_called()
        assert result["fallback_recommended"] is False


# ===========================================================================
# POSITIVE: ACTIVE PATH RETAINED FROM PHASE 4C2B
# ===========================================================================


class TestActiveMissPathRetained:
    """Step 9 (positive) — Authoritative ACTIVE get_or_create still succeeds."""

    def test_active_get_or_create_proceeds_to_bind(self):
        """Authoritative ACTIVE get_or_create → bind_genie_conversation called."""
        store  = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo = InMemoryConversationRepository()
        repo_bundle = FakeRepositoryBundle(repo)
        real_adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
        # load() returns None (MISS)
        real_adapter.load = MagicMock(return_value=None)
        bundle = _make_enabled_bundle(real_adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)

        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert result["status"] == "success"
        assert len(client.start_calls) == 1
        assert result["fallback_recommended"] is False
        # Durable record was created and bound in the repo
        final = repo.get_by_frontend_id(_VALID_OWNER_A, _FRONTEND_ID)
        assert final is not None
        assert final.status == ConversationStatus.ACTIVE
        assert final.genie_conversation_id == _GENIE_CONV_ID

    def test_miss_without_durable_bundle_unaffected(self):
        """MISS without durable bundle still executes Genie normally."""
        store  = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        result = pipeline.run("show shipments", _APP_CONV_ID)
        assert result["status"] == "success"
        assert len(client.start_calls) == 1
        assert result["fallback_recommended"] is False


# ===========================================================================
# NO IDENTIFIER LEAKAGE IN REPR AND RESPONSE
# ===========================================================================


class TestNoIdentifierLeakage:
    """Test 27: No owner hash, frontend ID, Genie IDs, or internals in repr/response."""

    @pytest.mark.parametrize("status", _INACTIVE_STATUSES)
    def test_inactive_response_no_owner_hash(self, status):
        pipeline, _, _, _ = _build_inactive_pipeline(status)
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        result_str = str(result)
        assert _VALID_OWNER_A not in result_str
        assert _FRONTEND_ID not in result_str
        assert _GENIE_CONV_ID not in result_str
        assert _GENIE_MSG_ID not in result_str

    @pytest.mark.parametrize("status", _INACTIVE_STATUSES)
    def test_inactive_response_no_status_value(self, status):
        """The durable status name (RESET/STALE/EXPIRED) must not appear in message."""
        pipeline, _, _, _ = _build_inactive_pipeline(status)
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_A,
            frontend_conversation_id=_FRONTEND_ID,
        )
        assert status.value not in result["message"]
