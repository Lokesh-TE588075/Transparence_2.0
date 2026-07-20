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
# 16. No adapter delete, bind, update-message or touch during reset (FIX A)
# ===========================================================================


class TestNoProhibitedOperations:
    """Scenario 16: Reset only uses load, get_or_create, set_status.

    Behavioural spies attached to all four REAL adapter method names:
    - delete
    - bind_genie_conversation
    - update_last_genie_message
    - touch
    """

    def test_33_all_prohibited_ops_zero_calls_on_active(self):
        """All four prohibited ops have zero calls after ACTIVE reset."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_active(adapter, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        spy_delete = MagicMock(side_effect=AssertionError("delete called"))
        spy_bind = MagicMock(side_effect=AssertionError("bind_genie_conversation called"))
        spy_update_msg = MagicMock(side_effect=AssertionError("update_last_genie_message called"))
        spy_touch = MagicMock(side_effect=AssertionError("touch called"))

        adapter.delete = spy_delete
        adapter.bind_genie_conversation = spy_bind
        adapter.update_last_genie_message = spy_update_msg
        adapter.touch = spy_touch

        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )

        assert spy_delete.call_count == 0
        assert spy_bind.call_count == 0
        assert spy_update_msg.call_count == 0
        assert spy_touch.call_count == 0

    def test_34_prohibited_ops_on_tombstone_creation_path(self):
        """Prohibited ops not called even on MISS/tombstone-creation path."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        spy_delete = MagicMock(side_effect=AssertionError("delete called"))
        spy_bind = MagicMock(side_effect=AssertionError("bind_genie_conversation called"))
        spy_update_msg = MagicMock(side_effect=AssertionError("update_last_genie_message called"))
        spy_touch = MagicMock(side_effect=AssertionError("touch called"))

        adapter.delete = spy_delete
        adapter.bind_genie_conversation = spy_bind
        adapter.update_last_genie_message = spy_update_msg
        adapter.touch = spy_touch

        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )

        assert spy_delete.call_count == 0
        assert spy_bind.call_count == 0
        assert spy_update_msg.call_count == 0
        assert spy_touch.call_count == 0

    def test_35_prohibited_ops_on_already_inactive_path(self):
        """Prohibited ops not called on ALREADY_INACTIVE path (RESET record)."""
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_with_status(adapter, ConversationStatus.RESET, _OWNER_A, _FRONTEND_1)

        spy_delete = MagicMock(side_effect=AssertionError("delete called"))
        spy_bind = MagicMock(side_effect=AssertionError("bind_genie_conversation called"))
        spy_update_msg = MagicMock(side_effect=AssertionError("update_last_genie_message called"))
        spy_touch = MagicMock(side_effect=AssertionError("touch called"))

        adapter.delete = spy_delete
        adapter.bind_genie_conversation = spy_bind
        adapter.update_last_genie_message = spy_update_msg
        adapter.touch = spy_touch

        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )

        assert spy_delete.call_count == 0
        assert spy_bind.call_count == 0
        assert spy_update_msg.call_count == 0
        assert spy_touch.call_count == 0


# ===========================================================================
# 17. No identifiers in HTTP responses (FIX E — caplog leakage)
# ===========================================================================


class TestNoIdentifiersInResponses:
    """Scenario 17: No sensitive values in HTTP response bodies or logs."""

    _SENSITIVE_TOKENS: List[str] = []

    @classmethod
    def _get_sensitive_tokens(cls) -> List[str]:
        """All tokens that must never appear in responses or logs."""
        return [
            _OWNER_A,
            _OWNER_B,
            _SESSION_A,
            _LOCAL_KEY_A,
            _LOCAL_KEY_B,
            "plc_v1_",
            _FRONTEND_1,
        ]

    def test_36_success_response_no_identifiers(self):
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
        body_str = json.dumps(json.loads(response.body))

        for token in self._get_sensitive_tokens():
            assert token not in body_str, f"Leaked: {token[:10]}..."

    def test_37_conflict_response_no_identifiers(self):
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
        body_str = json.dumps(json.loads(response.body))

        for token in self._get_sensitive_tokens():
            assert token not in body_str, f"Leaked: {token[:10]}..."

    def test_38_unavailable_response_no_identifiers(self):
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
        body_str = json.dumps(json.loads(response.body))

        for token in self._get_sensitive_tokens():
            assert token not in body_str, f"Leaked: {token[:10]}..."

    def test_39_unexpected_error_response_no_identifiers(self):
        identity = _make_identity(_OWNER_A)
        coord_mock = MagicMock()
        coord_mock.reset.side_effect = RuntimeError(f"crash with {_OWNER_A}")

        response = _run_route(
            _FRONTEND_1,
            identity_patch=lambda headers: identity,
            coordinator_patch=lambda: coord_mock,
            session_id=_SESSION_A,
        )
        import json
        body_str = json.dumps(json.loads(response.body))

        for token in self._get_sensitive_tokens():
            assert token not in body_str, f"Leaked: {token[:10]}..."


# ===========================================================================
# 18. Old-ID post-reset pipeline (FIX B) — real pipeline blocks old conv
# ===========================================================================


class TestOldIdPostResetPipeline:
    """Scenario 18: Real GeniePipeline with fake Genie client returns inactive
    for old frontend ID after reset, with no start_conversation/send_message
    or adapter bind/update/touch calls.
    """

    def _build_pipeline_and_reset(self):
        """Setup: active durable record → reset → return pipeline + adapter."""
        from app.services.genie_pipeline import GeniePipeline
        from app.services.durable_genie_session_runtime_factory import (
            DurableGenieSessionRuntimeBundle,
        )

        repo = InMemoryConversationRepository()
        repo_bundle = ConversationRepositoryBundle(
            repository=repo,
            backend=ConversationRepositoryBackend.MEMORY,
            durable=False,
        )
        adapter = DurableGenieSessionAdapter(
            repository_bundle=repo_bundle, cache_store=None, cache_enabled=False,
        )
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        # Create active record
        key = DurableGenieSessionKey(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        adapter.get_or_create(key)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-conv-original")

        # Reset
        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )

        # Build pipeline with recording client
        class _RecordingClient:
            def __init__(self):
                self.start_calls = []
                self.send_calls = []

            def start_conversation(self, space_id, message):
                self.start_calls.append(message)
                raise AssertionError("start_conversation must not be called")

            def send_message(self, space_id, conv_id, message):
                self.send_calls.append(message)
                raise AssertionError("send_message must not be called")

            def wait_for_message_completion(self, *a, **kw):
                return MagicMock(query_attachments=None, message_id="msg-x")

            def fetch_query_result(self, *a, **kw):
                return None

        client = _RecordingClient()
        pipeline = GeniePipeline(
            genie_client=client,
            session_store=store,
            space_id="space-lifecycle-test",
            fetch_query_results=False,
            enable_prompt_enrichment=False,
            enable_shape_validation=False,
            enable_table_summary=False,
        )
        # Attach durable bundle
        bundle = DurableGenieSessionRuntimeBundle(
            enabled=True, adapter=adapter, backend=MagicMock(), durable=True,
        )
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)
        return pipeline, client, adapter

    def test_40_old_id_returns_inactive_status(self):
        pipeline, client, adapter = self._build_pipeline_and_reset()
        result = pipeline.run(
            "show delayed shipments",
            _LOCAL_KEY_A,
            owner_key=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        assert result["status"] == "inactive"

    def test_41_old_id_has_fallback_recommended_false(self):
        pipeline, client, adapter = self._build_pipeline_and_reset()
        result = pipeline.run(
            "show delayed shipments",
            _LOCAL_KEY_A,
            owner_key=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        assert result.get("fallback_recommended") is False

    def test_42_old_id_no_start_conversation(self):
        pipeline, client, adapter = self._build_pipeline_and_reset()
        pipeline.run(
            "show delayed shipments",
            _LOCAL_KEY_A,
            owner_key=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        assert len(client.start_calls) == 0

    def test_43_old_id_no_send_message(self):
        pipeline, client, adapter = self._build_pipeline_and_reset()
        pipeline.run(
            "show delayed shipments",
            _LOCAL_KEY_A,
            owner_key=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        assert len(client.send_calls) == 0


# ===========================================================================
# 19. New-ID post-reset pipeline (FIX C) — new frontend ID unblocked
# ===========================================================================


class TestNewIdPostResetPipeline:
    """Scenario 19: New frontend ID after reset is NOT blocked as inactive.
    Pipeline takes the normal MISS/new-conversation path.
    """

    def test_44_new_id_not_inactive(self):
        from app.services.genie_pipeline import GeniePipeline
        from app.services.durable_genie_session_runtime_factory import (
            DurableGenieSessionRuntimeBundle,
        )

        repo = InMemoryConversationRepository()
        repo_bundle = ConversationRepositoryBundle(
            repository=repo,
            backend=ConversationRepositoryBackend.MEMORY,
            durable=False,
        )
        adapter = DurableGenieSessionAdapter(
            repository_bundle=repo_bundle, cache_store=None, cache_enabled=False,
        )
        store = GenieSessionStore()
        coord = ConversationResetCoordinator(adapter=adapter, session_store=store)

        # Setup and reset old frontend
        key = DurableGenieSessionKey(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        adapter.get_or_create(key)
        coord.reset(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
            process_local_conversation_key=_LOCAL_KEY_A,
        )

        # Build pipeline with successful client
        class _SuccessClient:
            def __init__(self):
                self.start_calls = []

            def start_conversation(self, space_id, message):
                self.start_calls.append(message)
                return {"conversation_id": "genie-new-conv", "message_id": "genie-new-msg"}

            def send_message(self, space_id, conv_id, message):
                return {"message_id": "genie-new-msg-2"}

            def wait_for_message_completion(self, space_id, conv_id, msg_id, **kw):
                class _Att:
                    content = "Here are delayed shipments."
                class _Resp:
                    query_attachments = None
                    message_id = msg_id
                    text_attachments = [_Att()]
                return _Resp()

            def fetch_query_result(self, *a, **kw):
                return None

        client = _SuccessClient()
        pipeline = GeniePipeline(
            genie_client=client,
            session_store=store,
            space_id="space-lifecycle-test",
            fetch_query_results=False,
            enable_prompt_enrichment=False,
            enable_shape_validation=False,
            enable_table_summary=False,
        )
        bundle = DurableGenieSessionRuntimeBundle(
            enabled=True, adapter=adapter, backend=MagicMock(), durable=True,
        )
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)

        # New frontend ID + new local key
        new_local_key = build_process_local_conversation_key(
            owner_user_id_hash=_OWNER_A,
            session_id=_SESSION_A,
            frontend_conversation_id=_FRONTEND_2,
        )

        result = pipeline.run(
            "show delayed shipments",
            new_local_key,
            owner_key=_OWNER_A,
            frontend_conversation_id=_FRONTEND_2,
        )
        # Must NOT be inactive — normal flow
        assert result["status"] != "inactive"
        # Client was called (new conversation started)
        assert len(client.start_calls) == 1


# ===========================================================================
# 20. Reset-versus-writeback race (FIX D) — tombstone wins in pipeline
# ===========================================================================


class TestResetVsWritebackPipelineRace:
    """Scenario 20: Pipeline MISS lookup → successful Genie exec → tombstone
    created by concurrent reset BEFORE writeback → pipeline detects non-ACTIVE
    on persist attempt → static inactive response, no bind/update, local removed.
    """

    def test_45_race_returns_inactive(self):
        """Real race: MISS → start_conversation → coordinator.reset() during
        wait_for_message_completion → writeback finds RESET tombstone → inactive.

        The coordinator is invoked inside the controlled Genie execution window
        (wait_for_message_completion) so the tombstone is created by production
        code between the initial MISS lookup and the durable writeback.
        """
        from app.services.genie_pipeline import GeniePipeline
        from app.services.durable_genie_session_runtime_factory import (
            DurableGenieSessionRuntimeBundle,
        )

        # --- Infrastructure: shared adapter + store (no pre-existing record) ---
        repo = InMemoryConversationRepository()
        repo_bundle = ConversationRepositoryBundle(
            repository=repo,
            backend=ConversationRepositoryBackend.MEMORY,
            durable=False,
        )
        adapter = DurableGenieSessionAdapter(
            repository_bundle=repo_bundle, cache_store=None, cache_enabled=False,
        )
        store = GenieSessionStore()

        # Real coordinator sharing the same adapter and store
        coordinator = ConversationResetCoordinator(
            adapter=adapter, session_store=store,
        )

        # --- Tracking counters ---
        start_calls = []
        send_calls = []
        reset_calls = []
        bind_calls = []
        update_msg_calls = []

        # Spy on adapter.bind_genie_conversation
        _orig_bind = adapter.bind_genie_conversation

        def _spy_bind(*a, **kw):
            bind_calls.append(1)
            return _orig_bind(*a, **kw)

        adapter.bind_genie_conversation = _spy_bind

        # Spy on adapter.update_last_genie_message
        _orig_update = adapter.update_last_genie_message

        def _spy_update(*a, **kw):
            update_msg_calls.append(1)
            return _orig_update(*a, **kw)

        adapter.update_last_genie_message = _spy_update

        # --- Completion object with proper text attachment ---
        class _TextAtt:
            def __init__(self, text):
                self.content = text

        class _CompletionResult:
            def __init__(self, msg_id):
                self.message_id = msg_id
                self.query_attachments = None
                self.text_attachments = [_TextAtt("Here are the delayed shipments.")]

        # --- Fake Genie client that triggers coordinator reset mid-execution ---
        class _RaceClient:
            def start_conversation(self_inner, space_id, message):
                start_calls.append({"space_id": space_id, "message": message})
                return {"conversation_id": "genie-race-conv", "message_id": "genie-race-msg"}

            def send_message(self_inner, space_id, conv_id, message):
                send_calls.append({"space_id": space_id, "conv_id": conv_id})
                return {"message_id": "genie-race-msg-2"}

            def wait_for_message_completion(self_inner, space_id, conv_id, msg_id, **kw):
                # *** RACE INJECTION POINT ***
                # At this point:
                #   - Durable lookup already returned MISS
                #   - start_conversation already executed
                #   - Durable writeback has NOT yet started
                # Invoke real coordinator to create RESET tombstone:
                result = coordinator.reset(
                    owner_user_id_hash=_OWNER_A,
                    frontend_conversation_id=_FRONTEND_1,
                    process_local_conversation_key=_LOCAL_KEY_A,
                )
                reset_calls.append(result)
                return _CompletionResult(msg_id)

            def fetch_query_result(self_inner, *a, **kw):
                return None

        # --- Build pipeline ---
        pipeline = GeniePipeline(
            genie_client=_RaceClient(),
            session_store=store,
            space_id="space-lifecycle-test",
            fetch_query_results=False,
            enable_prompt_enrichment=False,
            enable_shape_validation=False,
            enable_table_summary=False,
        )

        bundle = DurableGenieSessionRuntimeBundle(
            enabled=True, adapter=adapter, backend=MagicMock(), durable=True,
        )
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)

        # --- Execute pipeline ---
        result = pipeline.run(
            "show delayed",
            _LOCAL_KEY_A,
            owner_key=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )

        # === ASSERT ALL 10 RACE INVARIANTS ===

        # 1. start_conversation called exactly once
        assert len(start_calls) == 1, f"start_conversation count: {len(start_calls)}"

        # 2. send_message never called (new conversation, not existing)
        assert len(send_calls) == 0, f"send_message count: {len(send_calls)}"

        # 3. coordinator.reset() called exactly once (inside wait_for_message_completion)
        assert len(reset_calls) == 1, f"coordinator reset count: {len(reset_calls)}"

        # 4. adapter.bind_genie_conversation never called (writeback blocked)
        assert len(bind_calls) == 0, f"bind_genie_conversation count: {len(bind_calls)}"

        # 5. adapter.update_last_genie_message never called
        assert len(update_msg_calls) == 0, f"update_last_genie_message count: {len(update_msg_calls)}"

        # 6. Pipeline returns dedicated inactive response
        assert result["status"] == "inactive"

        # 7. fallback_recommended is False (no custom pipeline retry)
        assert result.get("fallback_recommended") is False

        # 8. Final durable status is RESET (tombstone preserved, no reactivation)
        durable_key = DurableGenieSessionKey(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        final_lookup = adapter.load(durable_key)
        assert final_lookup is not None
        assert final_lookup.record.status == ConversationStatus.RESET

        # 9. Local GenieSessionStore entry is absent
        assert store.get_session(_LOCAL_KEY_A) is None

        # 10. No durable reactivation (status remains RESET, not ACTIVE)
        assert final_lookup.record.genie_conversation_id is None

    def test_46_race_local_session_removed(self):
        """After race detection, local session is removed."""
        from app.services.genie_pipeline import GeniePipeline
        from app.services.durable_genie_session_runtime_factory import (
            DurableGenieSessionRuntimeBundle,
        )

        repo = InMemoryConversationRepository()
        repo_bundle = ConversationRepositoryBundle(
            repository=repo,
            backend=ConversationRepositoryBackend.MEMORY,
            durable=False,
        )
        adapter = DurableGenieSessionAdapter(
            repository_bundle=repo_bundle, cache_store=None, cache_enabled=False,
        )
        store = GenieSessionStore()
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-pre-existing")

        class _NoOpClient:
            def start_conversation(self, *a, **kw):
                return {"conversation_id": "x", "message_id": "y"}
            def send_message(self, *a, **kw):
                return {"message_id": "y"}
            def wait_for_message_completion(self, *a, **kw):
                class _A:
                    content = "result"
                class _R:
                    query_attachments = None
                    message_id = "y"
                    text_attachments = [_A()]
                return _R()
            def fetch_query_result(self, *a, **kw):
                return None

        pipeline = GeniePipeline(
            genie_client=_NoOpClient(),
            session_store=store,
            space_id="space-test",
            fetch_query_results=False,
            enable_prompt_enrichment=False,
            enable_shape_validation=False,
            enable_table_summary=False,
        )
        # Pre-create RESET tombstone
        key = DurableGenieSessionKey(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        create_result = adapter.get_or_create(key)
        adapter.set_status(key, ConversationStatus.RESET,
                           expected_version=create_result.record.version)

        bundle = DurableGenieSessionRuntimeBundle(
            enabled=True, adapter=adapter, backend=MagicMock(), durable=True,
        )
        setattr(pipeline, "_durable_session_runtime_bundle", bundle)

        pipeline.run(
            "show delayed",
            _LOCAL_KEY_A,
            owner_key=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        # Local session removed after inactive detection
        assert store.get_session(_LOCAL_KEY_A) is None


# ===========================================================================
# 21. Identifier leakage via caplog (FIX E)
# ===========================================================================


class TestNoLogLeakage:
    """Scenario 21: Logs must not contain sensitive identifiers."""

    def test_47_success_path_no_log_leakage(self, caplog):
        import logging
        bundle = _make_bundle()
        adapter = _make_adapter(bundle)
        store = GenieSessionStore()
        coord = _make_coordinator(adapter, store)

        _setup_active(adapter, _OWNER_A, _FRONTEND_1)
        store.set_genie_conversation_id(_LOCAL_KEY_A, "genie-lifecycle-test")

        identity = _make_identity(_OWNER_A)
        with caplog.at_level(logging.DEBUG):
            _run_route(
                _FRONTEND_1,
                identity_patch=lambda headers: identity,
                coordinator_patch=lambda: coord,
                session_id=_SESSION_A,
            )

        log_text = caplog.text
        assert _OWNER_A not in log_text
        assert _SESSION_A not in log_text
        assert _LOCAL_KEY_A not in log_text
        assert "plc_v1_" not in log_text

    def test_48_conflict_path_no_log_leakage(self, caplog):
        import logging
        identity = _make_identity(_OWNER_A)
        coord_mock = MagicMock()
        coord_mock.reset.side_effect = ResetCoordinatorConflictError("conflict")

        with caplog.at_level(logging.DEBUG):
            _run_route(
                _FRONTEND_1,
                identity_patch=lambda headers: identity,
                coordinator_patch=lambda: coord_mock,
                session_id=_SESSION_A,
            )

        log_text = caplog.text
        assert _OWNER_A not in log_text
        assert _SESSION_A not in log_text
        assert _LOCAL_KEY_A not in log_text
        assert "plc_v1_" not in log_text

    def test_49_unavailable_path_no_log_leakage(self, caplog):
        import logging
        identity = _make_identity(_OWNER_A)
        coord_mock = MagicMock()
        coord_mock.reset.side_effect = ResetCoordinatorUnavailableError("unavail")

        with caplog.at_level(logging.DEBUG):
            _run_route(
                _FRONTEND_1,
                identity_patch=lambda headers: identity,
                coordinator_patch=lambda: coord_mock,
                session_id=_SESSION_A,
            )

        log_text = caplog.text
        assert _OWNER_A not in log_text
        assert _SESSION_A not in log_text
        assert _LOCAL_KEY_A not in log_text
        assert "plc_v1_" not in log_text

    def test_50_unexpected_error_path_no_log_leakage(self, caplog):
        import logging
        identity = _make_identity(_OWNER_A)
        coord_mock = MagicMock()
        coord_mock.reset.side_effect = RuntimeError(f"crash {_OWNER_A}")

        with caplog.at_level(logging.DEBUG):
            _run_route(
                _FRONTEND_1,
                identity_patch=lambda headers: identity,
                coordinator_patch=lambda: coord_mock,
                session_id=_SESSION_A,
            )

        log_text = caplog.text
        assert _OWNER_A not in log_text
        assert _SESSION_A not in log_text
        assert _LOCAL_KEY_A not in log_text
        assert "plc_v1_" not in log_text


# ===========================================================================
# 22. Same-cookie/different-owner route-level derivation (FIX F)
# ===========================================================================


class TestSameCookieDifferentOwnerRouteLevel:
    """Scenario 22: Exercise the route key-derivation path — two trusted owners
    sharing the same session cookie and frontend ID produce different opaque
    keys delivered to the coordinator.
    """

    def test_51_route_delivers_distinct_keys_to_coordinator(self):
        """Two owners, same session + frontend → coordinator receives different keys."""
        identity_a = _make_identity(_OWNER_A)
        identity_b = _make_identity(_OWNER_B)

        received_keys: List[str] = []

        class _CapturingCoordinator:
            def reset(self, *, owner_user_id_hash, frontend_conversation_id,
                      process_local_conversation_key):
                received_keys.append(process_local_conversation_key)
                return ResetResult(success=True, outcome=ResetOutcome.TOMBSTONE_CREATED)

        coord = _CapturingCoordinator()

        _run_route(
            _FRONTEND_1,
            identity_patch=lambda headers: identity_a,
            coordinator_patch=lambda: coord,
            session_id=_SESSION_A,
        )
        _run_route(
            _FRONTEND_1,
            identity_patch=lambda headers: identity_b,
            coordinator_patch=lambda: coord,
            session_id=_SESSION_A,
        )

        assert len(received_keys) == 2
        assert received_keys[0] != received_keys[1]
        assert all(is_valid_process_local_key(k) for k in received_keys)

    def test_52_route_key_matches_expected_derivation(self):
        """Route-delivered key matches build_process_local_conversation_key output."""
        identity_a = _make_identity(_OWNER_A)

        received_keys: List[str] = []

        class _CapturingCoordinator:
            def reset(self, *, owner_user_id_hash, frontend_conversation_id,
                      process_local_conversation_key):
                received_keys.append(process_local_conversation_key)
                return ResetResult(success=True, outcome=ResetOutcome.TOMBSTONE_CREATED)

        coord = _CapturingCoordinator()

        _run_route(
            _FRONTEND_1,
            identity_patch=lambda headers: identity_a,
            coordinator_patch=lambda: coord,
            session_id=_SESSION_A,
        )

        expected = build_process_local_conversation_key(
            owner_user_id_hash=_OWNER_A,
            session_id=_SESSION_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        assert received_keys[0] == expected
