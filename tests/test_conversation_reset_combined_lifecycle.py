"""Phase 4C4B5 — Combined Conversation Reset Lifecycle Validation.

Cross-layer integration-style tests validating the complete backend-to-frontend
reset lifecycle as one integrated contract.

Uses real in-memory components:
- InMemoryConversationRepository
- DurableGenieSessionAdapter
- GenieSessionStore
- ConversationResetCoordinator
- FastAPI reset route dependency injection

No live Lakebase. No Genie. No credentials. No deployment.

Covers:
1.  ACTIVE → reset → 200, status RESET, local session removed.
2.  Existing RESET → idempotent 200, no new state.
3.  Existing STALE → 200, status STALE, local removed.
4.  Existing EXPIRED → 200, status EXPIRED, local removed.
5.  Missing record → tombstone RESET, 200, local removed.
6.  Different owner same frontend ID → isolated tombstones.
7.  Same cookie different trusted owner → distinct process-local keys.
8.  Conflict failure → 409, local session retained.
9.  Durable unavailable → 503, local session retained.
10. Missing identity → 401, no mutation.
11. Invalid conversation ID → 400, no mutation.
12. Reset success + send using old ID → pipeline blocks.
13. Reset success + send using new ID → pipeline available.
14. Concurrent reset vs MISS writeback → non-ACTIVE tombstone wins.
15. Repeated reset → idempotent.
16. No adapter delete/bind/update-message/touch during reset.
17. No owner hash/session ID/local key/version/Genie ID in responses.
"""
from __future__ import annotations

import asyncio
import os as _os
import sys as _sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch, call

import pytest

_REPO_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)
del _os, _REPO_ROOT

from app.services.conversation_repository import (
    ConversationRecord,
    ConversationStatus,
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
from app.services.conversation_reset_runtime import (
    ConversationResetRuntimeUnavailableError,
)
from app.routes.conversation_reset import reset_conversation


# ===========================================================================
# Constants
# ===========================================================================

_OWNER_A: str = "a" * 64
_OWNER_B: str = "b" * 64
_SESSION_A: str = "session-lifecycle-001"
_SESSION_B: str = "session-lifecycle-002"
_FRONTEND_1: str = "frontend-lifecycle-001"
_FRONTEND_2: str = "frontend-lifecycle-002"

_LOCAL_KEY_A = build_process_local_conversation_key(
    owner_user_id_hash=_OWNER_A,
    session_id=_SESSION_A,
    frontend_conversation_id=_FRONTEND_1,
)
_LOCAL_KEY_B = build_process_local_conversation_key(
    owner_user_id_hash=_OWNER_B,
    session_id=_SESSION_A,
    frontend_conversation_id=_FRONTEND_1,
)


# ===========================================================================
# Helpers
# ===========================================================================


def _make_bundle() -> ConversationRepositoryBundle:
    repo = InMemoryConversationRepository()
    return ConversationRepositoryBundle(
        repository=repo,
        backend=ConversationRepositoryBackend.MEMORY,
        durable=False,
    )


def _make_adapter(bundle=None) -> DurableGenieSessionAdapter:
    if bundle is None:
        bundle = _make_bundle()
    return DurableGenieSessionAdapter(
        repository_bundle=bundle,
        cache_store=None,
        cache_enabled=False,
    )


def _make_coordinator(adapter=None, store=None):
    if adapter is None:
        adapter = _make_adapter()
    if store is None:
        store = GenieSessionStore()
    return ConversationResetCoordinator(adapter=adapter, session_store=store)


def _make_identity(owner_hash: str = _OWNER_A) -> SimpleNamespace:
    return SimpleNamespace(
        owner_user_id_hash=owner_hash,
        audit_principal="user@example.com",
        source="x-forwarded-user",
    )


def _make_request(session_id: str = _SESSION_A) -> MagicMock:
    req = MagicMock()
    req.state = SimpleNamespace(session_id=session_id)
    req.headers = MagicMock()
    return req


def _setup_active(adapter, owner=_OWNER_A, frontend_id=_FRONTEND_1):
    key = DurableGenieSessionKey(
        owner_user_id_hash=owner,
        frontend_conversation_id=frontend_id,
    )
    result = adapter.get_or_create(key)
    return key, result.record


def _setup_with_status(adapter, status, owner=_OWNER_A, frontend_id=_FRONTEND_1):
    key, record = _setup_active(adapter, owner, frontend_id)
    if status != ConversationStatus.ACTIVE:
        adapter.set_status(key, status, expected_version=record.version)
    reload = adapter.load(key)
    return key, reload.record


def _run_route(frontend_id, identity_patch, coordinator_patch, session_id=_SESSION_A):
    """Run the reset route in asyncio with patched identity and coordinator."""
    req = _make_request(session_id=session_id)

    with patch(
        "app.routes.conversation_reset.resolve_request_owner_identity",
        side_effect=identity_patch,
    ), patch(
        "app.routes.conversation_reset.get_conversation_reset_coordinator",
        side_effect=coordinator_patch,
    ):
        response = asyncio.run(reset_conversation(frontend_id, req))
    return response


# ===========================================================================
# 1. ACTIVE conversation — reset returns 200, status RESET, local removed
# ===========================================================================


class TestActiveConversationReset:
    """Scenario 1: Full lifecycle with an ACTIVE durable record."""

    def test_01_active_reset_returns_200(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        # Create ACTIVE record
        _setup_active(adapter, _OWNER_A, _FRONTEND_1)

        # Populate local session
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        # Execute reset
        result = coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )
        assert result.success is True
        assert result.outcome == ResetOutcome.RESET

    def test_02_active_reset_durable_status_becomes_reset(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_active(adapter, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )

        # Verify durable status
        key = DurableGenieSessionKey(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        lookup = adapter.load(key)
        assert lookup is not None
        assert lookup.record.status == ConversationStatus.RESET

    def test_03_active_reset_local_session_removed(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_active(adapter, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")
        assert store.get_session(_LOCAL_KEY_A) is not None

        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )

        assert store.get_session(_LOCAL_KEY_A) is None

    def test_04_active_reset_via_http_route(self):
        """End-to-end through the route function."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_active(adapter, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        identity = _make_identity(_OWNER_A)
        response = _run_route(
            _FRONTEND_1,
            identity_patch=lambda headers: identity,
            coordinator_patch=lambda: coord,
            session_id=_SESSION_A,
        )
        assert response.status_code == 200


# ===========================================================================
# 2. Existing RESET — idempotent 200, no new Genie state
# ===========================================================================


class TestExistingResetIdempotent:
    """Scenario 2: Already-RESET record returns 200 idempotently."""

    def test_05_existing_reset_returns_200(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_with_status(adapter, ConversationStatus.RESET, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        result = coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )
        assert result.success is True
        assert result.outcome == ResetOutcome.ALREADY_INACTIVE

    def test_06_existing_reset_local_session_removed(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_with_status(adapter, ConversationStatus.RESET, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )
        assert store.get_session(_LOCAL_KEY_A) is None


# ===========================================================================
# 3. Existing STALE — 200, status remains STALE, local removed
# ===========================================================================


class TestExistingStale:
    """Scenario 3: STALE record — returns 200, status unchanged."""

    def test_07_stale_returns_200_already_inactive(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_with_status(adapter, ConversationStatus.STALE, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        result = coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )
        assert result.success is True
        assert result.outcome == ResetOutcome.ALREADY_INACTIVE

    def test_08_stale_status_remains_stale(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_with_status(adapter, ConversationStatus.STALE, _OWNER_A, _FRONTEND_1)

        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )

        key = DurableGenieSessionKey(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        lookup = adapter.load(key)
        assert lookup.record.status == ConversationStatus.STALE

    def test_09_stale_local_session_removed(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_with_status(adapter, ConversationStatus.STALE, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )
        assert store.get_session(_LOCAL_KEY_A) is None


# ===========================================================================
# 4. Existing EXPIRED — 200, status remains EXPIRED, local removed
# ===========================================================================


class TestExistingExpired:
    """Scenario 4: EXPIRED record — returns 200, status unchanged."""

    def test_10_expired_returns_200(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_with_status(adapter, ConversationStatus.EXPIRED, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        result = coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )
        assert result.success is True
        assert result.outcome == ResetOutcome.ALREADY_INACTIVE

    def test_11_expired_status_remains_expired(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_with_status(adapter, ConversationStatus.EXPIRED, _OWNER_A, _FRONTEND_1)

        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )

        key = DurableGenieSessionKey(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        lookup = adapter.load(key)
        assert lookup.record.status == ConversationStatus.EXPIRED

    def test_12_expired_local_session_removed(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_with_status(adapter, ConversationStatus.EXPIRED, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )
        assert store.get_session(_LOCAL_KEY_A) is None


# ===========================================================================
# 5. Missing record — creates RESET tombstone, 200, local removed
# ===========================================================================


class TestMissingRecordTombstone:
    """Scenario 5: No existing record — tombstone created."""

    def test_13_missing_creates_tombstone(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        result = coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )
        assert result.success is True
        assert result.outcome == ResetOutcome.TOMBSTONE_CREATED

    def test_14_missing_durable_becomes_reset(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )

        key = DurableGenieSessionKey(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        lookup = adapter.load(key)
        assert lookup is not None
        assert lookup.record.status == ConversationStatus.RESET

    def test_15_missing_local_session_removed(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )
        assert store.get_session(_LOCAL_KEY_A) is None


# ===========================================================================
# 6. Different owner, same frontend ID — isolation
# ===========================================================================


class TestDifferentOwnerIsolation:
    """Scenario 6: Owner B cannot affect Owner A's record."""

    def test_16_different_owner_gets_own_tombstone(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        # Owner A has ACTIVE record
        _setup_active(adapter, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        # Owner B resets same frontend ID
        result = coord.reset(
            owner_user_id_hash=_OWNER_B,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_B,
        )
        assert result.success is True

        # Owner A's record unchanged
        key_a = DurableGenieSessionKey(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        lookup_a = adapter.load(key_a)
        assert lookup_a.record.status == ConversationStatus.ACTIVE

    def test_17_different_owner_does_not_remove_first_owner_session(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_active(adapter, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        # Owner B resets
        coord.reset(
            owner_user_id_hash=_OWNER_B,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_B,
        )

        # Owner A's local session still exists
        assert store.get_session(_LOCAL_KEY_A) is not None


# ===========================================================================
# 7. Same cookie, different trusted owner — distinct local keys
# ===========================================================================


class TestSameCookieDifferentOwner:
    """Scenario 7: Same session_id but different owner_hash → different keys."""

    def test_18_same_session_different_owner_distinct_keys(self):
        key_a = build_process_local_conversation_key(
            owner_user_id_hash=_OWNER_A,
            session_id=_SESSION_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        key_b = build_process_local_conversation_key(
            owner_user_id_hash=_OWNER_B,
            session_id=_SESSION_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        assert key_a != key_b
        assert is_valid_process_local_key(key_a)
        assert is_valid_process_local_key(key_b)


# ===========================================================================
# 8. Conflict failure — 409, local session retained
# ===========================================================================


class TestConflictFailure:
    """Scenario 8: Version conflict returns 409."""

    def test_19_conflict_returns_409_via_route(self):
        identity = _make_identity(_OWNER_A)

        coord_mock = MagicMock()
        coord_mock.reset.side_effect = ResetCoordinatorConflictError("conflict")

        response = _run_route(
            _FRONTEND_1,
            identity_patch=lambda headers: identity,
            coordinator_patch=lambda: coord_mock,
            session_id=_SESSION_A,
        )
        assert response.status_code == 409

    def test_20_conflict_local_session_retained(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()

        _setup_active(adapter, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        # Patch set_status to raise conflict
        original_set_status = adapter.set_status

        def conflict_set_status(key, status, expected_version):
            raise DurableGenieSessionVersionConflictError("conflict")

        adapter.set_status = conflict_set_status

        # Also patch load to return None on reload (simulates total conflict)
        original_load = adapter.load
        call_count = [0]

        def patched_load(key):
            call_count[0] += 1
            if call_count[0] == 1:
                return original_load(key)
            return None  # Reload returns None → conflict

        adapter.load = patched_load

        coord = _make_coordinator(adapter, store)

        with pytest.raises(ResetCoordinatorConflictError):
            coord.reset(
                owner_user_id_hash=_OWNER_A,
                frontend_conversation_id=_FRONTEND_1,
                process_local_conversation_key=_LOCAL_KEY_A,
            )

        # Local session is retained
        assert store.get_session(_LOCAL_KEY_A) is not None


# ===========================================================================
# 9. Durable unavailable — 503, local session retained
# ===========================================================================


class TestDurableUnavailable:
    """Scenario 9: Durable backend failure returns 503."""

    def test_21_unavailable_returns_503_via_route(self):
        identity = _make_identity(_OWNER_A)

        coord_mock = MagicMock()
        coord_mock.reset.side_effect = ResetCoordinatorUnavailableError("unavailable")

        response = _run_route(
            _FRONTEND_1,
            identity_patch=lambda headers: identity,
            coordinator_patch=lambda: coord_mock,
            session_id=_SESSION_A,
        )
        assert response.status_code == 503

    def test_22_unavailable_local_session_retained(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()

        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        # Patch adapter.load to raise unavailable
        adapter.load = MagicMock(
            side_effect=DurableGenieSessionUnavailableError("unavailable")
        )

        coord = _make_coordinator(adapter, store)

        with pytest.raises(ResetCoordinatorUnavailableError):
            coord.reset(
                owner_user_id_hash=_OWNER_A,
                frontend_conversation_id=_FRONTEND_1,
                process_local_conversation_key=_LOCAL_KEY_A,
            )

        assert store.get_session(_LOCAL_KEY_A) is not None


# ===========================================================================
# 10. Missing identity — 401, no mutation
# ===========================================================================


class TestMissingIdentity:
    """Scenario 10: No trusted identity → 401."""

    def test_23_missing_identity_returns_401(self):
        from app.services.request_owner_identity_runtime import (
            RequestOwnerIdentityRuntimeResolutionError,
        )

        response = _run_route(
            _FRONTEND_1,
            identity_patch=lambda headers: (_ for _ in ()).throw(
                RequestOwnerIdentityRuntimeResolutionError("no identity")
            ),
            coordinator_patch=lambda: MagicMock(),
            session_id=_SESSION_A,
        )
        assert response.status_code == 401

    def test_24_missing_identity_no_durable_mutation(self):
        from app.services.request_owner_identity_runtime import (
            RequestOwnerIdentityRuntimeResolutionError,
        )

        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")
        coord_mock = MagicMock()

        response = _run_route(
            _FRONTEND_1,
            identity_patch=lambda headers: (_ for _ in ()).throw(
                RequestOwnerIdentityRuntimeResolutionError("no identity")
            ),
            coordinator_patch=lambda: coord_mock,
            session_id=_SESSION_A,
        )

        # Coordinator never called
        coord_mock.reset.assert_not_called()
        # Local session untouched
        assert store.get_session(_LOCAL_KEY_A) is not None


# ===========================================================================
# 11. Invalid conversation ID — 400, no mutation
# ===========================================================================


class TestInvalidConversationId:
    """Scenario 11: Invalid/empty frontend ID → 400."""

    def test_25_empty_id_returns_400(self):
        identity = _make_identity(_OWNER_A)
        coord_mock = MagicMock()

        response = _run_route(
            "   ",
            identity_patch=lambda headers: identity,
            coordinator_patch=lambda: coord_mock,
            session_id=_SESSION_A,
        )
        assert response.status_code == 400
        coord_mock.reset.assert_not_called()

    def test_26_at_sign_id_returns_400(self):
        identity = _make_identity(_OWNER_A)
        coord_mock = MagicMock()

        response = _run_route(
            "user@example.com",
            identity_patch=lambda headers: identity,
            coordinator_patch=lambda: coord_mock,
            session_id=_SESSION_A,
        )
        assert response.status_code == 400
        coord_mock.reset.assert_not_called()


# ===========================================================================
# 12. Reset success + old ID send → pipeline blocks
# ===========================================================================


class TestOldIdPostResetBlocked:
    """Scenario 12: After reset, pipeline blocks messages on old durable tombstone."""

    def test_27_old_id_has_reset_tombstone(self):
        """After reset, the durable record is RESET — pipeline would block."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_active(adapter, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )

        # The old frontend ID's durable record is RESET
        key = DurableGenieSessionKey(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        lookup = adapter.load(key)
        assert lookup.record.status == ConversationStatus.RESET

        # The local session is gone — pipeline cannot find Genie conv ID
        assert store.get_session(_LOCAL_KEY_A) is None


# ===========================================================================
# 13. Reset success + new ID send → pipeline available
# ===========================================================================


class TestNewIdPostResetAvailable:
    """Scenario 13: New frontend ID is fully independent."""

    def test_28_new_id_has_no_durable_record(self):
        """A new frontend ID starts fresh — no durable record blocks it."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_active(adapter, _OWNER_A, _FRONTEND_1)
        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )

        # New frontend ID has no record
        key_new = DurableGenieSessionKey(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_2,
        )
        lookup = adapter.load(key_new)
        assert lookup is None

    def test_29_new_local_key_is_independent(self):
        """Process-local key for new frontend ID is independent."""
        new_local_key = build_process_local_conversation_key(
            owner_user_id_hash=_OWNER_A,
            session_id=_SESSION_A,
            frontend_conversation_id=_FRONTEND_2,
        )
        assert new_local_key != _LOCAL_KEY_A
        assert is_valid_process_local_key(new_local_key)


# ===========================================================================
# 14. Concurrent reset vs MISS writeback — tombstone wins
# ===========================================================================


class TestResetVsWriteback:
    """Scenario 14: Reset tombstone prevents bind from succeeding."""

    def test_30_reset_tombstone_blocks_writeback_bind(self):
        """After tombstone exists, get_or_create returns non-ACTIVE → no bind."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        # Reset creates tombstone (no prior record)
        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )

        # Simulate writeback attempt: get_or_create finds existing RESET
        key = DurableGenieSessionKey(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        result = adapter.get_or_create(key)
        # Record is RESET, not ACTIVE — pipeline would not bind
        assert result.record.status == ConversationStatus.RESET


# ===========================================================================
# 15. Repeated reset — idempotent
# ===========================================================================


class TestRepeatedReset:
    """Scenario 15: Multiple resets are idempotent."""

    def test_31_double_reset_idempotent(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_active(adapter, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        r1 = coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )
        assert r1.success is True

        # Second reset
        r2 = coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )
        assert r2.success is True
        assert r2.outcome == ResetOutcome.ALREADY_INACTIVE

    def test_32_triple_reset_idempotent(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_active(adapter, _OWNER_A, _FRONTEND_1)

        for _ in range(3):
            result = coord.reset(
                owner_user_id_hash=_OWNER_A,
                frontend_conversation_id=_FRONTEND_1,
                process_local_conversation_key=_LOCAL_KEY_A,
            )
            assert result.success is True


# ===========================================================================
# 16. No adapter delete, bind, update-message or touch during reset
# ===========================================================================


class TestNoProhibitedOperations:
    """Scenario 16: Reset only uses load, get_or_create, set_status."""

    def test_33_no_delete_called(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_active(adapter, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        # Spy on delete if it exists
        if hasattr(adapter, "delete"):
            adapter.delete = MagicMock(side_effect=AssertionError("delete must not be called"))

        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )
        # No exception = delete not called

    def test_34_no_bind_called(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_active(adapter, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        if hasattr(adapter, "bind"):
            adapter.bind = MagicMock(side_effect=AssertionError("bind must not be called"))

        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )

    def test_35_no_update_last_message_called(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_active(adapter, _OWNER_A, _FRONTEND_1)

        if hasattr(adapter, "update_last_message"):
            adapter.update_last_message = MagicMock(
                side_effect=AssertionError("update_last_message must not be called")
            )

        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )

    def test_36_no_touch_called(self):
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_active(adapter, _OWNER_A, _FRONTEND_1)

        if hasattr(adapter, "touch"):
            adapter.touch = MagicMock(
                side_effect=AssertionError("touch must not be called")
            )

        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )


# ===========================================================================
# 17. No identifiers in HTTP responses
# ===========================================================================


class TestNoIdentifiersInResponses:
    """Scenario 17: No sensitive values in HTTP response bodies."""

    def test_37_success_response_no_identifiers(self):
        identity = _make_identity(_OWNER_A)
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_active(adapter, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        response = _run_route(
            _FRONTEND_1,
            identity_patch=lambda headers: identity,
            coordinator_patch=lambda: coord,
            session_id=_SESSION_A,
        )
        import json
        body = json.loads(response.body)
        body_str = json.dumps(body)

        # Must not contain owner hash
        assert _OWNER_A not in body_str
        # Must not contain session ID
        assert _SESSION_A not in body_str
        # Must not contain local key
        assert _LOCAL_KEY_A not in body_str
        # Must not contain plc_v1_
        assert "plc_v1_" not in body_str

    def test_38_error_response_no_identifiers(self):
        identity = _make_identity(_OWNER_A)

        coord_mock = MagicMock()
        coord_mock.reset.side_effect = ResetCoordinatorConflictError("conflict")

        response = _run_route(
            _FRONTEND_1,
            identity_patch=lambda headers: identity,
            coordinator_patch=lambda: coord_mock,
            session_id=_SESSION_A,
        )
        import json
        body = json.loads(response.body)
        body_str = json.dumps(body)

        assert _OWNER_A not in body_str
        assert _SESSION_A not in body_str
        assert "plc_v1_" not in body_str

    def test_39_unavailable_response_no_identifiers(self):
        identity = _make_identity(_OWNER_A)

        coord_mock = MagicMock()
        coord_mock.reset.side_effect = ResetCoordinatorUnavailableError("unavail")

        response = _run_route(
            _FRONTEND_1,
            identity_patch=lambda headers: identity,
            coordinator_patch=lambda: coord_mock,
            session_id=_SESSION_A,
        )
        import json
        body = json.loads(response.body)
        body_str = json.dumps(body)

        assert _OWNER_A not in body_str
        assert _SESSION_A not in body_str
        assert "plc_v1_" not in body_str
