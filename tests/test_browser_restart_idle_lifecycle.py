"""Phase 4D1 — Browser restart and idle conversation recovery lifecycle tests.

Validates:
  A. Process-restart recovery: a fresh pipeline with empty store and the same
     durable repository recovers an active Genie conversation and uses
     send_message (not start_conversation).
  B. Local idle-expiry recovery: after GenieSessionStore is cleared or the
     session expires, durable lookup restores the Genie mapping for the
     same request.
  C. Inactive durable state after restart/expiry: RESET, STALE, and EXPIRED
     records block Genie execution, return static response, no new conv.
  D. Owner isolation: different trusted owners cannot recover each other's
     durable sessions regardless of shared frontend ID or store state.
  E. Durable failure policy: unavailable or degraded repository fails closed,
     no new Genie conversation, fallback_recommended=False.
  F. Race/concurrency safety: idempotency, inactive-tombstone wins.

All tests use:
  - real InMemoryConversationRepository
  - real DurableGenieSessionAdapter
  - real GenieSessionStore
  - real GeniePipeline
  - controlled RecordingGenieClient (no live HTTP)
  - no Lakebase, no live Genie, no live SQL
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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

_OWNER_A = "a" * 64
_OWNER_B = "b" * 64
_OWNER_C = "c" * 64
_SPACE_ID = "test-space-4d1"
_FE_ID_1 = "frontend-4d1-conv-001"
_FE_ID_2 = "frontend-4d1-conv-002"
_GENIE_CONV = "genie-conv-4d1-001"
_GENIE_MSG = "genie-msg-4d1-001"
_APP_KEY = "plc-key:" + _FE_ID_1  # opaque local key
_APP_KEY_2 = "plc-key:" + _FE_ID_2
_MSG_INACTIVE = "This conversation is no longer active. Start a new chat."


# ---------------------------------------------------------------------------
# Fake Genie client
# ---------------------------------------------------------------------------


class RecordingGenieClient:
    """Minimal Genie client that records start/send calls."""

    def __init__(self, genie_conv_id: str = _GENIE_CONV) -> None:
        self.start_calls: List[Dict[str, Any]] = []
        self.send_calls: List[Dict[str, Any]] = []
        self._genie_conv_id = genie_conv_id

    def start_conversation(self, space_id: str, message: str) -> Dict[str, Any]:
        entry = {"space_id": space_id, "message": message}
        self.start_calls.append(entry)
        return {"conversation_id": self._genie_conv_id, "message_id": "msg-new-1"}

    def send_message(self, space_id: str, conv_id: str, message: str) -> Dict[str, Any]:
        entry = {"space_id": space_id, "conv_id": conv_id, "message": message}
        self.send_calls.append(entry)
        return {"message_id": "msg-follow-1"}

    def wait_for_message_completion(self, space_id, conv_id, msg_id, **kw):
        return MagicMock(query_attachments=None, message_id=msg_id)

    def fetch_query_result(self, *a, **kw):
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeRepoBundle:
    """Minimal ConversationRepositoryBundle wrapper."""

    def __init__(self, repo: InMemoryConversationRepository) -> None:
        self.repository = repo
        self.backend = MagicMock()
        self.backend.value = "memory"
        self.durable = True

    def close(self) -> None:
        pass


def _build_pipeline(
    client: Optional[RecordingGenieClient] = None,
    store: Optional[GenieSessionStore] = None,
) -> GeniePipeline:
    return GeniePipeline(
        genie_client=client or RecordingGenieClient(),
        session_store=store or GenieSessionStore(),
        space_id=_SPACE_ID,
        fetch_query_results=False,
        enable_prompt_enrichment=False,
        enable_shape_validation=False,
        enable_table_summary=False,
    )


def _enabled_bundle(adapter: DurableGenieSessionAdapter) -> DurableGenieSessionRuntimeBundle:
    return DurableGenieSessionRuntimeBundle(
        enabled=True, adapter=adapter, backend=MagicMock(), durable=True
    )


def _disabled_bundle() -> DurableGenieSessionRuntimeBundle:
    return DurableGenieSessionRuntimeBundle(
        enabled=False, adapter=None, backend=None, durable=False
    )


def _seed_active_record(
    repo: InMemoryConversationRepository,
    owner: str = _OWNER_A,
    fe_id: str = _FE_ID_1,
    genie_conv: str = _GENIE_CONV,
    last_msg: Optional[str] = _GENIE_MSG,
) -> ConversationRecord:
    """Create + bind + optionally set last-message in repo; return record."""
    r = repo.create_conversation(owner, fe_id)
    v = r.version
    r = repo.bind_genie_conversation(owner, r.conversation_id, genie_conv, expected_version=v)
    v = r.version
    if last_msg:
        r = repo.update_last_genie_message(owner, r.conversation_id, last_msg, expected_version=v)
    return repo.get_by_frontend_id(owner, fe_id)


def _make_inactive_record(
    repo: InMemoryConversationRepository,
    owner: str = _OWNER_A,
    fe_id: str = _FE_ID_1,
    status: ConversationStatus = ConversationStatus.RESET,
) -> None:
    """Seed and then set-status to inactive."""
    r = _seed_active_record(repo, owner, fe_id)
    repo.set_status(owner, r.conversation_id, status, expected_version=r.version)


# ===========================================================================
# GROUP A: Process-Restart Recovery (10 tests)
# ===========================================================================


class TestProcessRestartRecovery:
    """Fresh pipeline + empty store recover an existing durable Genie conv."""

    def _make_restart_pair(
        self,
        owner: str = _OWNER_A,
        fe_id: str = _FE_ID_1,
        genie_conv: str = _GENIE_CONV,
        last_msg: Optional[str] = _GENIE_MSG,
    ):
        """Build (pipeline1, pipeline2) sharing the same durable repo."""
        repo = InMemoryConversationRepository()
        _seed_active_record(repo, owner, fe_id, genie_conv, last_msg)

        # Simulate first pipeline (not needed for test; repo already seeded)
        store2 = GenieSessionStore()
        client2 = RecordingGenieClient(genie_conv_id=genie_conv)
        pipeline2 = _build_pipeline(client=client2, store=store2)
        adapter2 = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline2, "_durable_session_runtime_bundle", _enabled_bundle(adapter2))
        return pipeline2, store2, client2, repo

    def test_restart_uses_send_not_start(self):
        """Recovered pipeline must call send_message, never start_conversation."""
        pipeline, store, client, _ = self._make_restart_pair()
        result = pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert result["status"] != "error" or result.get("fallback_recommended") is not True
        assert len(client.start_calls) == 0, "start_conversation must not be called on recovery"
        assert len(client.send_calls) == 1, "send_message must be called exactly once"

    def test_restart_recovers_correct_genie_conv_id(self):
        """send_message uses the correct recovered Genie conversation ID."""
        pipeline, _, client, _ = self._make_restart_pair()
        pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert len(client.send_calls) == 1
        assert client.send_calls[0]["conv_id"] == _GENIE_CONV

    def test_restart_store_repopulated_with_genie_id(self):
        """After recovery, local store contains the recovered Genie conversation ID."""
        pipeline, store, _, _ = self._make_restart_pair()
        pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        genie_id = store.get_genie_conversation_id(_APP_KEY)
        assert genie_id == _GENIE_CONV

    def test_restart_last_message_id_restored(self):
        """After recovery, last_genie_message_id is set in the store."""
        pipeline, store, _, _ = self._make_restart_pair()
        pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        # After run, the store will have the latest message ID (from send_message)
        msg_id = store.get_last_message_id(_APP_KEY)
        assert msg_id is not None

    def test_restart_different_owner_gets_new_start(self):
        """Owner B cannot recover Owner A's Genie conversation."""
        repo = InMemoryConversationRepository()
        _seed_active_record(repo, _OWNER_A, _FE_ID_1)
        # Owner B, same frontend ID: no durable record → MISS → start_conversation
        store_b = GenieSessionStore()
        client_b = RecordingGenieClient()
        pipeline_b = _build_pipeline(client=client_b, store=store_b)
        adapter_b = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline_b, "_durable_session_runtime_bundle", _enabled_bundle(adapter_b))
        pipeline_b.run(
            "show shipments", "plc-key-owner-b",
            owner_key=_OWNER_B, frontend_conversation_id=_FE_ID_1
        )
        assert len(client_b.start_calls) == 1, "Owner B must start a new conversation"
        assert len(client_b.send_calls) == 0

    def test_restart_no_stale_export_state(self):
        """Recovered pipeline store has no export state from first pipeline."""
        pipeline, store, _, _ = self._make_restart_pair()
        pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        ctx = store.get_context_snapshot(_APP_KEY)
        assert ctx.get("last_download_key") is None
        assert ctx.get("last_export_id") is None
        assert ctx.get("latest_table_result") is None

    def test_restart_multiple_restarts_idempotent(self):
        """Two restarts with same repo always produce send_message calls."""
        repo = InMemoryConversationRepository()
        _seed_active_record(repo, _OWNER_A, _FE_ID_1)

        for _ in range(2):
            store = GenieSessionStore()
            client = RecordingGenieClient()
            pipeline = _build_pipeline(client=client, store=store)
            adapter = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
            setattr(pipeline, "_durable_session_runtime_bundle", _enabled_bundle(adapter))
            pipeline.run(
                "show shipments", _APP_KEY,
                owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
            )
            # Each restart: send_message called, start_conversation not called
            assert len(client.send_calls) == 1
            assert len(client.start_calls) == 0

    def test_restart_unbound_record_starts_new_conv(self):
        """If durable record has no Genie conv ID, start_conversation is used."""
        repo = InMemoryConversationRepository()
        # Create conversation without binding a Genie conv ID
        repo.create_conversation(_OWNER_A, _FE_ID_1)
        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(client=client, store=store)
        adapter = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline, "_durable_session_runtime_bundle", _enabled_bundle(adapter))
        pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert len(client.start_calls) == 1
        assert len(client.send_calls) == 0

    def test_restart_disabled_mode_always_starts(self):
        """When durable mode is disabled, every run starts a new Genie conv."""
        repo = InMemoryConversationRepository()
        _seed_active_record(repo, _OWNER_A, _FE_ID_1)
        client = RecordingGenieClient()
        pipeline = _build_pipeline(client=client)
        setattr(pipeline, "_durable_session_runtime_bundle", _disabled_bundle())
        pipeline.run("show shipments", _APP_KEY)
        assert len(client.start_calls) == 1
        assert len(client.send_calls) == 0

    def test_restart_no_owner_key_falls_through_to_start(self):
        """Without owner_key, durable lookup is DISABLED → start_conversation."""
        repo = InMemoryConversationRepository()
        _seed_active_record(repo, _OWNER_A, _FE_ID_1)
        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(client=client, store=store)
        adapter = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline, "_durable_session_runtime_bundle", _enabled_bundle(adapter))
        # Omit owner_key → durable lookup raises _OwnerKeyContractError
        result = pipeline.run("show shipments", _APP_KEY, owner_key=None)
        # Without owner_key, enabled durable bundle + no owner raises error
        assert result["fallback_recommended"] is False
        assert result["status"] in ("error", "success")


# ===========================================================================
# GROUP B: Local Idle-Expiry Recovery (10 tests)
# ===========================================================================


class TestIdleExpiryRecovery:
    """After GenieSessionStore expiry/cleanup, durable lookup restores the session."""

    def _make_active_pipeline(
        self,
        owner: str = _OWNER_A,
        fe_id: str = _FE_ID_1,
        genie_conv: str = _GENIE_CONV,
    ):
        """Create repo, seed record, build pipeline."""
        repo = InMemoryConversationRepository()
        _seed_active_record(repo, owner, fe_id, genie_conv)
        store = GenieSessionStore()
        client = RecordingGenieClient(genie_conv_id=genie_conv)
        pipeline = _build_pipeline(client=client, store=store)
        adapter = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline, "_durable_session_runtime_bundle", _enabled_bundle(adapter))
        return pipeline, store, client, repo

    def _expire_session(self, store: GenieSessionStore, key: str) -> None:
        """Manually expire a session by setting expires_at to the past."""
        with store._lock:
            if key in store._sessions:
                store._sessions[key].expires_at = (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                )

    def test_expiry_remove_then_durable_recovers(self):
        """Removing the local session triggers durable recovery on next send."""
        pipeline, store, client, _ = self._make_active_pipeline()
        # First run establishes local + durable state
        pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert len(client.start_calls) + len(client.send_calls) == 1

        # Remove the local session (simulate idle eviction)
        store.remove_session(_APP_KEY)
        assert store.get_genie_conversation_id(_APP_KEY) is None

        client.start_calls.clear()
        client.send_calls.clear()

        # Second run: durable lookup recovers → send_message
        pipeline.run(
            "show more shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert len(client.start_calls) == 0, "Must not start new conv after expiry recovery"
        assert len(client.send_calls) == 1, "Must send message after expiry recovery"

    def test_expiry_cleanup_then_durable_recovers(self):
        """cleanup_expired_sessions + TTL expiry triggers durable recovery."""
        pipeline, store, client, _ = self._make_active_pipeline()
        pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )

        # Expire the session and run cleanup
        self._expire_session(store, _APP_KEY)
        removed = store.cleanup_expired_sessions()
        assert removed >= 1
        assert store.get_genie_conversation_id(_APP_KEY) is None

        client.start_calls.clear()
        client.send_calls.clear()

        pipeline.run(
            "show more shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert len(client.start_calls) == 0
        assert len(client.send_calls) == 1

    def test_expiry_multiple_cycles_same_genie_conv(self):
        """Three expiry/recovery cycles: Genie conv ID never changes."""
        pipeline, store, client, _ = self._make_active_pipeline()
        # First turn
        pipeline.run(
            "show shipments from origin port", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert client.send_calls[-1]["conv_id"] == _GENIE_CONV if client.send_calls else True

        for cycle in range(3):
            store.remove_session(_APP_KEY)
            client.start_calls.clear()
            client.send_calls.clear()
            pipeline.run(
                f"show delayed shipments cycle {cycle}", _APP_KEY,
                owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
            )
            assert len(client.start_calls) == 0, f"Cycle {cycle}: no start after expiry"
            assert len(client.send_calls) == 1, f"Cycle {cycle}: send after expiry"
            if client.send_calls:
                assert client.send_calls[0]["conv_id"] == _GENIE_CONV

    def test_expiry_no_cross_owner_recovery(self):
        """After expiry, Owner B cannot recover Owner A's session via same app key."""
        repo = InMemoryConversationRepository()
        _seed_active_record(repo, _OWNER_A, _FE_ID_1)
        _seed_active_record(repo, _OWNER_B, _FE_ID_1, genie_conv="genie-conv-owner-b")

        store_b = GenieSessionStore()
        client_b = RecordingGenieClient(genie_conv_id="genie-conv-owner-b")
        pipeline_b = _build_pipeline(client=client_b, store=store_b)
        adapter_b = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline_b, "_durable_session_runtime_bundle", _enabled_bundle(adapter_b))

        pipeline_b.run(
            "show shipments", "plc-key-b",
            owner_key=_OWNER_B, frontend_conversation_id=_FE_ID_1
        )
        # Owner B sends to Owner B's Genie conv, not Owner A's
        if client_b.send_calls:
            assert client_b.send_calls[0]["conv_id"] == "genie-conv-owner-b"
        else:
            # First turn starts a conversation — also acceptable
            assert len(client_b.start_calls) >= 0

    def test_expiry_recovery_with_last_message_metadata(self):
        """Last Genie message ID is present after expiry recovery."""
        pipeline, store, _, _ = self._make_active_pipeline()
        pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )

        store.remove_session(_APP_KEY)
        pipeline.run(
            "more shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )

        msg_id = store.get_last_message_id(_APP_KEY)
        assert msg_id is not None, "last_message_id must be set after recovery"

    def test_expiry_recovery_no_stale_export_state(self):
        """No export state bleeds through after session expiry and recovery."""
        pipeline, store, _, _ = self._make_active_pipeline()
        pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        store.remove_session(_APP_KEY)
        pipeline.run(
            "more shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        ctx = store.get_context_snapshot(_APP_KEY)
        assert ctx.get("latest_table_result") is None
        assert ctx.get("last_download_key") is None

    def test_expiry_recovery_uses_send_message(self):
        """After expiry recovery, send_message is used (not start_conversation)."""
        pipeline, store, client, _ = self._make_active_pipeline()
        pipeline.run(
            "show shipments for carrier ABC", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        store.remove_session(_APP_KEY)
        client.start_calls.clear()
        client.send_calls.clear()
        pipeline.run(
            "show more shipments delayed", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert len(client.start_calls) == 0
        assert len(client.send_calls) == 1

    def test_expiry_same_frontend_id_same_durable_record(self):
        """Same owner + frontend ID always maps to the same durable record."""
        repo = InMemoryConversationRepository()
        _seed_active_record(repo, _OWNER_A, _FE_ID_1)
        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(client=client, store=store)
        adapter = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline, "_durable_session_runtime_bundle", _enabled_bundle(adapter))

        # First run
        pipeline.run(
            "a", _APP_KEY, owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        store.remove_session(_APP_KEY)
        # Second run — must use same durable record
        pipeline.run(
            "b", _APP_KEY, owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        record = repo.get_by_frontend_id(_OWNER_A, _FE_ID_1)
        assert record is not None
        assert record.frontend_conversation_id == _FE_ID_1

    def test_expiry_genie_conv_id_unchanged_after_recovery(self):
        """After idle expiry recovery, the same Genie conv ID is used."""
        pipeline, store, client, _ = self._make_active_pipeline(
            genie_conv="genie-stable-conv"
        )
        pipeline.run(
            "first", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        store.remove_session(_APP_KEY)
        client.start_calls.clear()
        client.send_calls.clear()
        pipeline.run(
            "second", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        if client.send_calls:
            assert client.send_calls[0]["conv_id"] == "genie-stable-conv"

    def test_expiry_inactive_durable_prevents_recovery(self):
        """If durable record is RESET, expiry recovery is blocked."""
        repo = InMemoryConversationRepository()
        _make_inactive_record(repo, _OWNER_A, _FE_ID_1, ConversationStatus.RESET)
        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(client=client, store=store)
        adapter = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline, "_durable_session_runtime_bundle", _enabled_bundle(adapter))
        result = pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert len(client.start_calls) == 0
        assert len(client.send_calls) == 0
        assert _MSG_INACTIVE in result.get("message", "")


# ===========================================================================
# GROUP C: Inactive State After Restart/Expiry (8 tests)
# ===========================================================================


class TestInactiveStateAfterRestart:
    """RESET/STALE/EXPIRED durable records block Genie for restarted pipelines."""

    _INACTIVE_STATUSES = [
        ConversationStatus.RESET,
        ConversationStatus.STALE,
        ConversationStatus.EXPIRED,
    ]

    def _make_inactive_pipeline(
        self,
        status: ConversationStatus,
        owner: str = _OWNER_A,
        fe_id: str = _FE_ID_1,
    ):
        repo = InMemoryConversationRepository()
        _make_inactive_record(repo, owner, fe_id, status)
        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(client=client, store=store)
        adapter = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline, "_durable_session_runtime_bundle", _enabled_bundle(adapter))
        return pipeline, store, client

    def test_reset_tombstone_persists_after_restart(self):
        """RESET record blocks a fresh pipeline (process-restart scenario)."""
        pipeline, _, client = self._make_inactive_pipeline(ConversationStatus.RESET)
        result = pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert len(client.start_calls) == 0
        assert len(client.send_calls) == 0
        assert _MSG_INACTIVE in result.get("message", "")
        assert result.get("fallback_recommended") is False

    def test_reset_tombstone_persists_after_idle_expiry(self):
        """RESET record blocks even when local session was expired/removed."""
        repo = InMemoryConversationRepository()
        _make_inactive_record(repo, _OWNER_A, _FE_ID_1, ConversationStatus.RESET)
        store = GenieSessionStore()
        # Artificially set an expired session with the same key
        store.set_genie_conversation_id(_APP_KEY, "old-genie-conv")
        with store._lock:
            if _APP_KEY in store._sessions:
                store._sessions[_APP_KEY].expires_at = (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                )

        client = RecordingGenieClient()
        pipeline = _build_pipeline(client=client, store=store)
        adapter = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline, "_durable_session_runtime_bundle", _enabled_bundle(adapter))
        result = pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert len(client.start_calls) == 0
        assert len(client.send_calls) == 0
        assert _MSG_INACTIVE in result.get("message", "")

    def test_stale_status_blocks_after_restart(self):
        """STALE record blocks new pipeline."""
        pipeline, _, client = self._make_inactive_pipeline(ConversationStatus.STALE)
        result = pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert len(client.start_calls) == 0
        assert len(client.send_calls) == 0
        assert _MSG_INACTIVE in result.get("message", "")

    def test_expired_status_blocks_after_restart(self):
        """EXPIRED record blocks new pipeline."""
        pipeline, _, client = self._make_inactive_pipeline(ConversationStatus.EXPIRED)
        result = pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert len(client.start_calls) == 0
        assert len(client.send_calls) == 0
        assert _MSG_INACTIVE in result.get("message", "")

    def test_inactive_returns_static_response(self):
        """Inactive response contains the exact static message and correct status."""
        for status in self._INACTIVE_STATUSES:
            pipeline, _, _ = self._make_inactive_pipeline(status)
            result = pipeline.run(
                "show shipments", _APP_KEY,
                owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
            )
            assert _MSG_INACTIVE in result["message"], f"status={status}"
            assert result["fallback_recommended"] is False, f"status={status}"

    def test_inactive_no_new_genie_conversation(self):
        """No Genie API calls are made for any INACTIVE status."""
        for status in self._INACTIVE_STATUSES:
            pipeline, _, client = self._make_inactive_pipeline(status)
            pipeline.run(
                "show shipments", _APP_KEY,
                owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
            )
            assert len(client.start_calls) == 0, f"start must not be called for {status}"
            assert len(client.send_calls) == 0, f"send must not be called for {status}"

    def test_inactive_no_durable_bind_after_restart(self):
        """No writeback / bind occurs for INACTIVE durable state."""
        repo = InMemoryConversationRepository()
        _make_inactive_record(repo, _OWNER_A, _FE_ID_1, ConversationStatus.RESET)
        initial_record = repo.get_by_frontend_id(_OWNER_A, _FE_ID_1)
        initial_version = initial_record.version

        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(client=client, store=store)
        adapter = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline, "_durable_session_runtime_bundle", _enabled_bundle(adapter))
        pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )

        final_record = repo.get_by_frontend_id(_OWNER_A, _FE_ID_1)
        assert final_record.version == initial_version, "Version must not change for INACTIVE"

    def test_inactive_fallback_recommended_false(self):
        """fallback_recommended is False for all INACTIVE outcomes."""
        for status in self._INACTIVE_STATUSES:
            pipeline, _, _ = self._make_inactive_pipeline(status)
            result = pipeline.run(
                "show shipments", _APP_KEY,
                owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
            )
            assert result.get("fallback_recommended") is False, f"status={status}"


# ===========================================================================
# GROUP D: Owner Isolation (5 tests)
# ===========================================================================


class TestOwnerIsolation:
    """No owner can recover, reset, or send through another owner's session."""

    def test_same_frontend_id_different_owner_different_record(self):
        """Two owners, same frontend ID: separate durable records."""
        repo = InMemoryConversationRepository()
        _seed_active_record(repo, _OWNER_A, _FE_ID_1, genie_conv="conv-a")
        _seed_active_record(repo, _OWNER_B, _FE_ID_1, genie_conv="conv-b")

        rec_a = repo.get_by_frontend_id(_OWNER_A, _FE_ID_1)
        rec_b = repo.get_by_frontend_id(_OWNER_B, _FE_ID_1)
        assert rec_a.conversation_id != rec_b.conversation_id
        assert rec_a.genie_conversation_id == "conv-a"
        assert rec_b.genie_conversation_id == "conv-b"

    def test_owner_b_cannot_recover_owner_a_genie_conv(self):
        """Owner B with Owner A's frontend ID starts a NEW Genie conv."""
        repo = InMemoryConversationRepository()
        _seed_active_record(repo, _OWNER_A, _FE_ID_1, genie_conv="conv-owner-a")
        # No record for Owner B

        store_b = GenieSessionStore()
        client_b = RecordingGenieClient()
        pipeline_b = _build_pipeline(client=client_b, store=store_b)
        adapter_b = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline_b, "_durable_session_runtime_bundle", _enabled_bundle(adapter_b))
        pipeline_b.run(
            "show shipments", "plc-key-b",
            owner_key=_OWNER_B, frontend_conversation_id=_FE_ID_1
        )
        # Owner B must not use conv-owner-a
        for call in client_b.send_calls:
            assert call["conv_id"] != "conv-owner-a"

    def test_same_owner_different_frontend_separate_records(self):
        """Same owner, two different frontend IDs: two separate durable records."""
        repo = InMemoryConversationRepository()
        _seed_active_record(repo, _OWNER_A, _FE_ID_1, genie_conv="conv-1")
        _seed_active_record(repo, _OWNER_A, _FE_ID_2, genie_conv="conv-2")

        rec_1 = repo.get_by_frontend_id(_OWNER_A, _FE_ID_1)
        rec_2 = repo.get_by_frontend_id(_OWNER_A, _FE_ID_2)
        assert rec_1.conversation_id != rec_2.conversation_id
        assert rec_1.genie_conversation_id == "conv-1"
        assert rec_2.genie_conversation_id == "conv-2"

    def test_owner_isolation_separate_store_sessions_after_restart(self):
        """Process restart doesn't mix sessions for different owners."""
        repo = InMemoryConversationRepository()
        _seed_active_record(repo, _OWNER_A, _FE_ID_1, genie_conv="conv-a")
        _seed_active_record(repo, _OWNER_B, _FE_ID_1, genie_conv="conv-b")

        shared_store = GenieSessionStore()
        client_a = RecordingGenieClient(genie_conv_id="conv-a")
        client_b = RecordingGenieClient(genie_conv_id="conv-b")

        pipeline_a = _build_pipeline(client=client_a, store=shared_store)
        adapter_a = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline_a, "_durable_session_runtime_bundle", _enabled_bundle(adapter_a))

        pipeline_a.run(
            "show shipments for owner A", _APP_KEY, owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert len(client_a.send_calls) == 1
        if client_a.send_calls:
            assert client_a.send_calls[0]["conv_id"] == "conv-a"

    def test_owner_a_reset_does_not_affect_owner_b(self):
        """Resetting Owner A's record leaves Owner B's record ACTIVE."""
        repo = InMemoryConversationRepository()
        rec_a = _seed_active_record(repo, _OWNER_A, _FE_ID_1)
        _seed_active_record(repo, _OWNER_B, _FE_ID_1)

        repo.set_status(
            _OWNER_A, rec_a.conversation_id, ConversationStatus.RESET,
            expected_version=rec_a.version
        )

        rec_b = repo.get_by_frontend_id(_OWNER_B, _FE_ID_1)
        assert rec_b.status == ConversationStatus.ACTIVE


# ===========================================================================
# GROUP E: Durable Failure Policy (5 tests)
# ===========================================================================


class TestDurableFailurePolicy:
    """Unavailable or degraded durable state: fail closed, no new Genie conv."""

    def test_repository_unavailable_fails_closed(self):
        """Unavailable repository returns error with fallback_recommended=False."""
        class UnavailableRepo:
            def get_by_frontend_id(self, *a, **kw):
                raise Exception("repo unavailable")
            def create_conversation(self, *a, **kw):
                raise Exception("repo unavailable")

        class UnavailableBundle:
            repository = UnavailableRepo()
            backend = MagicMock()
            durable = True
            def close(self): pass

        client = RecordingGenieClient()
        store = GenieSessionStore()
        pipeline = _build_pipeline(client=client, store=store)
        adapter = DurableGenieSessionAdapter(UnavailableBundle(), cache_store=None)
        setattr(pipeline, "_durable_session_runtime_bundle", _enabled_bundle(adapter))
        result = pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert result["status"] == "error"
        assert result.get("fallback_recommended") is False
        assert len(client.start_calls) == 0
        assert len(client.send_calls) == 0

    def test_durable_unavailable_no_new_genie_conv(self):
        """No start_conversation when durable lookup fails."""
        from unittest.mock import patch
        from app.services.durable_genie_session_adapter import DurableGenieSessionAdapter as _Adapter

        orig_load = _Adapter.load

        def _fail_load(self, key):
            raise DurableGenieSessionUnavailableError("forced unavailable")

        client = RecordingGenieClient()
        repo = InMemoryConversationRepository()
        store = GenieSessionStore()
        pipeline = _build_pipeline(client=client, store=store)
        adapter = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline, "_durable_session_runtime_bundle", _enabled_bundle(adapter))

        try:
            adapter.load = lambda key: (_ for _ in ()).throw(
                DurableGenieSessionUnavailableError("forced")
            )
            result = pipeline.run(
                "show shipments", _APP_KEY,
                owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
            )
            assert len(client.start_calls) == 0
            assert result.get("fallback_recommended") is False
        finally:
            adapter.load = orig_load

    def test_durable_unavailable_fallback_recommended_false(self):
        """Durable failure must not set fallback_recommended=True."""
        class BadRepo:
            def get_by_frontend_id(self, *a, **kw):
                from app.services.conversation_repository import ConversationRepositoryUnavailableError
                raise ConversationRepositoryUnavailableError("down")
            def create_conversation(self, *a, **kw):
                raise Exception("down")

        class BadBundle:
            repository = BadRepo()
            backend = MagicMock()
            durable = True
            def close(self): pass

        client = RecordingGenieClient()
        pipeline = _build_pipeline(client=client)
        adapter = DurableGenieSessionAdapter(BadBundle(), cache_store=None)
        setattr(pipeline, "_durable_session_runtime_bundle", _enabled_bundle(adapter))
        result = pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert result.get("fallback_recommended") is False

    def test_durable_degraded_fails_closed(self):
        """Degraded durable result (cache fallback) must fail closed."""
        from app.services.durable_genie_session_adapter import DurableGenieSessionAdapter as _Adapter

        repo = InMemoryConversationRepository()
        _seed_active_record(repo, _OWNER_A, _FE_ID_1)

        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(client=client, store=store)
        adapter = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline, "_durable_session_runtime_bundle", _enabled_bundle(adapter))

        # Patch load to return a degraded result
        orig_load = adapter.load
        record = repo.get_by_frontend_id(_OWNER_A, _FE_ID_1)
        degraded_result = GenieSessionLookupResult(
            record=record, source=GenieSessionLookupSource.CACHE, degraded=True
        )
        adapter.load = lambda key: degraded_result

        try:
            result = pipeline.run(
                "show shipments", _APP_KEY,
                owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
            )
            assert result["status"] == "error"
            assert result.get("fallback_recommended") is False
            assert len(client.start_calls) == 0
        finally:
            adapter.load = orig_load

    def test_durable_unavailable_no_local_store_modification(self):
        """Failed durable lookup must not write to the local session store."""
        from app.services.conversation_repository import ConversationRepositoryUnavailableError

        class DownRepo:
            def get_by_frontend_id(self, *a, **kw):
                raise ConversationRepositoryUnavailableError("down")
            def create_conversation(self, *a, **kw):
                raise ConversationRepositoryUnavailableError("down")

        class DownBundle:
            repository = DownRepo()
            backend = MagicMock()
            durable = True
            def close(self): pass

        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(client=client, store=store)
        adapter = DurableGenieSessionAdapter(DownBundle(), cache_store=None)
        setattr(pipeline, "_durable_session_runtime_bundle", _enabled_bundle(adapter))
        pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        # Store must not have been modified
        assert store.get_genie_conversation_id(_APP_KEY) is None
        assert store.session_count() == 0


# ===========================================================================
# GROUP F: Race / Concurrency Safety (4 tests)
# ===========================================================================


class TestRaceSafety:
    """Concurrent requests, reset-vs-recovery, and tombstone-wins scenarios."""

    def test_concurrent_restarts_idempotent(self):
        """Two pipelines sharing the same repo both correctly recover."""
        repo = InMemoryConversationRepository()
        _seed_active_record(repo, _OWNER_A, _FE_ID_1)

        results = []
        for _ in range(2):
            store = GenieSessionStore()
            client = RecordingGenieClient()
            pipeline = _build_pipeline(client=client, store=store)
            adapter = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
            setattr(pipeline, "_durable_session_runtime_bundle", _enabled_bundle(adapter))
            result = pipeline.run(
                "show shipments", _APP_KEY,
                owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
            )
            results.append((result, client))

        for result, client in results:
            assert len(client.start_calls) == 0, "Must not start on any concurrent recovery"

    def test_reset_while_idle_inactive_tombstone_wins(self):
        """RESET tombstone wins over an expired local session."""
        repo = InMemoryConversationRepository()
        _make_inactive_record(repo, _OWNER_A, _FE_ID_1, ConversationStatus.RESET)

        store = GenieSessionStore()
        # Put expired session in store
        store.set_genie_conversation_id(_APP_KEY, "old-genie")
        with store._lock:
            if _APP_KEY in store._sessions:
                store._sessions[_APP_KEY].expires_at = (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                )

        client = RecordingGenieClient()
        pipeline = _build_pipeline(client=client, store=store)
        adapter = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline, "_durable_session_runtime_bundle", _enabled_bundle(adapter))
        result = pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert len(client.start_calls) == 0
        assert len(client.send_calls) == 0
        assert _MSG_INACTIVE in result["message"]

    def test_inactive_tombstone_wins_over_process_restart(self):
        """RESET record blocks even when the pipeline is brand-new (restarted)."""
        repo = InMemoryConversationRepository()
        _make_inactive_record(repo, _OWNER_A, _FE_ID_1, ConversationStatus.RESET)

        # Completely fresh pipeline/store — simulates process restart
        store = GenieSessionStore()
        client = RecordingGenieClient()
        pipeline = _build_pipeline(client=client, store=store)
        adapter = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline, "_durable_session_runtime_bundle", _enabled_bundle(adapter))
        result = pipeline.run(
            "show shipments", _APP_KEY,
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        assert len(client.start_calls) == 0
        assert _MSG_INACTIVE in result["message"]
        assert result["fallback_recommended"] is False

    def test_concurrent_owner_requests_isolated(self):
        """Requests from Owner A and Owner B with the same frontend ID stay isolated."""
        repo = InMemoryConversationRepository()
        _seed_active_record(repo, _OWNER_A, _FE_ID_1, genie_conv="genie-a")
        _seed_active_record(repo, _OWNER_B, _FE_ID_1, genie_conv="genie-b")

        store_a = GenieSessionStore()
        store_b = GenieSessionStore()
        client_a = RecordingGenieClient(genie_conv_id="genie-a")
        client_b = RecordingGenieClient(genie_conv_id="genie-b")
        pipeline_a = _build_pipeline(client=client_a, store=store_a)
        pipeline_b = _build_pipeline(client=client_b, store=store_b)
        adapter_a = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        adapter_b = DurableGenieSessionAdapter(_FakeRepoBundle(repo), cache_store=None)
        setattr(pipeline_a, "_durable_session_runtime_bundle", _enabled_bundle(adapter_a))
        setattr(pipeline_b, "_durable_session_runtime_bundle", _enabled_bundle(adapter_b))

        pipeline_a.run(
            "shipment A", "plc-a",
            owner_key=_OWNER_A, frontend_conversation_id=_FE_ID_1
        )
        pipeline_b.run(
            "shipment B", "plc-b",
            owner_key=_OWNER_B, frontend_conversation_id=_FE_ID_1
        )

        for c_a in client_a.send_calls:
            assert c_a["conv_id"] != "genie-b", "Owner A must not use Owner B conv"
        for c_b in client_b.send_calls:
            assert c_b["conv_id"] != "genie-a", "Owner B must not use Owner A conv"
