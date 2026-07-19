"""Phase 4C2A — GeniePipeline durable session lookup tests.

Tests the read-only durable Genie session lookup added to GeniePipeline.run().
Validates: disabled mode, enabled prerequisites, lookup hit with recovery,
lookup miss, lookup failure, restart simulation, standalone recovery,
confirmed degraded recovery, and no-write enforcement.

Every test makes exact behavioural assertions — no bare pass, no or-True,
no pre-seeded local intent for restart tests.
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

_VALID_OWNER_KEY = "a" * 64
_VALID_OWNER_KEY_2 = "b" * 64
_SPACE_ID = "test-space-id"
_FRONTEND_CONV_ID = "frontend-conv-abc-123"
_FRONTEND_CONV_ID_2 = "frontend-conv-def-456"
_GENIE_CONV_ID = "genie-conv-recovered-001"
_GENIE_MSG_ID = "genie-msg-last-001"
_APP_CONV_ID = "nosession:frontend-conv-abc-123"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class RecordingGenieClient:
    """Genie client that records all method calls with arguments."""

    def __init__(self):
        self.start_calls: List[Dict[str, Any]] = []
        self.send_calls: List[Dict[str, Any]] = []

    def start_conversation(self, space_id, message):
        self.start_calls.append({"space_id": space_id, "message": message})
        return {"conversation_id": "genie-conv-new", "message_id": "msg-new-1"}

    def send_message(self, space_id, conv_id, message):
        self.send_calls.append({"space_id": space_id, "conv_id": conv_id, "message": message})
        return {"message_id": "msg-followup-1"}

    def wait_for_message_completion(self, space_id, conv_id, msg_id, **kwargs):
        return MagicMock(query_attachments=None)

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


def _build_pipeline(
    genie_client=None,
    session_store=None,
    **kwargs,
) -> GeniePipeline:
    """Build a minimal pipeline for testing."""
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
    """Create an enabled runtime bundle with the given adapter."""
    return DurableGenieSessionRuntimeBundle(
        enabled=True,
        adapter=adapter,
        backend=MagicMock(),
        durable=True,
    )


def _make_disabled_bundle() -> DurableGenieSessionRuntimeBundle:
    """Create a disabled runtime bundle."""
    return DurableGenieSessionRuntimeBundle(
        enabled=False,
        adapter=None,
        backend=None,
        durable=False,
    )


def _seed_repo_record(
    repo: InMemoryConversationRepository,
    owner_hash: str = _VALID_OWNER_KEY,
    frontend_id: str = _FRONTEND_CONV_ID,
    genie_conv_id: str = _GENIE_CONV_ID,
    last_msg_id: Optional[str] = _GENIE_MSG_ID,
) -> ConversationRecord:
    """Pre-seed a durable record in the repository and return it."""
    created = repo.create_conversation(owner_hash, frontend_id)
    current_version = created.version
    if genie_conv_id:
        bound = repo.bind_genie_conversation(
            owner_hash, created.conversation_id, genie_conv_id,
            expected_version=current_version,
        )
        current_version = bound.version
    if last_msg_id and genie_conv_id:
        repo.update_last_genie_message(
            owner_hash, created.conversation_id, last_msg_id,
            expected_version=current_version,
        )
    return repo.get_by_frontend_id(owner_hash, frontend_id)


def _build_recovered_pipeline(
    repo: Optional[InMemoryConversationRepository] = None,
    owner_hash: str = _VALID_OWNER_KEY,
    frontend_id: str = _FRONTEND_CONV_ID,
    genie_conv_id: str = _GENIE_CONV_ID,
    last_msg_id: Optional[str] = _GENIE_MSG_ID,
):
    """Build a pipeline with a fresh empty store and a pre-seeded durable record."""
    if repo is None:
        repo = InMemoryConversationRepository()
        _seed_repo_record(repo, owner_hash, frontend_id, genie_conv_id, last_msg_id)
    store = GenieSessionStore()
    client = RecordingGenieClient()
    pipeline = _build_pipeline(genie_client=client, session_store=store)
    repo_bundle = FakeRepositoryBundle(repo)
    adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
    bundle = _make_enabled_bundle(adapter)
    setattr(pipeline, "_durable_session_runtime_bundle", bundle)
    return pipeline, store, client, adapter, repo


# ===========================================================================
# DISABLED MODE
# ===========================================================================


class TestDisabledMode:
    """When durable mode is disabled, no durable access occurs."""

    def test_disabled_no_bundle(self):
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client)
        result = pipeline.run("show shipments", _APP_CONV_ID)
        assert result["status"] == "success"
        assert len(client.start_calls) == 1

    def test_disabled_bundle_attached(self):
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client)
        bundle = _make_disabled_bundle()
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)
        result = pipeline.run("show shipments", _APP_CONV_ID)
        assert result["status"] == "success"
        assert len(client.start_calls) == 1

    def test_disabled_owner_key_optional(self):
        pipeline = _build_pipeline()
        bundle = _make_disabled_bundle()
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)
        result = pipeline.run("hello", _APP_CONV_ID)
        assert result["status"] == "success"

    def test_disabled_frontend_id_optional(self):
        pipeline = _build_pipeline()
        bundle = _make_disabled_bundle()
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)
        result = pipeline.run(
            "hello", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=None,
        )
        assert result["status"] == "success"

    def test_disabled_adapter_never_accessed(self):
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client)
        bundle = _make_disabled_bundle()
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)
        # If adapter were accessed it would fail (adapter is None)
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert result["status"] == "success"
        assert len(client.start_calls) == 1


# ===========================================================================
# ENABLED PREREQUISITES
# ===========================================================================


class TestEnabledPrerequisites:
    """When durable mode is enabled, owner_key and frontend_id are required."""

    def _make_enabled_pipeline(self):
        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo = InMemoryConversationRepository()
        repo_bundle = FakeRepositoryBundle(repo)
        adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
        bundle = _make_enabled_bundle(adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)
        return pipeline, client

    def test_missing_owner_key_errors(self):
        pipeline, client = self._make_enabled_pipeline()
        result = pipeline.run(
            "hello", _APP_CONV_ID,
            owner_key=None,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False
        assert len(client.start_calls) == 0
        assert len(client.send_calls) == 0

    def test_missing_frontend_id_errors(self):
        pipeline, client = self._make_enabled_pipeline()
        result = pipeline.run(
            "hello", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=None,
        )
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False
        assert len(client.start_calls) == 0
        assert len(client.send_calls) == 0

    def test_empty_frontend_id_errors(self):
        pipeline, client = self._make_enabled_pipeline()
        result = pipeline.run(
            "hello", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id="",
        )
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False
        assert len(client.start_calls) == 0

    def test_invalid_owner_key_errors(self):
        pipeline, client = self._make_enabled_pipeline()
        result = pipeline.run(
            "hello", _APP_CONV_ID,
            owner_key="invalid-not-hex-64",
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False
        assert len(client.start_calls) == 0


# ===========================================================================
# LOOKUP HIT — RECOVERY (Issue 1, 6, 7)
# ===========================================================================


class TestLookupHitRecovery:
    """Active durable record found: Genie conversation is recovered.
    send_message is used; start_conversation is never called.
    """

    def test_recovery_uses_send_message(self):
        """Recovered mapping → send_message on recovered Genie conv."""
        pipeline, store, client, _, _ = _build_recovered_pipeline()
        result = pipeline.run(
            "which are in transit?", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert result["status"] == "success"
        assert len(client.send_calls) == 1
        assert client.send_calls[0]["conv_id"] == _GENIE_CONV_ID
        assert len(client.start_calls) == 0

    def test_recovery_sends_current_message(self):
        """The user's message is sent unchanged to Genie."""
        pipeline, _, client, _, _ = _build_recovered_pipeline()
        pipeline.run(
            "which are in transit?", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert client.send_calls[0]["message"] == "which are in transit?"

    def test_recovery_restores_mapping_in_store(self):
        """After recovery, session store has the recovered genie_conv_id."""
        pipeline, store, _, _, _ = _build_recovered_pipeline()
        # Store is empty before
        assert store.get_genie_conversation_id(_APP_CONV_ID) is None
        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert store.get_genie_conversation_id(_APP_CONV_ID) == _GENIE_CONV_ID

    def test_recovery_no_durable_mutations(self):
        """No create/bind/update/touch/status/delete on adapter."""
        pipeline, _, _, adapter, _ = _build_recovered_pipeline()
        # Guard: wire traps on mutation methods
        adapter.get_or_create = MagicMock(side_effect=AssertionError("create"))
        adapter.bind_genie_conversation = MagicMock(side_effect=AssertionError("bind"))
        adapter.update_last_genie_message = MagicMock(side_effect=AssertionError("update"))
        adapter.touch = MagicMock(side_effect=AssertionError("touch"))
        adapter.set_status = MagicMock(side_effect=AssertionError("status"))
        adapter.delete = MagicMock(side_effect=AssertionError("delete"))
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert result["status"] == "success"

    def test_recovery_response_no_owner_hash(self):
        """Response must not contain owner hash or frontend durable key."""
        pipeline, _, _, _, _ = _build_recovered_pipeline()
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert _VALID_OWNER_KEY not in str(result)
        assert _FRONTEND_CONV_ID not in str(result.get("message", ""))
        assert "owner_key" not in result
        assert "owner_user_id_hash" not in result

    def test_recovery_fallback_false(self):
        """Recovered request has fallback_recommended=False."""
        pipeline, _, _, _, _ = _build_recovered_pipeline()
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert result["fallback_recommended"] is False


# ===========================================================================
# ISSUE 6 — TRUE EMPTY-MEMORY RESTART TEST
# ===========================================================================


class TestEmptyMemoryRestart:
    """Simulate container restart: completely fresh GenieSessionStore,
    zero sessions, zero context, zero last_intent, zero Genie mappings.
    Durable record exists in the repository from a prior instance.
    """

    def test_restart_recovery_with_follow_up_prompt(self):
        """'which are in transit?' recovers via send_message, not start."""
        # Step 1: Persistent repository with pre-seeded record
        repo = InMemoryConversationRepository()
        _seed_repo_record(repo)

        # Step 2: Fresh pipeline instance (simulates restart)
        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo_bundle = FakeRepositoryBundle(repo)
        adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
        bundle = _make_enabled_bundle(adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)

        # Step 3: Confirm store is COMPLETELY empty
        assert store.get_genie_conversation_id(_APP_CONV_ID) is None
        ctx = store.get_context_snapshot(_APP_CONV_ID)
        assert ctx.get("last_intent") is None
        assert ctx.get("genie_conversation_id") is None

        # Step 4: Execute
        result = pipeline.run(
            "which are in transit?", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        # Step 5: Exact assertions
        assert result["status"] == "success"
        assert len(client.send_calls) == 1
        assert client.send_calls[0]["conv_id"] == _GENIE_CONV_ID
        assert len(client.start_calls) == 0
        assert result["fallback_recommended"] is False
        assert _VALID_OWNER_KEY not in str(result)
        # Mapping is restored
        assert store.get_genie_conversation_id(_APP_CONV_ID) == _GENIE_CONV_ID

    def test_restart_recovery_adapter_load_called_once(self):
        """adapter.load is called exactly once during recovery."""
        repo = InMemoryConversationRepository()
        _seed_repo_record(repo)

        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo_bundle = FakeRepositoryBundle(repo)
        adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
        original_load = adapter.load
        load_calls = []

        def tracking_load(key):
            load_calls.append(key)
            return original_load(key)

        adapter.load = tracking_load
        bundle = _make_enabled_bundle(adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)

        pipeline.run(
            "which are in transit?", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )

        assert len(load_calls) == 1


# ===========================================================================
# ISSUE 7 — STANDALONE MESSAGE IN SAME UI CHAT
# ===========================================================================


class TestStandaloneMessageRecovery:
    """A standalone-looking prompt (not a linguistic follow-up) must still
    use send_message on the recovered Genie conversation. Recovery is
    authoritative for the UI chat regardless of local classification.
    """

    def test_standalone_prompt_uses_send_message(self):
        """'Show shipments from Germany' → send_message on recovered conv."""
        pipeline, store, client, _, _ = _build_recovered_pipeline()
        # Store is empty — no last_intent, no context
        assert store.get_genie_conversation_id(_APP_CONV_ID) is None

        result = pipeline.run(
            "Show shipments from Germany", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert result["status"] == "success"
        assert len(client.send_calls) == 1
        assert client.send_calls[0]["conv_id"] == _GENIE_CONV_ID
        assert len(client.start_calls) == 0

    def test_standalone_prompt_mapping_preserved(self):
        """After standalone recovery, mapping stays in store."""
        pipeline, store, client, _, _ = _build_recovered_pipeline()
        pipeline.run(
            "Show shipments from Germany", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert store.get_genie_conversation_id(_APP_CONV_ID) == _GENIE_CONV_ID

    def test_another_standalone_prompt(self):
        """'how many shipments are delayed' — still uses send_message."""
        pipeline, store, client, _, _ = _build_recovered_pipeline()
        result = pipeline.run(
            "how many shipments are delayed", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert len(client.send_calls) == 1
        assert client.send_calls[0]["conv_id"] == _GENIE_CONV_ID
        assert len(client.start_calls) == 0


# ===========================================================================
# LOOKUP MISS (Issue 8)
# ===========================================================================


class TestLookupMiss:
    """No durable record found: proceed with new conversation."""

    def _make_miss_pipeline(self):
        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo = InMemoryConversationRepository()
        repo_bundle = FakeRepositoryBundle(repo)
        adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
        bundle = _make_enabled_bundle(adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)
        return pipeline, store, client, adapter

    def test_miss_starts_new_conversation(self):
        """Lookup miss → start_conversation called exactly once."""
        pipeline, _, client, _ = self._make_miss_pipeline()
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert result["status"] == "success"
        assert len(client.start_calls) == 1
        assert len(client.send_calls) == 0

    def test_miss_no_durable_mutation(self):
        """Lookup miss → no create/bind/update/delete on adapter."""
        pipeline, _, _, adapter = self._make_miss_pipeline()
        adapter.get_or_create = MagicMock(side_effect=AssertionError("create"))
        adapter.bind_genie_conversation = MagicMock(side_effect=AssertionError("bind"))
        adapter.update_last_genie_message = MagicMock(side_effect=AssertionError("update"))
        adapter.delete = MagicMock(side_effect=AssertionError("delete"))
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert result["status"] == "success"

    def test_miss_response_contract(self):
        """Lookup miss → normal response with required fields."""
        pipeline, _, _, _ = self._make_miss_pipeline()
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert "status" in result
        assert "message" in result
        assert "fallback_recommended" in result
        assert result["fallback_recommended"] is False


# ===========================================================================
# LOOKUP FAILURE (Issue 8)
# ===========================================================================


class TestLookupFailure:
    """Repository unavailable, no confirmed snapshot → fail closed."""

    def _make_failing_pipeline(self, load_side_effect):
        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo = InMemoryConversationRepository()
        repo_bundle = FakeRepositoryBundle(repo)
        adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
        adapter.load = MagicMock(side_effect=load_side_effect)
        bundle = _make_enabled_bundle(adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)
        return pipeline, client

    def test_unavailable_fails_closed(self):
        """DurableGenieSessionUnavailableError → error, no Genie calls."""
        pipeline, client = self._make_failing_pipeline(
            DurableGenieSessionUnavailableError("repo down")
        )
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False
        assert len(client.start_calls) == 0
        assert len(client.send_calls) == 0

    def test_unexpected_exception_fails_closed(self):
        """Random exception in load → error, no Genie calls."""
        pipeline, client = self._make_failing_pipeline(
            RuntimeError("unexpected")
        )
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False
        assert len(client.start_calls) == 0
        assert len(client.send_calls) == 0

    def test_unavailable_sanitized_response(self):
        """Error message is static, does not leak exception details."""
        pipeline, _ = self._make_failing_pipeline(
            DurableGenieSessionUnavailableError("secret details")
        )
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert "secret" not in result["message"]
        assert _VALID_OWNER_KEY not in result["message"]


# ===========================================================================
# CONFIRMED DEGRADED RECOVERY (Issue 4, 8)
# ===========================================================================


class TestConfirmedDegradedRecovery:
    """Adapter returns a confirmed degraded snapshot → recovery proceeds."""

    def test_degraded_confirmed_snapshot_recovers(self):
        """degraded=True from adapter confirmed snapshot → send_message."""
        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo = InMemoryConversationRepository()
        repo_bundle = FakeRepositoryBundle(repo)
        adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)

        # Create a confirmed degraded result
        now = datetime.now(timezone.utc)
        record = ConversationRecord(
            conversation_id=str(uuid.uuid4()),
            owner_user_id_hash=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
            genie_conversation_id=_GENIE_CONV_ID,
            last_genie_message_id=_GENIE_MSG_ID,
            status=ConversationStatus.ACTIVE,
            version=1,
            created_at=now,
            updated_at=now,
            last_active_at=now,
        )
        degraded_result = GenieSessionLookupResult(
            record=record,
            source=GenieSessionLookupSource.CACHE,
            degraded=True,
        )
        adapter.load = MagicMock(return_value=degraded_result)
        bundle = _make_enabled_bundle(adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)

        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert result["status"] == "success"
        assert len(client.send_calls) == 1
        assert client.send_calls[0]["conv_id"] == _GENIE_CONV_ID
        assert len(client.start_calls) == 0

    def test_degraded_mapping_restored(self):
        """Degraded recovery restores mapping in session store."""
        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo = InMemoryConversationRepository()
        repo_bundle = FakeRepositoryBundle(repo)
        adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)

        now = datetime.now(timezone.utc)
        record = ConversationRecord(
            conversation_id=str(uuid.uuid4()),
            owner_user_id_hash=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
            genie_conversation_id=_GENIE_CONV_ID,
            last_genie_message_id=None,
            status=ConversationStatus.ACTIVE,
            version=1,
            created_at=now, updated_at=now, last_active_at=now,
        )
        degraded_result = GenieSessionLookupResult(
            record=record, source=GenieSessionLookupSource.CACHE, degraded=True,
        )
        adapter.load = MagicMock(return_value=degraded_result)
        bundle = _make_enabled_bundle(adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)

        pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert store.get_genie_conversation_id(_APP_CONV_ID) == _GENIE_CONV_ID


# ===========================================================================
# OWNERSHIP ISOLATION
# ===========================================================================


class TestOwnership:
    """Owner-scoped isolation between different users."""

    def test_different_owner_gets_miss(self):
        """Owner B cannot recover Owner A's conversation."""
        repo = InMemoryConversationRepository()
        _seed_repo_record(repo, owner_hash=_VALID_OWNER_KEY)

        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo_bundle = FakeRepositoryBundle(repo)
        adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
        bundle = _make_enabled_bundle(adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)

        # Owner B tries to look up Owner A's record
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY_2,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert result["status"] == "success"
        # start_conversation was called (miss → new conversation)
        assert len(client.start_calls) == 1
        assert len(client.send_calls) == 0
        # Owner A's genie conv not in store
        assert store.get_genie_conversation_id(_APP_CONV_ID) != _GENIE_CONV_ID

    def test_email_as_owner_key_rejected(self):
        """Email strings are structurally invalid owner keys."""
        pipeline, _, client, _, _ = _build_recovered_pipeline()
        result = pipeline.run(
            "hello", _APP_CONV_ID,
            owner_key="user@example.com",
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert result["status"] == "error"
        assert result["fallback_recommended"] is False
        assert len(client.start_calls) == 0


# ===========================================================================
# SHAPE RETRY WITH RECOVERY (Issue 3)
# ===========================================================================


class TestShapeRetryRecovery:
    """Shape retry on a recovered request must not start a new conversation."""

    def test_shape_retry_preserves_recovered_mapping(self):
        """When shape validation triggers retry, recovered mapping is kept."""
        pipeline, store, client, _, _ = _build_recovered_pipeline()
        # Enable shape validation
        pipeline._enable_shape_validation = True

        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        # Regardless of whether shape retry fired, no start_conversation
        assert len(client.start_calls) == 0
        # All Genie operations went to the recovered conv
        for call in client.send_calls:
            assert call["conv_id"] == _GENIE_CONV_ID


# ===========================================================================
# NON-ACTIVE STATUS
# ===========================================================================


class TestNonActiveStatus:
    """STALE/RESET/EXPIRED records are not recovered."""

    @pytest.mark.parametrize("status", [
        ConversationStatus.STALE,
        ConversationStatus.RESET,
        ConversationStatus.EXPIRED,
    ])
    def test_non_active_starts_new_conversation(self, status):
        """Non-ACTIVE records → miss → start_conversation."""
        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo = InMemoryConversationRepository()
        repo_bundle = FakeRepositoryBundle(repo)
        adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)

        now = datetime.now(timezone.utc)
        record = ConversationRecord(
            conversation_id=str(uuid.uuid4()),
            owner_user_id_hash=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
            genie_conversation_id=_GENIE_CONV_ID,
            last_genie_message_id=_GENIE_MSG_ID,
            status=status,
            version=1,
            created_at=now, updated_at=now, last_active_at=now,
        )
        result_obj = GenieSessionLookupResult(
            record=record, source=GenieSessionLookupSource.REPOSITORY, degraded=False,
        )
        adapter.load = MagicMock(return_value=result_obj)
        bundle = _make_enabled_bundle(adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)

        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert result["status"] == "success"
        assert len(client.start_calls) == 1
        assert len(client.send_calls) == 0


# ===========================================================================
# NO-WRITE ENFORCEMENT
# ===========================================================================


class TestNoWriteEnforcement:
    """Verify no durable mutation occurs in any path."""

    def _make_guarded_pipeline(self, load_return=None):
        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(genie_client=client, session_store=store)
        repo = InMemoryConversationRepository()
        repo_bundle = FakeRepositoryBundle(repo)
        adapter = DurableGenieSessionAdapter(repo_bundle, cache_store=None)
        adapter.load = MagicMock(return_value=load_return)
        adapter.get_or_create = MagicMock(side_effect=AssertionError("create prohibited"))
        adapter.bind_genie_conversation = MagicMock(side_effect=AssertionError("bind prohibited"))
        adapter.update_last_genie_message = MagicMock(side_effect=AssertionError("update prohibited"))
        adapter.set_status = MagicMock(side_effect=AssertionError("set_status prohibited"))
        adapter.delete = MagicMock(side_effect=AssertionError("delete prohibited"))
        adapter.touch = MagicMock(side_effect=AssertionError("touch prohibited"))
        bundle = _make_enabled_bundle(adapter)
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)
        return pipeline, adapter

    def test_miss_no_mutations(self):
        pipeline, adapter = self._make_guarded_pipeline(load_return=None)
        result = pipeline.run(
            "hello", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert result["status"] == "success"
        adapter.get_or_create.assert_not_called()
        adapter.bind_genie_conversation.assert_not_called()
        adapter.update_last_genie_message.assert_not_called()
        adapter.set_status.assert_not_called()
        adapter.delete.assert_not_called()
        adapter.touch.assert_not_called()

    def test_hit_no_mutations(self):
        now = datetime.now(timezone.utc)
        record = ConversationRecord(
            conversation_id=str(uuid.uuid4()),
            owner_user_id_hash=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
            genie_conversation_id=_GENIE_CONV_ID,
            last_genie_message_id=_GENIE_MSG_ID,
            status=ConversationStatus.ACTIVE,
            version=1,
            created_at=now, updated_at=now, last_active_at=now,
        )
        hit_result = GenieSessionLookupResult(
            record=record, source=GenieSessionLookupSource.REPOSITORY, degraded=False,
        )
        pipeline, adapter = self._make_guarded_pipeline(load_return=hit_result)
        result = pipeline.run(
            "show shipments", _APP_CONV_ID,
            owner_key=_VALID_OWNER_KEY,
            frontend_conversation_id=_FRONTEND_CONV_ID,
        )
        assert result["status"] == "success"
        adapter.get_or_create.assert_not_called()
        adapter.bind_genie_conversation.assert_not_called()
        adapter.update_last_genie_message.assert_not_called()
        adapter.set_status.assert_not_called()
        adapter.delete.assert_not_called()
        adapter.touch.assert_not_called()
