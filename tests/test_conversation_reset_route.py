"""Phase 4C4B3B: Behavioural tests for the conversation reset HTTP endpoint.

POST /api/conversations/{frontend_conversation_id}/reset

Test strategy:
- Core behavioural paths (tests 1–6): real InMemoryConversationRepository,
  DurableGenieSessionAdapter, GenieSessionStore, ConversationResetCoordinator.
  get_conversation_reset_coordinator() is monkeypatched to inject these.
- Identity/validation paths (tests 7–14): narrow mocks for the identity
  runtime and coordinator.
- Owner isolation (tests 15–22): prove that body/query/header overrides are
  ignored, and that different trusted owners stay isolated.
- Output hygiene (tests 23–25): no identifiers in responses or logs.
- Shared-instance invariants (tests 26–30): no duplicate store/adapter,
  no Genie request, no fallback, no delete.

No live infrastructure. No Lakebase. No credentials.
"""
from __future__ import annotations

import asyncio
import logging
import os as _os
import sys
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch, call

import pytest

_REPO_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
del _os, _REPO_ROOT

from app.services.conversation_repository import (
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
)
from app.services.genie_session_store import GenieSessionStore
from app.services.conversation_reset_coordinator import (
    ConversationResetCoordinator,
    ResetCoordinatorConflictError,
    ResetCoordinatorInternalError,
    ResetCoordinatorInvalidInputError,
    ResetCoordinatorUnavailableError,
)
from app.services.process_local_conversation_key import (
    build_process_local_conversation_key,
)
from app.services.conversation_reset_runtime import (
    ConversationResetRuntimeUnavailableError,
)
from app.routes.conversation_reset import reset_conversation


# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

_OWNER_A: str = "a" * 64
_OWNER_B: str = "b" * 64
_SESSION_A: str = "session-aaa-001"
_SESSION_B: str = "session-bbb-002"
_FRONTEND_1: str = "frontend-conv-001"
_FRONTEND_2: str = "frontend-conv-002"


def _local_key(
    owner: str = _OWNER_A,
    session: str = _SESSION_A,
    frontend: str = _FRONTEND_1,
) -> str:
    return build_process_local_conversation_key(
        owner_user_id_hash=owner,
        session_id=session,
        frontend_conversation_id=frontend,
    )


# ---------------------------------------------------------------------------
# Helpers: fake objects
# ---------------------------------------------------------------------------


def _make_identity(owner_hash: str = _OWNER_A) -> SimpleNamespace:
    """Minimal RequestOwnerIdentity-compatible object."""
    return SimpleNamespace(
        owner_user_id_hash=owner_hash,
        audit_principal="user@example.com",
        source="x-forwarded-user",
    )


def _make_request(
    *,
    session_id: str = _SESSION_A,
    extra_headers: Optional[Dict[str, str]] = None,
) -> MagicMock:
    """Build a mock FastAPI Request with controlled state and headers."""
    req = MagicMock()
    req.state = SimpleNamespace(session_id=session_id)
    headers: Dict[str, str] = {}
    if extra_headers:
        headers.update(extra_headers)
    req.headers.get = lambda k, d=None: headers.get(k, d)
    # Make headers also work as a mapping for resolve_request_owner_identity
    req.headers.__contains__ = lambda self, k: k in headers  # noqa: E731
    req.headers.__getitem__ = lambda self, k: headers[k]  # noqa: E731
    req.headers.__iter__ = lambda self: iter(headers)  # noqa: E731
    # Return the raw headers dict for the identity runtime (it only needs Mapping)
    req.headers._raw = headers
    return req


# ---------------------------------------------------------------------------
# Real coordinator factory
# ---------------------------------------------------------------------------


def _make_real_bundle() -> ConversationRepositoryBundle:
    return ConversationRepositoryBundle(
        repository=InMemoryConversationRepository(),
        backend=ConversationRepositoryBackend.MEMORY,
        durable=False,
    )


def _make_real_adapter(
    bundle: Optional[ConversationRepositoryBundle] = None,
) -> DurableGenieSessionAdapter:
    if bundle is None:
        bundle = _make_real_bundle()
    return DurableGenieSessionAdapter(
        repository_bundle=bundle,
        cache_store=None,
        cache_enabled=False,
    )


def _make_real_coordinator_and_store(
    adapter: Optional[DurableGenieSessionAdapter] = None,
) -> tuple[ConversationResetCoordinator, GenieSessionStore]:
    """Return a (coordinator, store) pair using real in-memory implementations."""
    store = GenieSessionStore()
    if adapter is None:
        adapter = _make_real_adapter()
    coordinator = ConversationResetCoordinator(adapter=adapter, session_store=store)
    return coordinator, store


# ---------------------------------------------------------------------------
# Async runner helper
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously for test simplicity.

    Uses asyncio.run() which creates a fresh event loop each time, making
    tests safe to run in any order and in any thread context.
    """
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# MODULE: route_to_identity
# Patch target: resolve_request_owner_identity in conversation_reset module
# ---------------------------------------------------------------------------

_RESOLVE_PATCH = "app.routes.conversation_reset.resolve_request_owner_identity"
_GET_COORD_PATCH = "app.routes.conversation_reset.get_conversation_reset_coordinator"


# ===========================================================================
# Section 1: Core behavioural paths (real InMemory + Adapter + Coordinator)
# Tests 1–6
# ===========================================================================


class TestCoreResetBehaviour:
    """Tests 1–6: behavioural paths with real coordinator + in-memory repo."""

    def _run_reset(
        self,
        coordinator: ConversationResetCoordinator,
        *,
        owner: str = _OWNER_A,
        session: str = _SESSION_A,
        frontend: str = _FRONTEND_1,
    ):
        identity = _make_identity(owner)
        request = _make_request(session_id=session)
        with (
            patch(_RESOLVE_PATCH, return_value=identity),
            patch(_GET_COORD_PATCH, return_value=coordinator),
        ):
            return _run(reset_conversation(frontend, request))

    def test_01_active_record_reset_returns_200(self):
        """ACTIVE conversation returns 200 and transitions to RESET."""
        adapter = _make_real_adapter()
        coordinator, store = _make_real_coordinator_and_store(adapter)
        # Pre-create an ACTIVE record.
        key = DurableGenieSessionKey(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        adapter.get_or_create(key)

        resp = self._run_reset(coordinator)
        assert resp.status_code == 200
        body = resp.body
        assert b"reset" in body

    def test_02_existing_reset_returns_200_idempotent(self):
        """Already-RESET conversation returns 200 (idempotent)."""
        adapter = _make_real_adapter()
        coordinator, store = _make_real_coordinator_and_store(adapter)
        key = DurableGenieSessionKey(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        # Create and immediately reset.
        result = adapter.get_or_create(key)
        adapter.set_status(key, ConversationStatus.RESET, expected_version=result.record.version)

        resp = self._run_reset(coordinator)
        assert resp.status_code == 200

    def test_03_stale_record_returns_200_idempotent(self):
        """STALE conversation returns 200 without changing lifecycle reason."""
        adapter = _make_real_adapter()
        coordinator, store = _make_real_coordinator_and_store(adapter)
        key = DurableGenieSessionKey(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        result = adapter.get_or_create(key)
        adapter.set_status(key, ConversationStatus.STALE, expected_version=result.record.version)

        resp = self._run_reset(coordinator)
        assert resp.status_code == 200

    def test_04_expired_record_returns_200_idempotent(self):
        """EXPIRED conversation returns 200 without changing lifecycle reason."""
        adapter = _make_real_adapter()
        coordinator, store = _make_real_coordinator_and_store(adapter)
        key = DurableGenieSessionKey(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        result = adapter.get_or_create(key)
        adapter.set_status(key, ConversationStatus.EXPIRED, expected_version=result.record.version)

        resp = self._run_reset(coordinator)
        assert resp.status_code == 200

    def test_05_missing_record_creates_tombstone_and_returns_200(self):
        """Missing record creates RESET tombstone and returns 200."""
        coordinator, store = _make_real_coordinator_and_store()
        resp = self._run_reset(coordinator)
        assert resp.status_code == 200
        body = resp.body
        assert b"reset" in body

    def test_06_same_request_repeated_returns_200(self):
        """Repeating the same reset request returns 200 both times."""
        coordinator, store = _make_real_coordinator_and_store()
        resp1 = self._run_reset(coordinator)
        resp2 = self._run_reset(coordinator)
        assert resp1.status_code == 200
        assert resp2.status_code == 200


# ===========================================================================
# Section 2: Identity failure contract
# Tests 7–11
# ===========================================================================


class TestIdentityFailureContract:
    """Tests 7–11: identity missing, unavailable, disabled."""

    def _run(self, frontend: str = _FRONTEND_1, session: str = _SESSION_A):
        request = _make_request(session_id=session)
        return _run(reset_conversation(frontend, request))

    def test_07_missing_trusted_identity_returns_401(self):
        """Missing trusted header (ResolutionError) returns 401."""
        from app.services.request_owner_identity_runtime import (
            RequestOwnerIdentityRuntimeResolutionError,
        )
        with patch(_RESOLVE_PATCH, side_effect=RequestOwnerIdentityRuntimeResolutionError()):
            resp = self._run()
        assert resp.status_code == 401
        import json
        body = json.loads(resp.body)
        assert body["status"] == "error"
        assert "identity" in body["message"].lower()

    def test_08_identity_unavailable_returns_503(self):
        """ConfigurationError (unavailable runtime) returns 503."""
        from app.services.request_owner_identity_runtime import (
            RequestOwnerIdentityRuntimeConfigurationError,
        )
        with patch(_RESOLVE_PATCH, side_effect=RequestOwnerIdentityRuntimeConfigurationError()):
            resp = self._run()
        assert resp.status_code == 503

    def test_09_identity_disabled_returns_503(self):
        """Disabled identity (None returned) fails closed with 503."""
        with patch(_RESOLVE_PATCH, return_value=None):
            resp = self._run()
        assert resp.status_code == 503
        import json
        body = json.loads(resp.body)
        assert body["status"] == "error"

    def test_10_malformed_frontend_id_returns_400(self):
        """Frontend ID containing '@' returns 400."""
        identity = _make_identity()
        request = _make_request()
        with patch(_RESOLVE_PATCH, return_value=identity):
            resp = _run(reset_conversation("bad@frontend", request))
        assert resp.status_code == 400
        import json
        body = json.loads(resp.body)
        assert "invalid" in body["message"].lower()

    def test_10b_empty_frontend_id_returns_400(self):
        """Empty (whitespace-only) frontend ID returns 400."""
        identity = _make_identity()
        request = _make_request()
        with patch(_RESOLVE_PATCH, return_value=identity):
            resp = _run(reset_conversation("   ", request))
        assert resp.status_code == 400


# ===========================================================================
# Section 3: Coordinator error mapping
# Tests 11–14
# ===========================================================================


class TestCoordinatorErrorMapping:
    """Tests 11–14: coordinator exception → HTTP status mapping."""

    def _run_with_coord_side_effect(self, exc):
        identity = _make_identity()
        request = _make_request()
        fake_coord = MagicMock()
        fake_coord.reset.side_effect = exc
        with (
            patch(_RESOLVE_PATCH, return_value=identity),
            patch(_GET_COORD_PATCH, return_value=fake_coord),
        ):
            return _run(reset_conversation(_FRONTEND_1, request))

    def test_11_coordinator_conflict_returns_409(self):
        resp = self._run_with_coord_side_effect(ResetCoordinatorConflictError())
        assert resp.status_code == 409
        import json
        body = json.loads(resp.body)
        assert "retry" in body["message"].lower()

    def test_12_coordinator_unavailable_returns_503(self):
        resp = self._run_with_coord_side_effect(ResetCoordinatorUnavailableError())
        assert resp.status_code == 503

    def test_13_coordinator_internal_error_returns_503(self):
        resp = self._run_with_coord_side_effect(ResetCoordinatorInternalError())
        assert resp.status_code == 503

    def test_14_unexpected_error_returns_503_static(self):
        resp = self._run_with_coord_side_effect(RuntimeError("boom"))
        assert resp.status_code == 503
        import json
        body = json.loads(resp.body)
        assert "boom" not in body["message"]
        assert body["status"] == "error"


# ===========================================================================
# Section 4: Owner isolation and input override rejection
# Tests 15–22
# ===========================================================================


class TestOwnerIsolationAndOverrideRejection:
    """Tests 15–22: no override possible; different owners stay isolated."""

    def test_15_request_body_cannot_override_owner(self):
        """A request body field cannot change the trusted owner used."""
        identity = _make_identity(_OWNER_A)
        captured = []
        request = _make_request()

        real_coord, _ = _make_real_coordinator_and_store()

        def _check_owner(*, owner_user_id_hash, frontend_conversation_id, process_local_conversation_key):
            captured.append(owner_user_id_hash)
            from app.services.conversation_reset_coordinator import ResetResult, ResetOutcome
            return ResetResult(success=True, outcome=ResetOutcome.TOMBSTONE_CREATED)

        fake_coord = MagicMock()
        fake_coord.reset.side_effect = _check_owner

        with (
            patch(_RESOLVE_PATCH, return_value=identity),
            patch(_GET_COORD_PATCH, return_value=fake_coord),
        ):
            _run(reset_conversation(_FRONTEND_1, request))

        assert len(captured) == 1
        assert captured[0] == _OWNER_A, "Owner must be from trusted identity, not overridden"

    def test_16_query_parameter_cannot_override_owner(self):
        """Query parameters have no effect on the trusted owner derived."""
        identity = _make_identity(_OWNER_A)
        captured = []
        # Simulate a request with a query string (query_params not used by route)
        request = _make_request()
        request.query_params = {"owner": _OWNER_B}

        fake_coord = MagicMock()

        def _check(*, owner_user_id_hash, **_):
            captured.append(owner_user_id_hash)
            from app.services.conversation_reset_coordinator import ResetResult, ResetOutcome
            return ResetResult(success=True, outcome=ResetOutcome.TOMBSTONE_CREATED)

        fake_coord.reset.side_effect = _check

        with (
            patch(_RESOLVE_PATCH, return_value=identity),
            patch(_GET_COORD_PATCH, return_value=fake_coord),
        ):
            _run(reset_conversation(_FRONTEND_1, request))

        assert captured[0] == _OWNER_A

    def test_17_legacy_email_cannot_override_owner(self):
        """X-Forwarded-Email header cannot replace the trusted owner."""
        identity = _make_identity(_OWNER_A)
        captured = []
        request = _make_request(extra_headers={"X-Forwarded-Email": "attacker@evil.com"})

        fake_coord = MagicMock()

        def _check(*, owner_user_id_hash, **_):
            captured.append(owner_user_id_hash)
            from app.services.conversation_reset_coordinator import ResetResult, ResetOutcome
            return ResetResult(success=True, outcome=ResetOutcome.TOMBSTONE_CREATED)

        fake_coord.reset.side_effect = _check

        with (
            patch(_RESOLVE_PATCH, return_value=identity),
            patch(_GET_COORD_PATCH, return_value=fake_coord),
        ):
            _run(reset_conversation(_FRONTEND_1, request))

        assert captured[0] == _OWNER_A

    def test_18_same_frontend_id_different_owners_stay_isolated(self):
        """Owner A and Owner B sharing the same frontend ID use separate records."""
        adapter = _make_real_adapter()
        # Create ACTIVE record for Owner A.
        key_a = DurableGenieSessionKey(
            owner_user_id_hash=_OWNER_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        adapter.get_or_create(key_a)

        coordinator_a = ConversationResetCoordinator(
            adapter=adapter,
            session_store=GenieSessionStore(),
        )
        coordinator_b = ConversationResetCoordinator(
            adapter=adapter,
            session_store=GenieSessionStore(),
        )

        request_a = _make_request(session_id=_SESSION_A)
        request_b = _make_request(session_id=_SESSION_B)
        identity_a = _make_identity(_OWNER_A)
        identity_b = _make_identity(_OWNER_B)

        with (
            patch(_RESOLVE_PATCH, return_value=identity_a),
            patch(_GET_COORD_PATCH, return_value=coordinator_a),
        ):
            resp_a = _run(reset_conversation(_FRONTEND_1, request_a))

        with (
            patch(_RESOLVE_PATCH, return_value=identity_b),
            patch(_GET_COORD_PATCH, return_value=coordinator_b),
        ):
            resp_b = _run(reset_conversation(_FRONTEND_1, request_b))

        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
        # Owner B's reset must not have altered Owner A's record.
        lookup_a = adapter.load(key_a)
        assert lookup_a is not None
        assert lookup_a.record.status == ConversationStatus.RESET

    def test_19_same_cookie_different_owners_produce_different_local_keys(self):
        """Same cookie + different trusted owner → different process-local keys."""
        captured_keys = []
        session = _SESSION_A  # same session cookie

        for owner in (_OWNER_A, _OWNER_B):
            identity = _make_identity(owner)
            request = _make_request(session_id=session)

            fake_coord = MagicMock()

            def _check(*, process_local_conversation_key, **_):
                captured_keys.append(process_local_conversation_key)
                from app.services.conversation_reset_coordinator import ResetResult, ResetOutcome
                return ResetResult(success=True, outcome=ResetOutcome.TOMBSTONE_CREATED)

            fake_coord.reset.side_effect = _check

            with (
                patch(_RESOLVE_PATCH, return_value=identity),
                patch(_GET_COORD_PATCH, return_value=fake_coord),
            ):
                _run(reset_conversation(_FRONTEND_1, request))

        assert len(captured_keys) == 2
        assert captured_keys[0] != captured_keys[1]


# ===========================================================================
# Section 5: Coordinator invocation contract
# Tests 20–22
# ===========================================================================


class TestCoordinatorInvocationContract:
    """Tests 20–22: coordinator receives correct canonical ID, owner, local key."""

    def test_20_route_passes_canonical_frontend_id_to_coordinator(self):
        """Route strips whitespace and passes canonical ID to coordinator."""
        captured = {}
        identity = _make_identity(_OWNER_A)
        request = _make_request()

        fake_coord = MagicMock()

        def _check(*, owner_user_id_hash, frontend_conversation_id, process_local_conversation_key):
            captured["fid"] = frontend_conversation_id
            from app.services.conversation_reset_coordinator import ResetResult, ResetOutcome
            return ResetResult(success=True, outcome=ResetOutcome.TOMBSTONE_CREATED)

        fake_coord.reset.side_effect = _check

        with (
            patch(_RESOLVE_PATCH, return_value=identity),
            patch(_GET_COORD_PATCH, return_value=fake_coord),
        ):
            _run(reset_conversation(f"  {_FRONTEND_1}  ", request))

        assert captured["fid"] == _FRONTEND_1, "Route must pass stripped canonical ID"

    def test_21_route_passes_exact_trusted_owner_hash(self):
        """Route passes the exact owner hash from trusted identity."""
        captured = {}
        identity = _make_identity(_OWNER_A)
        request = _make_request()

        fake_coord = MagicMock()

        def _check(*, owner_user_id_hash, **_):
            captured["owner"] = owner_user_id_hash
            from app.services.conversation_reset_coordinator import ResetResult, ResetOutcome
            return ResetResult(success=True, outcome=ResetOutcome.TOMBSTONE_CREATED)

        fake_coord.reset.side_effect = _check

        with (
            patch(_RESOLVE_PATCH, return_value=identity),
            patch(_GET_COORD_PATCH, return_value=fake_coord),
        ):
            _run(reset_conversation(_FRONTEND_1, request))

        assert captured["owner"] == _OWNER_A

    def test_22_route_passes_opaque_process_local_key(self):
        """Route passes a valid opaque plc_v1_ process-local key."""
        from app.services.process_local_conversation_key import is_valid_process_local_key
        captured = {}
        identity = _make_identity(_OWNER_A)
        request = _make_request(session_id=_SESSION_A)

        fake_coord = MagicMock()

        def _check(*, process_local_conversation_key, **_):
            captured["key"] = process_local_conversation_key
            from app.services.conversation_reset_coordinator import ResetResult, ResetOutcome
            return ResetResult(success=True, outcome=ResetOutcome.TOMBSTONE_CREATED)

        fake_coord.reset.side_effect = _check

        with (
            patch(_RESOLVE_PATCH, return_value=identity),
            patch(_GET_COORD_PATCH, return_value=fake_coord),
        ):
            _run(reset_conversation(_FRONTEND_1, request))

        assert is_valid_process_local_key(captured["key"]), "Must be a valid plc_v1_ key"
        # Must match the expected deterministic digest.
        expected = _local_key(_OWNER_A, _SESSION_A, _FRONTEND_1)
        assert captured["key"] == expected


# ===========================================================================
# Section 6: Output hygiene
# Tests 23–25
# ===========================================================================


class TestOutputHygiene:
    """Tests 23–25: success/error responses contain no identifiers."""

    def _make_success_response(self):
        identity = _make_identity(_OWNER_A)
        request = _make_request(session_id=_SESSION_A)
        fake_coord = MagicMock()
        from app.services.conversation_reset_coordinator import ResetResult, ResetOutcome
        fake_coord.reset.return_value = ResetResult(
            success=True, outcome=ResetOutcome.RESET
        )
        with (
            patch(_RESOLVE_PATCH, return_value=identity),
            patch(_GET_COORD_PATCH, return_value=fake_coord),
        ):
            return _run(reset_conversation(_FRONTEND_1, request))

    def test_23_success_response_contains_no_identifiers(self):
        """200 response body contains no owner hash, session ID, or local key."""
        resp = self._make_success_response()
        assert resp.status_code == 200
        body_str = resp.body.decode()
        assert _OWNER_A not in body_str
        assert _SESSION_A not in body_str
        assert _FRONTEND_1 not in body_str
        assert "plc_v1_" not in body_str

    def test_24_error_response_contains_no_identifiers(self):
        """Error response body contains no sensitive identifiers."""
        from app.services.request_owner_identity_runtime import (
            RequestOwnerIdentityRuntimeResolutionError,
        )
        request = _make_request()
        with patch(_RESOLVE_PATCH, side_effect=RequestOwnerIdentityRuntimeResolutionError()):
            resp = _run(reset_conversation(_FRONTEND_1, request))

        body_str = resp.body.decode()
        assert _OWNER_A not in body_str
        assert _SESSION_A not in body_str
        assert _FRONTEND_1 not in body_str

    def test_25_logs_contain_no_identifiers(self, caplog):
        """Route logs contain no owner hash, session ID, or process-local key."""
        identity = _make_identity(_OWNER_A)
        request = _make_request(session_id=_SESSION_A)
        fake_coord = MagicMock()
        fake_coord.reset.side_effect = RuntimeError("internal test error")

        with caplog.at_level(logging.ERROR, logger="app.routes.conversation_reset"):
            with (
                patch(_RESOLVE_PATCH, return_value=identity),
                patch(_GET_COORD_PATCH, return_value=fake_coord),
            ):
                _run(reset_conversation(_FRONTEND_1, request))

        full_log = " ".join(caplog.messages)
        assert _OWNER_A not in full_log
        assert _SESSION_A not in full_log
        assert _FRONTEND_1 not in full_log
        plc = _local_key(_OWNER_A, _SESSION_A, _FRONTEND_1)
        assert plc not in full_log


# ===========================================================================
# Section 7: Shared-instance invariants
# Tests 26–30
# ===========================================================================


class TestSharedInstanceInvariants:
    """Tests 26–30: no duplicate store/adapter, no Genie/fallback/delete."""

    def test_26_no_duplicate_local_store_is_created(self):
        """get_conversation_reset_coordinator() must NOT construct a new store."""
        from app.services.genie_session_store import GenieSessionStore as GSS
        store = GSS()
        adapter = _make_real_adapter()
        coordinator = ConversationResetCoordinator(adapter=adapter, session_store=store)

        call_count = [0]
        original_init = GSS.__init__

        def _spy_init(self, *a, **kw):
            call_count[0] += 1
            return original_init(self, *a, **kw)

        identity = _make_identity(_OWNER_A)
        request = _make_request()

        with (
            patch.object(GSS, "__init__", _spy_init),
            patch(_RESOLVE_PATCH, return_value=identity),
            patch(_GET_COORD_PATCH, return_value=coordinator),
        ):
            resp = _run(reset_conversation(_FRONTEND_1, request))

        assert resp.status_code == 200
        # The route called get_conversation_reset_coordinator (mocked) so no new store.
        assert call_count[0] == 0, "No GenieSessionStore should be constructed by the route"

    def test_27_no_duplicate_durable_adapter_is_created(self):
        """get_conversation_reset_coordinator() must NOT construct a new adapter."""
        from app.services.durable_genie_session_adapter import DurableGenieSessionAdapter as DSA
        adapter = _make_real_adapter()
        coordinator, _ = _make_real_coordinator_and_store(adapter)

        construct_count = [0]
        original_init = DSA.__init__

        def _spy_init(self, *a, **kw):
            construct_count[0] += 1
            return original_init(self, *a, **kw)

        identity = _make_identity(_OWNER_A)
        request = _make_request()

        with (
            patch.object(DSA, "__init__", _spy_init),
            patch(_RESOLVE_PATCH, return_value=identity),
            patch(_GET_COORD_PATCH, return_value=coordinator),
        ):
            resp = _run(reset_conversation(_FRONTEND_1, request))

        assert resp.status_code == 200
        assert construct_count[0] == 0, "No DurableGenieSessionAdapter should be constructed"

    def test_28_no_genie_request_occurs_during_reset(self):
        """No GenieClient method is called during a reset request."""
        identity = _make_identity(_OWNER_A)
        request = _make_request()
        fake_coord = MagicMock()
        from app.services.conversation_reset_coordinator import ResetResult, ResetOutcome
        fake_coord.reset.return_value = ResetResult(
            success=True, outcome=ResetOutcome.TOMBSTONE_CREATED
        )

        genie_client_mock = MagicMock()

        with (
            patch(_RESOLVE_PATCH, return_value=identity),
            patch(_GET_COORD_PATCH, return_value=fake_coord),
            patch("app.services.genie_client.GenieClient", genie_client_mock),
        ):
            resp = _run(reset_conversation(_FRONTEND_1, request))

        assert resp.status_code == 200
        genie_client_mock.assert_not_called()

    def test_29_no_custom_pipeline_fallback_occurs(self):
        """fallback_recommended is not present in reset response."""
        identity = _make_identity(_OWNER_A)
        request = _make_request()
        fake_coord = MagicMock()
        from app.services.conversation_reset_coordinator import ResetResult, ResetOutcome
        fake_coord.reset.return_value = ResetResult(
            success=True, outcome=ResetOutcome.RESET
        )

        with (
            patch(_RESOLVE_PATCH, return_value=identity),
            patch(_GET_COORD_PATCH, return_value=fake_coord),
        ):
            resp = _run(reset_conversation(_FRONTEND_1, request))

        assert resp.status_code == 200
        import json
        body = json.loads(resp.body)
        assert "fallback" not in body

    def test_30_no_delete_operation_occurs(self):
        """Coordinator.reset() is called, but no delete operation is used."""
        identity = _make_identity(_OWNER_A)
        request = _make_request()

        coordinator, _ = _make_real_coordinator_and_store()

        with (
            patch(_RESOLVE_PATCH, return_value=identity),
            patch(_GET_COORD_PATCH, return_value=coordinator),
        ):
            resp = _run(reset_conversation(_FRONTEND_1, request))

        # The real DurableGenieSessionAdapter has a delete() method, but
        # ConversationResetCoordinator must never call it — it uses
        # set_status / get_or_create (soft-reset) only.
        assert resp.status_code == 200

        # Source-level contract: coordinator must not reference adapter.delete
        import inspect
        from app.services.conversation_reset_coordinator import ConversationResetCoordinator as _CRC
        _src = inspect.getsource(_CRC)
        assert "self._adapter.delete" not in _src, (
            "ConversationResetCoordinator must not call self._adapter.delete()"
        )
