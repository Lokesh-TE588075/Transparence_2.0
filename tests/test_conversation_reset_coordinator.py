"""Tests for Phase 4C4B1 conversation reset coordinator.

Uses:
- Real InMemoryConversationRepository
- Real DurableGenieSessionAdapter
- Real GenieSessionStore

Narrow fakes used only for fault injection (unavailable/degraded scenarios).

No live Lakebase. No HTTP. No credentials.
"""

from __future__ import annotations

import os as _os
import sys as _sys
import threading
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock, patch, PropertyMock

_REPO_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)
del _os, _REPO_ROOT

from app.services.conversation_repository import (
    ConversationRecord,
    ConversationStatus,
    ConversationVersionConflictError,
    InMemoryConversationRepository,
)
from app.services.conversation_repository_factory import (
    ConversationRepositoryBackend,
    ConversationRepositoryBundle,
)
from app.services.durable_genie_session_adapter import (
    DurableGenieSessionAdapter,
    DurableGenieSessionKey,
    DurableGenieSessionUnavailableError,
    DurableGenieSessionVersionConflictError,
    GenieSessionLookupResult,
    GenieSessionLookupSource,
)
from app.services.genie_session_store import GenieSessionStore
from app.services.conversation_reset_coordinator import (
    ConversationResetCoordinator,
    ResetCoordinatorConflictError,
    ResetCoordinatorError,
    ResetCoordinatorInternalError,
    ResetCoordinatorInvalidInputError,
    ResetCoordinatorUnavailableError,
    ResetOutcome,
    ResetResult,
)
from app.services.process_local_conversation_key import (
    build_process_local_conversation_key,
    is_valid_process_local_key,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_OWNER = "a" * 64  # 64 lowercase hex chars
_VALID_OWNER_B = "b" * 64
_VALID_FRONTEND_ID = "frontend-conv-001"
_VALID_FRONTEND_ID_B = "frontend-conv-002"
_SESSION_ID = "test-session-001"

# Opaque process-local keys derived from the above constants.
# Used as the process_local_conversation_key argument to coord.reset().
_VALID_LOCAL_KEY = build_process_local_conversation_key(
    owner_user_id_hash=_VALID_OWNER,
    session_id=_SESSION_ID,
    frontend_conversation_id=_VALID_FRONTEND_ID,
)
_VALID_LOCAL_KEY_B = build_process_local_conversation_key(
    owner_user_id_hash=_VALID_OWNER_B,
    session_id=_SESSION_ID,
    frontend_conversation_id=_VALID_FRONTEND_ID,
)


def _make_bundle() -> ConversationRepositoryBundle:
    repo = InMemoryConversationRepository()
    return ConversationRepositoryBundle(
        repository=repo,
        backend=ConversationRepositoryBackend.MEMORY,
        durable=False,
    )


def _make_adapter(bundle: Optional[ConversationRepositoryBundle] = None) -> DurableGenieSessionAdapter:
    if bundle is None:
        bundle = _make_bundle()
    return DurableGenieSessionAdapter(
        repository_bundle=bundle,
        cache_store=None,
        cache_enabled=False,
    )


def _make_coordinator(
    adapter: Optional[DurableGenieSessionAdapter] = None,
    store: Optional[GenieSessionStore] = None,
) -> ConversationResetCoordinator:
    if adapter is None:
        adapter = _make_adapter()
    if store is None:
        store = GenieSessionStore()
    return ConversationResetCoordinator(adapter=adapter, session_store=store)


def _setup_active_record(adapter, owner=_VALID_OWNER, frontend_id=_VALID_FRONTEND_ID):
    """Create an ACTIVE record and return it."""
    key = DurableGenieSessionKey(
        owner_user_id_hash=owner,
        frontend_conversation_id=frontend_id,
    )
    result = adapter.get_or_create(key)
    return key, result.record


def _setup_record_with_status(adapter, status, owner=_VALID_OWNER, frontend_id=_VALID_FRONTEND_ID):
    """Create a record and set it to the given status."""
    key, record = _setup_active_record(adapter, owner, frontend_id)
    if status != ConversationStatus.ACTIVE:
        adapter.set_status(key, status, expected_version=record.version)
    result = adapter.load(key)
    return key, result.record


# =============================================================================
# INPUT AND ISOLATION TESTS (1-5)
# =============================================================================


class TestInputValidation:
    """Tests 1-5: Input validation and isolation."""

    def test_01_valid_owner_and_key_accepted(self):
        coord = _make_coordinator()
        adapter = coord._adapter
        # Pre-create a record so the flow completes
        key = DurableGenieSessionKey(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
        )
        adapter.get_or_create(key)
        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        assert result.success is True

    def test_02_invalid_owner_hash_rejected_too_short(self):
        coord = _make_coordinator()
        try:
            coord.reset(
                owner_user_id_hash="abc123",
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
            assert False, "Should have raised"
        except ResetCoordinatorInvalidInputError:
            pass

    def test_02b_invalid_owner_hash_rejected_uppercase(self):
        coord = _make_coordinator()
        try:
            coord.reset(
                owner_user_id_hash="A" * 64,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
            assert False, "Should have raised"
        except ResetCoordinatorInvalidInputError:
            pass

    def test_02c_invalid_owner_hash_rejected_empty(self):
        coord = _make_coordinator()
        try:
            coord.reset(
                owner_user_id_hash="",
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
            assert False, "Should have raised"
        except ResetCoordinatorInvalidInputError:
            pass

    def test_03_invalid_frontend_id_rejected_empty(self):
        coord = _make_coordinator()
        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id="",
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
            assert False, "Should have raised"
        except ResetCoordinatorInvalidInputError:
            pass

    def test_03b_invalid_frontend_id_rejected_email(self):
        coord = _make_coordinator()
        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id="user@example.com",
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
            assert False, "Should have raised"
        except ResetCoordinatorInvalidInputError:
            pass

    def test_04_same_frontend_different_owners_isolated(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        # Create records for two owners with same frontend ID
        key_a = DurableGenieSessionKey(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
        )
        key_b = DurableGenieSessionKey(
            owner_user_id_hash=_VALID_OWNER_B,
            frontend_conversation_id=_VALID_FRONTEND_ID,
        )
        adapter.get_or_create(key_a)
        adapter.get_or_create(key_b)

        # Reset owner A
        result_a = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        assert result_a.success is True

        # Owner B still ACTIVE
        lookup_b = adapter.load(key_b)
        assert lookup_b is not None
        assert lookup_b.record.status == ConversationStatus.ACTIVE

    def test_05_no_identifier_in_result_repr(self):
        coord = _make_coordinator()
        adapter = coord._adapter
        key = DurableGenieSessionKey(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
        )
        adapter.get_or_create(key)
        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        r = repr(result)
        assert _VALID_OWNER not in r
        assert _VALID_FRONTEND_ID not in r

    def test_05b_no_identifier_in_error_repr(self):
        coord = _make_coordinator()
        try:
            coord.reset(
                owner_user_id_hash="bad",
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
        except ResetCoordinatorInvalidInputError as e:
            r = repr(e)
            assert "bad" not in r
            assert _VALID_FRONTEND_ID not in r


# =============================================================================
# EXISTING ACTIVE TESTS (6-10)
# =============================================================================


class TestExistingActive:
    """Tests 6-10: ACTIVE record transitions to RESET."""

    def test_06_active_transitions_to_reset(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, record = _setup_active_record(adapter)
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "genie-123")

        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        assert result.success is True
        assert result.outcome == ResetOutcome.RESET

        # Confirm durable state
        reloaded = adapter.load(key)
        assert reloaded.record.status == ConversationStatus.RESET

    def test_07_exact_expected_version_used(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, record = _setup_active_record(adapter)
        initial_version = record.version

        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        assert result.success is True

        # Reloaded version should be initial + 1
        reloaded = adapter.load(key)
        assert reloaded.record.version == initial_version + 1

    def test_08_returned_reset_is_confirmed(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, _ = _setup_active_record(adapter)
        coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )

        reloaded = adapter.load(key)
        assert reloaded is not None
        assert reloaded.record.status == ConversationStatus.RESET
        assert reloaded.degraded is False

    def test_09_version_increments_once(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, record = _setup_active_record(adapter)
        v_before = record.version

        coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )

        reloaded = adapter.load(key)
        assert reloaded.record.version == v_before + 1

    def test_10_local_session_removed_after_durable_success(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, _ = _setup_active_record(adapter)
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "genie-abc")
        store.update_context(_VALID_LOCAL_KEY, last_intent="AGGREGATION")

        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        assert result.success is True

        # Local session completely gone
        assert store.get_session(_VALID_LOCAL_KEY) is None
        assert store.get_genie_conversation_id(_VALID_LOCAL_KEY) is None


# =============================================================================
# EXISTING INACTIVE TESTS (11-14)
# =============================================================================


class TestExistingInactive:
    """Tests 11-14: Existing RESET/STALE/EXPIRED returns idempotent success."""

    def test_11_existing_reset_returns_idempotent_success(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, _ = _setup_record_with_status(adapter, ConversationStatus.RESET)
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "genie-old")

        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        assert result.success is True
        assert result.outcome == ResetOutcome.ALREADY_INACTIVE

    def test_11b_existing_reset_no_set_status_called(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, record = _setup_record_with_status(adapter, ConversationStatus.RESET)
        version_before = record.version

        coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )

        # Version unchanged — no set_status was called
        reloaded = adapter.load(key)
        assert reloaded.record.version == version_before

    def test_12_existing_stale_returns_idempotent_success(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, _ = _setup_record_with_status(adapter, ConversationStatus.STALE)

        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        assert result.success is True
        assert result.outcome == ResetOutcome.ALREADY_INACTIVE

    def test_13_existing_expired_returns_idempotent_success(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, _ = _setup_record_with_status(adapter, ConversationStatus.EXPIRED)

        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        assert result.success is True
        assert result.outcome == ResetOutcome.ALREADY_INACTIVE

    def test_14_local_session_removed_for_inactive(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, _ = _setup_record_with_status(adapter, ConversationStatus.STALE)
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "genie-stale")

        coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )

        assert store.get_session(_VALID_LOCAL_KEY) is None


# =============================================================================
# MISSING/TOMBSTONE TESTS (15-22)
# =============================================================================


class TestMissingTombstone:
    """Tests 15-22: Missing record tombstone flow."""

    def test_15_missing_record_invokes_get_or_create(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        # No pre-existing record
        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        assert result.success is True
        assert result.outcome == ResetOutcome.TOMBSTONE_CREATED

    def test_16_missing_creates_active_then_transitions_to_reset(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        assert result.success is True
        assert result.outcome == ResetOutcome.TOMBSTONE_CREATED

        # Verify final status is RESET
        key = DurableGenieSessionKey(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
        )
        reloaded = adapter.load(key)
        assert reloaded.record.status == ConversationStatus.RESET

    def test_17_tombstone_occupies_logical_key(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )

        key = DurableGenieSessionKey(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
        )
        reloaded = adapter.load(key)
        assert reloaded is not None
        assert reloaded.record.status == ConversationStatus.RESET

    def test_18_repeated_reset_remains_idempotent(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        r1 = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        r2 = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        assert r1.success is True
        assert r2.success is True
        assert r2.outcome == ResetOutcome.ALREADY_INACTIVE

    def test_19_concurrent_precreated_reset_accepted(self):
        """get_or_create returns an already-RESET record (another request beat us)."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        # Simulate: another request created and reset the record
        key = DurableGenieSessionKey(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
        )
        adapter.get_or_create(key)
        adapter.set_status(key, ConversationStatus.RESET, expected_version=1)

        # Our reset should see existing RESET and succeed idempotently
        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        assert result.success is True
        assert result.outcome == ResetOutcome.ALREADY_INACTIVE

    def test_20_degraded_get_or_create_rejected(self):
        """When get_or_create returns degraded, fail closed."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        # Patch adapter.get_or_create to return degraded result
        key = DurableGenieSessionKey(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
        )
        fake_record = ConversationRecord(
            conversation_id="fake-id",
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            genie_conversation_id=None,
            last_genie_message_id=None,
            status=ConversationStatus.ACTIVE,
            version=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            last_active_at=datetime.now(timezone.utc),
        )
        degraded_result = GenieSessionLookupResult(
            record=fake_record,
            source=GenieSessionLookupSource.CACHE,
            degraded=True,
        )

        original_get_or_create = adapter.get_or_create
        call_count = [0]

        def patched_get_or_create(k, **kwargs):
            call_count[0] += 1
            return degraded_result

        adapter.get_or_create = patched_get_or_create

        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
            assert False, "Should have raised"
        except ResetCoordinatorUnavailableError:
            pass

    def test_21_get_or_create_unavailable_rejected(self):
        """When get_or_create raises unavailable, fail closed."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        def patched_get_or_create(k, **kwargs):
            raise DurableGenieSessionUnavailableError("unavailable")

        adapter.get_or_create = patched_get_or_create

        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
            assert False, "Should have raised"
        except ResetCoordinatorUnavailableError:
            pass

    def test_22_no_success_before_reset_confirmed(self):
        """Tombstone flow must confirm RESET status before returning success."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        assert result.success is True

        # Confirm the durable record is actually RESET
        key = DurableGenieSessionKey(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
        )
        reloaded = adapter.load(key)
        assert reloaded.record.status == ConversationStatus.RESET


# =============================================================================
# CONFLICT TESTS (23-30)
# =============================================================================


class TestConflictHandling:
    """Tests 23-30: Version conflict policy.

    Correct conflict-path injection pattern:
    1. Initial adapter.load(key) returns authoritative ACTIVE record.
    2. Patched set_status mutates the real repo to a competing status,
       then raises DurableGenieSessionVersionConflictError.
    3. Coordinator calls adapter.load(key) exactly once after the conflict.
    4. Reload returns the competing durable state.
    """

    def test_23_conflict_causes_exactly_one_reload_and_one_set_status(self):
        """set_status called once; exactly one post-conflict reload; total loads = 2."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, record = _setup_active_record(adapter)
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "g-1")

        original_load = adapter.load
        load_count = [0]
        set_status_count = [0]

        def conflict_with_mutation(k, status, **kwargs):
            set_status_count[0] += 1
            # Simulate concurrent reset: mutate underlying repo to RESET
            bundle.repository.set_status(
                _VALID_OWNER, record.conversation_id,
                ConversationStatus.RESET, expected_version=record.version,
            )
            raise DurableGenieSessionVersionConflictError("conflict")

        def counting_load(k):
            load_count[0] += 1
            return original_load(k)

        adapter.set_status = conflict_with_mutation
        adapter.load = counting_load

        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )

        assert result.success is True
        assert result.outcome == ResetOutcome.ALREADY_INACTIVE
        assert load_count[0] == 2  # initial load + one post-conflict reload
        assert set_status_count[0] == 1  # no CAS retry

    def test_24_reload_reset_succeeds_idempotently(self):
        """Conflict → reload shows RESET → idempotent success, session removed."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, record = _setup_active_record(adapter)
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "g-keep")

        original_load = adapter.load

        def conflict_with_reset(k, status, **kwargs):
            # Concurrent reset beats us
            bundle.repository.set_status(
                _VALID_OWNER, record.conversation_id,
                ConversationStatus.RESET, expected_version=record.version,
            )
            raise DurableGenieSessionVersionConflictError("conflict")

        adapter.set_status = conflict_with_reset

        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )

        assert result.success is True
        assert result.outcome == ResetOutcome.ALREADY_INACTIVE
        # Local session removed
        assert store.get_session(_VALID_LOCAL_KEY) is None

    def test_25_reload_stale_succeeds_idempotently(self):
        """Conflict → reload shows STALE → idempotent success, session removed."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, record = _setup_active_record(adapter)
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "g-stale")

        def conflict_with_stale(k, status, **kwargs):
            # Concurrent staleness-marker beats us
            bundle.repository.set_status(
                _VALID_OWNER, record.conversation_id,
                ConversationStatus.STALE, expected_version=record.version,
            )
            raise DurableGenieSessionVersionConflictError("conflict")

        adapter.set_status = conflict_with_stale

        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )

        assert result.success is True
        assert result.outcome == ResetOutcome.ALREADY_INACTIVE
        assert store.get_session(_VALID_LOCAL_KEY) is None

    def test_26_reload_expired_succeeds_idempotently(self):
        """Conflict → reload shows EXPIRED → idempotent success, session removed."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, record = _setup_active_record(adapter)
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "g-exp")

        def conflict_with_expired(k, status, **kwargs):
            # Concurrent expiry beats us
            bundle.repository.set_status(
                _VALID_OWNER, record.conversation_id,
                ConversationStatus.EXPIRED, expected_version=record.version,
            )
            raise DurableGenieSessionVersionConflictError("conflict")

        adapter.set_status = conflict_with_expired

        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )

        assert result.success is True
        assert result.outcome == ResetOutcome.ALREADY_INACTIVE
        assert store.get_session(_VALID_LOCAL_KEY) is None

    def test_27_reload_active_returns_conflict(self):
        """Conflict → reload shows ACTIVE → conflict error, session retained."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, record = _setup_active_record(adapter)
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "g-keep")

        # Patch set_status to conflict without changing state (remains ACTIVE)
        def conflict_no_mutation(k, status, **kwargs):
            raise DurableGenieSessionVersionConflictError("conflict")

        adapter.set_status = conflict_no_mutation

        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
            assert False, "Should have raised"
        except ResetCoordinatorConflictError:
            pass

        # Session retained
        assert store.get_genie_conversation_id(_VALID_LOCAL_KEY) == "g-keep"

    def test_28_reload_none_returns_conflict(self):
        """Conflict → reload returns None → conflict error, session retained."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, record = _setup_active_record(adapter)
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "g-keep")

        original_load = adapter.load
        load_count = [0]

        def conflict_no_mutation(k, status, **kwargs):
            raise DurableGenieSessionVersionConflictError("conflict")

        def load_none_on_reload(k):
            load_count[0] += 1
            if load_count[0] == 1:
                return original_load(k)
            return None

        adapter.set_status = conflict_no_mutation
        adapter.load = load_none_on_reload

        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
            assert False, "Should have raised"
        except ResetCoordinatorConflictError:
            pass

        assert load_count[0] == 2  # initial + one reload
        assert store.get_genie_conversation_id(_VALID_LOCAL_KEY) == "g-keep"

    def test_29_reload_unavailable_returns_unavailable(self):
        """Conflict → reload unavailable → unavailable error, session retained."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, record = _setup_active_record(adapter)
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "g-keep")

        original_load = adapter.load
        load_count = [0]

        def conflict_no_mutation(k, status, **kwargs):
            raise DurableGenieSessionVersionConflictError("conflict")

        def load_unavailable_on_reload(k):
            load_count[0] += 1
            if load_count[0] == 1:
                return original_load(k)
            raise DurableGenieSessionUnavailableError("unavailable")

        adapter.set_status = conflict_no_mutation
        adapter.load = load_unavailable_on_reload

        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
            assert False, "Should have raised"
        except ResetCoordinatorUnavailableError:
            pass

        assert load_count[0] == 2
        assert store.get_genie_conversation_id(_VALID_LOCAL_KEY) == "g-keep"

    def test_30_no_cas_retry_loop(self):
        """set_status called exactly once; no second CAS attempt."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, record = _setup_active_record(adapter)

        set_status_count = [0]

        def counting_conflict(k, status, **kwargs):
            set_status_count[0] += 1
            raise DurableGenieSessionVersionConflictError("conflict")

        adapter.set_status = counting_conflict

        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
        except (ResetCoordinatorConflictError, ResetCoordinatorUnavailableError):
            pass

        assert set_status_count[0] == 1



# =============================================================================
# MUTATION PROHIBITION TESTS (31-35)
# =============================================================================


class TestMutationProhibitions:
    """Tests 31-35: No delete, bind, update_message, touch, or direct repo."""

    def test_31_no_delete(self):
        """Coordinator never calls adapter.delete."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, _ = _setup_active_record(adapter)
        delete_called = [False]
        original_delete = adapter.delete

        def track_delete(k):
            delete_called[0] = True
            return original_delete(k)

        adapter.delete = track_delete

        coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        assert delete_called[0] is False

    def test_32_no_bind_genie_conversation(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, _ = _setup_active_record(adapter)
        bind_called = [False]
        original_bind = adapter.bind_genie_conversation

        def track_bind(k, genie_id, **kwargs):
            bind_called[0] = True
            return original_bind(k, genie_id, **kwargs)

        adapter.bind_genie_conversation = track_bind

        coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        assert bind_called[0] is False

    def test_33_no_update_last_genie_message(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, _ = _setup_active_record(adapter)
        update_called = [False]
        original_update = adapter.update_last_genie_message

        def track_update(k, msg_id, **kwargs):
            update_called[0] = True
            return original_update(k, msg_id, **kwargs)

        adapter.update_last_genie_message = track_update

        coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        assert update_called[0] is False

    def test_34_no_touch(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, _ = _setup_active_record(adapter)
        touch_called = [False]
        original_touch = adapter.touch

        def track_touch(k, **kwargs):
            touch_called[0] = True
            return original_touch(k, **kwargs)

        adapter.touch = track_touch

        coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        assert touch_called[0] is False

    def test_35_no_direct_repository_access(self):
        """Coordinator uses adapter methods, not direct repo methods."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        # Verify coordinator does not hold a reference to the repository
        assert not hasattr(coord, '_repository')
        assert not hasattr(coord, 'repository')


# =============================================================================
# SESSION STORE INTEGRATION TESTS (36-40)
# =============================================================================


class TestSessionStoreIntegration:
    """Tests 36-40: Session store removal behavior in coordinator context."""

    def test_36_remove_session_physically_removes(self):
        store = GenieSessionStore()
        store.set_genie_conversation_id(_VALID_FRONTEND_ID, "g-1")
        result = store.remove_session(_VALID_FRONTEND_ID)
        assert result is True
        with store._lock:
            assert _VALID_FRONTEND_ID not in store._sessions

    def test_37_missing_remove_is_idempotent(self):
        store = GenieSessionStore()
        result = store.remove_session("non-existent")
        assert result is False

    def test_38_other_sessions_remain_unchanged(self):
        store = GenieSessionStore()
        store.set_genie_conversation_id(_VALID_FRONTEND_ID, "g-1")
        store.set_genie_conversation_id(_VALID_FRONTEND_ID_B, "g-2")

        store.remove_session(_VALID_FRONTEND_ID)

        assert store.get_genie_conversation_id(_VALID_FRONTEND_ID_B) == "g-2"

    def test_39_concurrent_removal_safe(self):
        store = GenieSessionStore()
        store.set_genie_conversation_id(_VALID_FRONTEND_ID, "g-1")

        results = []
        barrier = threading.Barrier(4)

        def _remove():
            barrier.wait()
            results.append(store.remove_session(_VALID_FRONTEND_ID))

        threads = [threading.Thread(target=_remove) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 1
        assert results.count(False) == 3

    def test_40_context_heavy_session_completely_removed(self):
        store = GenieSessionStore()
        store.set_genie_conversation_id(_VALID_FRONTEND_ID, "g-ctx")
        store.set_last_message_id(_VALID_FRONTEND_ID, "msg-ctx")
        store.update_context(
            _VALID_FRONTEND_ID,
            last_intent="BROAD_LISTING",
            last_entities=["US", "CN"],
            last_filters={"bu": "ION"},
            last_user_prompt="show shipments",
        )

        store.remove_session(_VALID_FRONTEND_ID)
        assert store.get_session(_VALID_FRONTEND_ID) is None
        assert store.get_context_snapshot(_VALID_FRONTEND_ID) == {}


# =============================================================================
# ADDITIONAL COVERAGE
# =============================================================================


class TestAdditionalCoverage:
    """Additional tests for edge cases."""

    def test_degraded_load_rejected(self):
        """When initial load returns degraded, fail closed."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, record = _setup_active_record(adapter)

        fake_record = ConversationRecord(
            conversation_id="fake",
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            genie_conversation_id=None,
            last_genie_message_id=None,
            status=ConversationStatus.ACTIVE,
            version=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            last_active_at=datetime.now(timezone.utc),
        )
        degraded_result = GenieSessionLookupResult(
            record=fake_record,
            source=GenieSessionLookupSource.CACHE,
            degraded=True,
        )

        adapter.load = lambda k: degraded_result

        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
            assert False, "Should have raised"
        except ResetCoordinatorUnavailableError:
            pass

    def test_load_unavailable_raises(self):
        """When adapter.load raises unavailable, coordinator raises unavailable."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        adapter.load = lambda k: (_ for _ in ()).throw(
            DurableGenieSessionUnavailableError("down")
        )

        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
            assert False, "Should have raised"
        except ResetCoordinatorUnavailableError:
            pass

    def test_session_retained_on_durable_failure(self):
        """When durable reset fails, local session is NOT removed."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, record = _setup_active_record(adapter)
        store.set_genie_conversation_id(_VALID_FRONTEND_ID, "genie-keep")

        # Make set_status raise unavailable
        def fail_set_status(k, status, **kwargs):
            raise DurableGenieSessionUnavailableError("down")

        adapter.set_status = fail_set_status

        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
        except ResetCoordinatorUnavailableError:
            pass

        # Session retained
        assert store.get_genie_conversation_id(_VALID_FRONTEND_ID) == "genie-keep"

    def test_session_retained_on_conflict(self):
        """When conflict returns ACTIVE, session is NOT removed."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, record = _setup_active_record(adapter)
        store.set_genie_conversation_id(_VALID_FRONTEND_ID, "genie-keep")

        def always_conflict(k, status, **kwargs):
            raise DurableGenieSessionVersionConflictError("conflict")

        adapter.set_status = always_conflict

        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
        except ResetCoordinatorConflictError:
            pass

        # Session retained
        assert store.get_genie_conversation_id(_VALID_FRONTEND_ID) == "genie-keep"


# =============================================================================
# PHASE 4C4B3A: PROCESS-LOCAL KEY EXPLICIT TESTS (new tests 1-14)
# =============================================================================


class TestProcessLocalKeyContract:
    """Phase 4C4B3A: 14 explicit tests for the corrected coordinator interface.

    These tests prove the key contract separation between the durable
    repository key (owner_hash + frontend_id) and the process-local session
    key (opaque plc_v1_ digest).
    """

    # ---------- helpers -------------------------------------------------------

    @staticmethod
    def _make() -> tuple:
        """Return (adapter, store, coord) with a clean in-memory backing."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)
        return adapter, store, coord

    # ---------- test 1 --------------------------------------------------------

    def test_plk_01_durable_record_uses_raw_frontend_id(self):
        """1. Coordinator durable record is keyed by owner_hash + frontend_id."""
        adapter, store, coord = self._make()
        # Create and reset
        coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        # Authoritative record must exist under the durable key
        durable_key = DurableGenieSessionKey(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
        )
        result = adapter.load(durable_key)
        assert result is not None
        assert result.record.status == ConversationStatus.RESET
        # Confirm durable record carries the raw frontend ID
        assert result.record.frontend_conversation_id == _VALID_FRONTEND_ID

    # ---------- test 2 --------------------------------------------------------

    def test_plk_02_local_removal_uses_opaque_key_not_frontend_id(self):
        """2. GenieSessionStore removal uses the opaque key, not the raw frontend ID."""
        adapter, store, coord = self._make()
        _setup_active_record(adapter)

        # Populate session under opaque key
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "genie-xyz")
        # Also populate under raw frontend ID (must NOT be removed)
        store.set_genie_conversation_id(_VALID_FRONTEND_ID, "genie-frontend-only")

        coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )

        # Opaque-key session removed
        assert store.get_session(_VALID_LOCAL_KEY) is None
        # Raw frontend_id session untouched
        assert store.get_genie_conversation_id(_VALID_FRONTEND_ID) == "genie-frontend-only"

    # ---------- test 3 --------------------------------------------------------

    def test_plk_03_session_under_opaque_key_physically_removed(self):
        """3. Session stored under opaque key is physically removed after success."""
        adapter, store, coord = self._make()
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "genie-remove-me")
        store.update_context(_VALID_LOCAL_KEY, last_intent="AGGREGATION", last_entities=["US"])

        coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )

        assert store.get_session(_VALID_LOCAL_KEY) is None
        assert store.get_context_snapshot(_VALID_LOCAL_KEY) == {}

    # ---------- test 4 --------------------------------------------------------

    def test_plk_04_session_under_raw_frontend_id_not_touched(self):
        """4. Session stored only under raw frontend ID is never removed by coordinator."""
        adapter, store, coord = self._make()
        _setup_active_record(adapter)

        # Store ONLY under the raw frontend ID, NOT the opaque key
        store.set_genie_conversation_id(_VALID_FRONTEND_ID, "should-survive")

        coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )

        # Raw frontend_id session must NOT have been removed
        assert store.get_genie_conversation_id(_VALID_FRONTEND_ID) == "should-survive"

    # ---------- test 5 --------------------------------------------------------

    def test_plk_05_another_owners_session_untouched(self):
        """5. Another owner's opaque session key remains untouched after reset."""
        adapter, store, coord = self._make()
        _setup_active_record(adapter)

        # Owner A's opaque key
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "owner-a-session")
        # Owner B's opaque key (different owner, same session + frontend)
        store.set_genie_conversation_id(_VALID_LOCAL_KEY_B, "owner-b-session")

        # Reset owner A
        coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )

        # Owner A's session removed
        assert store.get_session(_VALID_LOCAL_KEY) is None
        # Owner B's session untouched
        assert store.get_genie_conversation_id(_VALID_LOCAL_KEY_B) == "owner-b-session"

    # ---------- test 6 --------------------------------------------------------

    def test_plk_06_invalid_process_local_key_rejected(self):
        """6. Invalid process-local key (not plc_v1_ format) is rejected."""
        _, _, coord = self._make()
        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key="not-a-valid-key",
            )
            assert False, "Should have raised"
        except ResetCoordinatorInvalidInputError:
            pass

    # ---------- test 7 --------------------------------------------------------

    def test_plk_07_raw_owner_hash_cannot_be_passed_as_local_key(self):
        """7. Raw owner hash (64 hex chars, no plc_v1_ prefix) is rejected."""
        _, _, coord = self._make()
        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_OWNER,  # raw hash, wrong format
            )
            assert False, "Should have raised"
        except ResetCoordinatorInvalidInputError:
            pass

    # ---------- test 8 --------------------------------------------------------

    def test_plk_08_raw_session_id_cannot_be_passed_as_local_key(self):
        """8. Raw session ID string cannot satisfy the plc_v1_ format requirement."""
        _, _, coord = self._make()
        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key="test-session-001",
            )
            assert False, "Should have raised"
        except ResetCoordinatorInvalidInputError:
            pass

    # ---------- test 9 --------------------------------------------------------

    def test_plk_09_raw_frontend_id_cannot_be_passed_as_local_key(self):
        """9. Raw frontend conversation ID cannot satisfy the plc_v1_ format requirement."""
        _, _, coord = self._make()
        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_FRONTEND_ID,  # raw ID, not opaque
            )
            assert False, "Should have raised"
        except ResetCoordinatorInvalidInputError:
            pass

    # ---------- test 10 -------------------------------------------------------

    def test_plk_10_durable_failure_retains_opaque_local_session(self):
        """10. Durable set_status failure must NOT remove the opaque local session."""
        adapter, store, coord = self._make()
        _setup_active_record(adapter)
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "retain-on-failure")

        def fail_set_status(k, status, **kwargs):
            raise DurableGenieSessionUnavailableError("down")

        adapter.set_status = fail_set_status

        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
        except ResetCoordinatorUnavailableError:
            pass

        assert store.get_genie_conversation_id(_VALID_LOCAL_KEY) == "retain-on-failure"

    # ---------- test 11 -------------------------------------------------------

    def test_plk_11_successful_tombstone_removes_opaque_session(self):
        """11. Successful tombstone creation (missing → RESET) removes the opaque session."""
        adapter, store, coord = self._make()
        # No pre-existing durable record
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "tombstone-session")

        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )

        assert result.success is True
        assert result.outcome == ResetOutcome.TOMBSTONE_CREATED
        assert store.get_session(_VALID_LOCAL_KEY) is None

    # ---------- test 12 -------------------------------------------------------

    def test_plk_12_conflict_success_removes_opaque_session(self):
        """12. Conflict → reload shows RESET → local opaque session is removed."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        key, record = _setup_active_record(adapter)
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "conflict-session")

        def conflict_mutate_to_reset(k, status, **kwargs):
            bundle.repository.set_status(
                _VALID_OWNER, record.conversation_id,
                ConversationStatus.RESET, expected_version=record.version,
            )
            raise DurableGenieSessionVersionConflictError("conflict")

        adapter.set_status = conflict_mutate_to_reset

        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )

        assert result.success is True
        assert store.get_session(_VALID_LOCAL_KEY) is None

    # ---------- test 13 -------------------------------------------------------

    def test_plk_13_conflict_failure_retains_opaque_session(self):
        """13. Conflict → reload shows ACTIVE → local opaque session is retained."""
        adapter, store, coord = self._make()
        _setup_active_record(adapter)
        store.set_genie_conversation_id(_VALID_LOCAL_KEY, "active-conflict-retain")

        def always_conflict(k, status, **kwargs):
            raise DurableGenieSessionVersionConflictError("conflict")

        adapter.set_status = always_conflict

        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key=_VALID_LOCAL_KEY,
            )
        except ResetCoordinatorConflictError:
            pass

        assert store.get_genie_conversation_id(_VALID_LOCAL_KEY) == "active-conflict-retain"

    # ---------- test 14 -------------------------------------------------------

    def test_plk_14_result_and_exceptions_expose_no_local_key(self):
        """14. ResetResult repr and coordinator exceptions expose no local key value."""
        adapter, store, coord = self._make()
        # Success case
        result = coord.reset(
            owner_user_id_hash=_VALID_OWNER,
            frontend_conversation_id=_VALID_FRONTEND_ID,
            process_local_conversation_key=_VALID_LOCAL_KEY,
        )
        r = repr(result)
        assert _VALID_LOCAL_KEY not in r
        assert _VALID_OWNER not in r
        assert _VALID_FRONTEND_ID not in r

        # Error case — invalid local key
        try:
            coord.reset(
                owner_user_id_hash=_VALID_OWNER,
                frontend_conversation_id=_VALID_FRONTEND_ID,
                process_local_conversation_key="invalid-key",
            )
        except ResetCoordinatorInvalidInputError as exc:
            e_repr = repr(exc)
            assert "invalid-key" not in e_repr
            assert _VALID_OWNER not in e_repr
            assert _VALID_FRONTEND_ID not in e_repr


# =============================================================================
# RUNNER
# =============================================================================

if __name__ == "__main__":
    import traceback

    test_classes = [
        TestInputValidation,
        TestExistingActive,
        TestExistingInactive,
        TestMissingTombstone,
        TestConflictHandling,
        TestMutationProhibitions,
        TestSessionStoreIntegration,
        TestAdditionalCoverage,
    ]

    passed = failed = total = 0
    print("=" * 70)
    print("PHASE 4C4B1: conversation_reset_coordinator.py unit tests")
    print("=" * 70)

    for cls in test_classes:
        instance = cls()
        for method_name in sorted(m for m in dir(instance) if m.startswith("test_")):
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
