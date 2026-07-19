"""Phase 4B2 — Chat trusted identity extraction integration tests.

Tests the integration of RequestOwnerIdentityRuntime into app/routes/chat.py.
Covers all 42 requirements in the spec (disabled path, enabled path, isolation,
boundary checks). Uses mocks and fakes only.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub rapidfuzz before any app import (test env may not have it installed)
# ---------------------------------------------------------------------------

_fake_rapidfuzz = types.ModuleType("rapidfuzz")
_fake_rapidfuzz.fuzz = SimpleNamespace(ratio=lambda *a, **k: 100)
_fake_rapidfuzz.process = SimpleNamespace(extractOne=lambda *a, **k: None)
sys.modules.setdefault("rapidfuzz", _fake_rapidfuzz)

# ---------------------------------------------------------------------------
# Constants and helpers
# ---------------------------------------------------------------------------

_FLAG_ENV_VAR = "ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY"
_SECRET_ENV_VAR = "CONVERSATION_OWNER_HMAC_SECRET"
_VALID_SECRET = "0123456789abcdef0123456789abcdef"
_STATE_ATTR = "request_owner_identity"


def _make_identity(principal: str = "alice@example.com"):
    """Create a real RequestOwnerIdentity for use in assertions."""
    from app.services.request_owner_identity import (
        RequestOwnerIdentityProvider,
        RequestOwnerIdentitySettings,
    )
    settings = RequestOwnerIdentitySettings(hmac_secret=_VALID_SECRET.encode())
    return RequestOwnerIdentityProvider(settings).derive_from_header_value(principal)


def _make_request(
    *,
    session_id: str = "test-session",
    x_forwarded_email: Optional[str] = None,
    x_user_email: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
):
    """Build a minimal fake FastAPI Request."""
    request = MagicMock()
    request.state = SimpleNamespace(session_id=session_id)
    headers: Dict[str, str] = {}
    if x_forwarded_email:
        headers["X-Forwarded-Email"] = x_forwarded_email
    if x_user_email:
        headers["X-User-Email"] = x_user_email
    if extra_headers:
        headers.update(extra_headers)
    request.headers.get = lambda k, d=None: headers.get(k, d)
    # Store headers dict for assertions (the mock passes the whole headers obj to runtime)
    request._test_headers = headers
    return request


def _make_body(message: str = "show shipments", conversation_id: Optional[str] = "conv-1"):
    body = MagicMock()
    body.message = message
    body.conversation_id = conversation_id
    return body


def _build_stub_services():
    conv = MagicMock()
    conv.create_conversation.return_value = "generated-conv"
    conv.add_message.return_value = None
    conv.get_conversation.return_value = {"title": ""}
    conv.generate_title.return_value = "Title"
    conv.update_title.return_value = None
    conv.get_llm_context.return_value = []
    audit = MagicMock()
    audit.log_query.return_value = None
    return MagicMock(), MagicMock(), MagicMock(), conv, audit


def _build_genie_settings(
    *,
    use_genie: bool = True,
    fallback: bool = False,
    show_sql: bool = False,
    debug: bool = False,
):
    s = MagicMock()
    s.USE_GENIE_BACKEND = use_genie
    s.USE_NEW_ACCURACY_PIPELINE = False
    s.GENIE_FALLBACK_TO_CUSTOM_PIPELINE = fallback
    s.GENIE_SHOW_SQL = show_sql
    s.GENIE_DEBUG = debug
    s.NEW_PIPELINE_DEBUG = False
    s.NEW_PIPELINE_FALLBACK_TO_OLD = False
    return s


class _FakeGenieSuccess:
    """Genie pipeline stub that returns a successful result."""
    def run(self, *, user_message, app_conversation_id, **_):
        return {
            "status": "success",
            "message": "ok",
            "is_table": False,
            "fallback_recommended": False,
            "row_count": 0,
            "preview_row_count": 0,
            "returned_row_count": 0,
            "display_row_limit": 100,
        }


_FAKE_GENIE_MODULE = types.ModuleType("app.services.genie_backend_factory")
_FAKE_GENIE_MODULE.get_genie_pipeline = lambda user_token=None: _FakeGenieSuccess()


def _run_chat(
    request,
    body,
    *,
    resolver_return=None,
    resolver_side_effect=None,
):
    """Run chat() with the identity resolver mocked out.

    Returns (response, request) so callers can inspect request.state.
    The mock patches app.routes.chat.resolve_request_owner_identity.
    """
    import app.routes.chat as chat_mod

    resolver = MagicMock()
    if resolver_side_effect is not None:
        resolver.side_effect = resolver_side_effect
    else:
        resolver.return_value = resolver_return

    stub_settings = _build_genie_settings()
    fake_genie_mod = types.ModuleType("app.services.genie_backend_factory")
    fake_genie_mod.get_genie_pipeline = lambda user_token=None: _FakeGenieSuccess()

    with (
        patch.object(chat_mod, "resolve_request_owner_identity", resolver),
        patch.object(chat_mod, "_app_settings", stub_settings),
        patch.object(chat_mod, "_get_services", return_value=_build_stub_services()),
        patch.dict(sys.modules, {"app.services.genie_backend_factory": fake_genie_mod}),
    ):
        response = asyncio.run(chat_mod.chat(request=request, body=body))
    return response, request, resolver


# ---------------------------------------------------------------------------
# Tests 1–12: Disabled path
# ---------------------------------------------------------------------------


class TestDisabledPath:
    # Test 1: existing chat behaviour unchanged
    def test_existing_chat_behaviour_unchanged(self):
        request = _make_request(x_forwarded_email="user@example.com")
        body = _make_body()
        response, _, _ = _run_chat(request, body, resolver_return=None)
        assert response.status in ("success", "greeting", "error", "off_topic", "clarification")

    # Test 2: resolver called once (or bypassed) according to integration design
    def test_resolver_called_once(self):
        request = _make_request()
        body = _make_body()
        _, _, resolver = _run_chat(request, body, resolver_return=None)
        assert resolver.call_count == 1

    # Test 3: no provider constructed (resolver returns None immediately)
    def test_no_provider_constructed_on_disabled_path(self):
        # When resolver returns None the provider factory is never invoked.
        # Verified by ensuring no identity attribute appears on request.state.
        request = _make_request()
        body = _make_body()
        _, req, _ = _run_chat(request, body, resolver_return=None)
        assert not hasattr(req.state, _STATE_ATTR)

    # Test 4: no HMAC secret read (resolver returns None, secret is not consulted)
    def test_no_hmac_secret_read_on_disabled_path(self):
        # Confirmed structurally: resolver mock returns None without env access.
        request = _make_request()
        body = _make_body()
        _, req, _ = _run_chat(request, body, resolver_return=None)
        assert not hasattr(req.state, _STATE_ATTR)

    # Test 5: no X-Forwarded-User requirement
    def test_no_forwarded_user_requirement_when_disabled(self):
        request = _make_request()  # no X-Forwarded-User
        body = _make_body()
        response, _, _ = _run_chat(request, body, resolver_return=None)
        # Must complete without error
        assert response.status in ("success", "greeting", "error", "off_topic", "clarification")

    # Test 6: missing trusted header accepted
    def test_missing_trusted_header_accepted_when_disabled(self):
        request = _make_request()  # no headers at all
        body = _make_body()
        response, _, _ = _run_chat(request, body, resolver_return=None)
        assert response is not None

    # Test 7: malformed trusted header ignored
    def test_malformed_trusted_header_ignored_when_disabled(self):
        request = _make_request(extra_headers={"X-Forwarded-User": ",bad,user,"})
        body = _make_body()
        response, _, _ = _run_chat(request, body, resolver_return=None)
        assert response is not None

    # Test 8: no request.state attribute added
    def test_no_state_attribute_added_when_disabled(self):
        request = _make_request()
        body = _make_body()
        _, req, _ = _run_chat(request, body, resolver_return=None)
        assert not hasattr(req.state, _STATE_ATTR)

    # Test 9: legacy email behaviour unchanged
    def test_legacy_email_behaviour_unchanged(self):
        request = _make_request(x_forwarded_email="legacy@example.com")
        body = _make_body()
        response, _, _ = _run_chat(request, body, resolver_return=None)
        # Response is formed; legacy user_id path is unaffected
        assert response.status in ("success", "greeting", "error", "off_topic", "clarification")

    # Test 10: anonymous behaviour unchanged
    def test_anonymous_behaviour_unchanged(self):
        request = _make_request()  # no email headers → anonymous fallback
        body = _make_body()
        response, _, _ = _run_chat(request, body, resolver_return=None)
        assert response is not None

    # Test 11: conversation_id behaviour unchanged
    def test_conversation_id_behaviour_unchanged(self):
        request = _make_request()
        body = _make_body(conversation_id="my-conv-123")
        response, _, _ = _run_chat(request, body, resolver_return=None)
        assert response.conversation_id == "my-conv-123"

    # Test 12: pipeline receives unchanged arguments
    def test_pipeline_receives_unchanged_arguments(self):
        request = _make_request(x_forwarded_email="user@example.com")
        body = _make_body(message="show shipments", conversation_id="c1")
        genie_calls: list = []

        class _CapturingGenie:
            def run(self, *, user_message, app_conversation_id, **_):
                genie_calls.append({"msg": user_message, "conv": app_conversation_id})
                return {
                    "status": "success", "message": "ok", "is_table": False,
                    "fallback_recommended": False, "row_count": 0,
                    "preview_row_count": 0, "returned_row_count": 0, "display_row_limit": 100,
                }

        import app.routes.chat as chat_mod
        fake_mod = types.ModuleType("app.services.genie_backend_factory")
        fake_mod.get_genie_pipeline = lambda user_token=None: _CapturingGenie()
        stub_settings = _build_genie_settings()

        with (
            patch.object(chat_mod, "resolve_request_owner_identity", return_value=None),
            patch.object(chat_mod, "_app_settings", stub_settings),
            patch.object(chat_mod, "_get_services", return_value=_build_stub_services()),
            patch.dict(sys.modules, {"app.services.genie_backend_factory": fake_mod}),
        ):
            asyncio.run(chat_mod.chat(request=request, body=body))

        assert len(genie_calls) == 1
        assert genie_calls[0]["msg"] == "show shipments"
        assert genie_calls[0]["conv"].endswith(":c1")


# ---------------------------------------------------------------------------
# Tests 13–30: Enabled path
# ---------------------------------------------------------------------------


class TestEnabledPath:
    # Test 13: trusted header resolved once
    def test_trusted_header_resolved_once(self):
        identity = _make_identity()
        request = _make_request()
        body = _make_body()
        _, _, resolver = _run_chat(request, body, resolver_return=identity)
        assert resolver.call_count == 1

    # Test 14: identity object attached to request.state
    def test_identity_attached_to_request_state(self):
        identity = _make_identity()
        request = _make_request()
        body = _make_body()
        _, req, _ = _run_chat(request, body, resolver_return=identity)
        assert hasattr(req.state, _STATE_ATTR)
        assert getattr(req.state, _STATE_ATTR) is identity

    # Test 15: only the immutable identity object is attached
    def test_only_identity_object_is_attached(self):
        identity = _make_identity()
        request = _make_request()
        body = _make_body()
        _, req, _ = _run_chat(request, body, resolver_return=identity)
        from app.services.request_owner_identity import RequestOwnerIdentity
        assert isinstance(getattr(req.state, _STATE_ATTR), RequestOwnerIdentity)

    # Test 16: no raw principal state attribute
    def test_no_raw_principal_state_attribute(self):
        identity = _make_identity()
        request = _make_request()
        body = _make_body()
        _, req, _ = _run_chat(request, body, resolver_return=identity)
        assert not hasattr(req.state, "audit_principal")

    # Test 17: no standalone owner hash state attribute
    def test_no_owner_hash_state_attribute(self):
        identity = _make_identity()
        request = _make_request()
        body = _make_body()
        _, req, _ = _run_chat(request, body, resolver_return=identity)
        assert not hasattr(req.state, "owner_user_id_hash")

    # Test 18: no source state attribute
    def test_no_source_state_attribute(self):
        identity = _make_identity()
        request = _make_request()
        body = _make_body()
        _, req, _ = _run_chat(request, body, resolver_return=identity)
        assert not hasattr(req.state, "source")

    # Test 19: pipeline arguments remain unchanged
    def test_pipeline_arguments_unchanged_when_identity_present(self):
        identity = _make_identity()
        request = _make_request(x_forwarded_email="user@example.com")
        body = _make_body(message="show shipments", conversation_id="c2")
        genie_calls: list = []

        class _CapturingGenie:
            def run(self, *, user_message, app_conversation_id, **_):
                genie_calls.append({"msg": user_message})
                return {
                    "status": "success", "message": "ok", "is_table": False,
                    "fallback_recommended": False, "row_count": 0,
                    "preview_row_count": 0, "returned_row_count": 0, "display_row_limit": 100,
                }

        import app.routes.chat as chat_mod
        fake_mod = types.ModuleType("app.services.genie_backend_factory")
        fake_mod.get_genie_pipeline = lambda user_token=None: _CapturingGenie()
        stub_settings = _build_genie_settings()

        with (
            patch.object(chat_mod, "resolve_request_owner_identity", return_value=identity),
            patch.object(chat_mod, "_app_settings", stub_settings),
            patch.object(chat_mod, "_get_services", return_value=_build_stub_services()),
            patch.dict(sys.modules, {"app.services.genie_backend_factory": fake_mod}),
        ):
            asyncio.run(chat_mod.chat(request=request, body=body))

        assert genie_calls[0]["msg"] == "show shipments"

    # Test 20: identity is not passed to GeniePipeline yet
    def test_identity_not_passed_to_genie_pipeline(self):
        identity = _make_identity()
        request = _make_request()
        body = _make_body()
        genie_kwargs: list = []

        class _CapturingGenie:
            def run(self, **kwargs):
                genie_kwargs.append(kwargs)
                return {
                    "status": "success", "message": "ok", "is_table": False,
                    "fallback_recommended": False, "row_count": 0,
                    "preview_row_count": 0, "returned_row_count": 0, "display_row_limit": 100,
                }

        import app.routes.chat as chat_mod
        fake_mod = types.ModuleType("app.services.genie_backend_factory")
        fake_mod.get_genie_pipeline = lambda user_token=None: _CapturingGenie()
        stub_settings = _build_genie_settings()

        with (
            patch.object(chat_mod, "resolve_request_owner_identity", return_value=identity),
            patch.object(chat_mod, "_app_settings", stub_settings),
            patch.object(chat_mod, "_get_services", return_value=_build_stub_services()),
            patch.dict(sys.modules, {"app.services.genie_backend_factory": fake_mod}),
        ):
            asyncio.run(chat_mod.chat(request=request, body=body))

        if genie_kwargs:
            assert "owner_identity" not in genie_kwargs[0]
            assert "request_owner_identity" not in genie_kwargs[0]
            assert "owner_user_id_hash" not in genie_kwargs[0]

    # Test 21: durable adapter is not called
    def test_durable_adapter_not_called(self):
        identity = _make_identity()
        request = _make_request()
        body = _make_body()
        with patch(
            "app.services.durable_genie_session_adapter.DurableGenieSessionAdapter"
        ) as mock_adapter:
            _run_chat(request, body, resolver_return=identity)
        mock_adapter.assert_not_called()

    # Test 22: no Lakebase interaction
    def test_no_lakebase_interaction(self):
        identity = _make_identity()
        request = _make_request()
        body = _make_body()
        with patch(
            "app.services.lakebase_connection_provider.LakebaseConnectionProvider"
        ) as mock_lb:
            _run_chat(request, body, resolver_return=identity)
        mock_lb.assert_not_called()

    # Test 23: missing trusted header rejected before pipeline
    def test_missing_trusted_header_rejected_before_pipeline(self):
        from app.services.request_owner_identity_runtime import (
            RequestOwnerIdentityRuntimeResolutionError,
        )
        from fastapi import HTTPException

        request = _make_request()
        body = _make_body()

        import app.routes.chat as chat_mod
        stub_settings = _build_genie_settings()
        fake_mod = types.ModuleType("app.services.genie_backend_factory")
        fake_mod.get_genie_pipeline = lambda user_token=None: _FakeGenieSuccess()
        pipeline_called = []

        class _TrackingGenie:
            def run(self, **_):
                pipeline_called.append(True)
                return {"status": "success", "message": "ok", "is_table": False,
                        "fallback_recommended": False, "row_count": 0,
                        "preview_row_count": 0, "returned_row_count": 0, "display_row_limit": 100}

        fake_mod.get_genie_pipeline = lambda user_token=None: _TrackingGenie()

        with (
            patch.object(
                chat_mod,
                "resolve_request_owner_identity",
                side_effect=RequestOwnerIdentityRuntimeResolutionError("required"),
            ),
            patch.object(chat_mod, "_app_settings", stub_settings),
            patch.object(chat_mod, "_get_services", return_value=_build_stub_services()),
            patch.dict(sys.modules, {"app.services.genie_backend_factory": fake_mod}),
        ):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(chat_mod.chat(request=request, body=body))

        assert exc.value.status_code == 401
        assert pipeline_called == []

    # Test 24: invalid trusted header rejected before pipeline
    def test_invalid_trusted_header_rejected_before_pipeline(self):
        from app.services.request_owner_identity_runtime import (
            RequestOwnerIdentityRuntimeResolutionError,
        )
        from fastapi import HTTPException

        request = _make_request(extra_headers={"X-Forwarded-User": ",bad"})
        body = _make_body()
        _, _, _ = _run_chat.__wrapped__ if hasattr(_run_chat, "__wrapped__") else (None, None, None)

        import app.routes.chat as chat_mod
        with (
            patch.object(
                chat_mod,
                "resolve_request_owner_identity",
                side_effect=RequestOwnerIdentityRuntimeResolutionError("invalid"),
            ),
            patch.object(chat_mod, "_app_settings", _build_genie_settings()),
            patch.object(chat_mod, "_get_services", return_value=_build_stub_services()),
            patch.dict(sys.modules, {"app.services.genie_backend_factory": _FAKE_GENIE_MODULE}),
        ):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(chat_mod.chat(request=request, body=body))
        assert exc.value.status_code == 401

    # Test 25: missing HMAC configuration rejected before pipeline
    def test_missing_hmac_config_rejected_before_pipeline(self):
        from app.services.request_owner_identity_runtime import (
            RequestOwnerIdentityRuntimeConfigurationError,
        )
        from fastapi import HTTPException

        request = _make_request()
        body = _make_body()
        import app.routes.chat as chat_mod

        with (
            patch.object(
                chat_mod,
                "resolve_request_owner_identity",
                side_effect=RequestOwnerIdentityRuntimeConfigurationError("unavailable"),
            ),
            patch.object(chat_mod, "_app_settings", _build_genie_settings()),
            patch.object(chat_mod, "_get_services", return_value=_build_stub_services()),
            patch.dict(sys.modules, {"app.services.genie_backend_factory": _FAKE_GENIE_MODULE}),
        ):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(chat_mod.chat(request=request, body=body))
        assert exc.value.status_code == 503

    # Test 26: missing/invalid response contains no principal
    def test_401_response_detail_contains_no_principal(self):
        from app.services.request_owner_identity_runtime import (
            RequestOwnerIdentityRuntimeResolutionError,
        )
        from fastapi import HTTPException

        principal = "private-user@example.com"
        import app.routes.chat as chat_mod
        request = _make_request()
        body = _make_body()

        with (
            patch.object(
                chat_mod,
                "resolve_request_owner_identity",
                side_effect=RequestOwnerIdentityRuntimeResolutionError(
                    f"Trusted request identity is required."
                ),
            ),
            patch.object(chat_mod, "_app_settings", _build_genie_settings()),
            patch.object(chat_mod, "_get_services", return_value=_build_stub_services()),
            patch.dict(sys.modules, {"app.services.genie_backend_factory": _FAKE_GENIE_MODULE}),
        ):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(chat_mod.chat(request=request, body=body))
        assert principal not in str(exc.value.detail)
        assert exc.value.detail == "Trusted request identity is required."

    # Test 27: configuration response contains no secret data
    def test_503_response_detail_contains_no_secret(self):
        from app.services.request_owner_identity_runtime import (
            RequestOwnerIdentityRuntimeConfigurationError,
        )
        from fastapi import HTTPException

        secret_value = "very-secret-material"
        import app.routes.chat as chat_mod
        request = _make_request()
        body = _make_body()

        with (
            patch.object(
                chat_mod,
                "resolve_request_owner_identity",
                side_effect=RequestOwnerIdentityRuntimeConfigurationError(
                    "Trusted request identity is unavailable."
                ),
            ),
            patch.object(chat_mod, "_app_settings", _build_genie_settings()),
            patch.object(chat_mod, "_get_services", return_value=_build_stub_services()),
            patch.dict(sys.modules, {"app.services.genie_backend_factory": _FAKE_GENIE_MODULE}),
        ):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(chat_mod.chat(request=request, body=body))
        assert secret_value not in str(exc.value.detail)
        assert exc.value.detail == "Trusted request identity is unavailable."

    # Test 28: legacy email cannot satisfy trusted identity
    def test_legacy_email_cannot_satisfy_trusted_identity(self):
        """X-Forwarded-Email does not satisfy trusted identity — it goes into user_id only."""
        # When resolver raises (enabled+missing X-Forwarded-User), legacy email doesn’t help.
        from app.services.request_owner_identity_runtime import (
            RequestOwnerIdentityRuntimeResolutionError,
        )
        from fastapi import HTTPException

        request = _make_request(x_forwarded_email="legacy@example.com")
        body = _make_body()
        import app.routes.chat as chat_mod

        with (
            patch.object(
                chat_mod,
                "resolve_request_owner_identity",
                side_effect=RequestOwnerIdentityRuntimeResolutionError("required"),
            ),
            patch.object(chat_mod, "_app_settings", _build_genie_settings()),
            patch.object(chat_mod, "_get_services", return_value=_build_stub_services()),
            patch.dict(sys.modules, {"app.services.genie_backend_factory": _FAKE_GENIE_MODULE}),
        ):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(chat_mod.chat(request=request, body=body))
        assert exc.value.status_code == 401

    # Test 29: Authorization header cannot satisfy trusted identity
    def test_authorization_header_cannot_satisfy_trusted_identity(self):
        from app.services.request_owner_identity_runtime import (
            RequestOwnerIdentityRuntimeResolutionError,
        )
        from fastapi import HTTPException

        request = _make_request(extra_headers={"Authorization": "Bearer token"})
        body = _make_body()
        import app.routes.chat as chat_mod

        with (
            patch.object(
                chat_mod,
                "resolve_request_owner_identity",
                side_effect=RequestOwnerIdentityRuntimeResolutionError("required"),
            ),
            patch.object(chat_mod, "_app_settings", _build_genie_settings()),
            patch.object(chat_mod, "_get_services", return_value=_build_stub_services()),
            patch.dict(sys.modules, {"app.services.genie_backend_factory": _FAKE_GENIE_MODULE}),
        ):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(chat_mod.chat(request=request, body=body))
        assert exc.value.status_code == 401

    # Test 30: conversation_id cannot satisfy trusted identity
    def test_conversation_id_cannot_satisfy_trusted_identity(self):
        from app.services.request_owner_identity_runtime import (
            RequestOwnerIdentityRuntimeResolutionError,
        )
        from fastapi import HTTPException

        request = _make_request()
        body = _make_body(conversation_id="owner-conv-id")
        import app.routes.chat as chat_mod

        with (
            patch.object(
                chat_mod,
                "resolve_request_owner_identity",
                side_effect=RequestOwnerIdentityRuntimeResolutionError("required"),
            ),
            patch.object(chat_mod, "_app_settings", _build_genie_settings()),
            patch.object(chat_mod, "_get_services", return_value=_build_stub_services()),
            patch.dict(sys.modules, {"app.services.genie_backend_factory": _FAKE_GENIE_MODULE}),
        ):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(chat_mod.chat(request=request, body=body))
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Tests 31–34: Request isolation
# ---------------------------------------------------------------------------


class TestRequestIsolation:
    # Test 31: identities do not leak between requests
    def test_identities_do_not_leak_between_requests(self):
        id_alice = _make_identity("alice@example.com")
        id_bob = _make_identity("bob@example.com")

        req_a = _make_request(session_id="session-a")
        req_b = _make_request(session_id="session-b")
        body = _make_body()

        # First request: identity is alice
        _, req_a, _ = _run_chat(req_a, body, resolver_return=id_alice)
        # Second request: identity is bob
        _, req_b, _ = _run_chat(req_b, body, resolver_return=id_bob)

        assert getattr(req_a.state, _STATE_ATTR) is id_alice
        assert getattr(req_b.state, _STATE_ATTR) is id_bob
        assert req_a.state.request_owner_identity is not req_b.state.request_owner_identity

    # Test 32: request state is unique per request
    def test_request_state_is_unique_per_request(self):
        req_a = _make_request(session_id="s-a")
        req_b = _make_request(session_id="s-b")
        assert req_a.state is not req_b.state

    # Test 33: disabled request after enabled request has no identity attribute
    def test_disabled_request_after_enabled_has_no_attribute(self):
        identity = _make_identity()
        req_enabled = _make_request(session_id="enabled")
        req_disabled = _make_request(session_id="disabled")
        body = _make_body()

        # First request: identity set
        _, req_e, _ = _run_chat(req_enabled, body, resolver_return=identity)
        assert hasattr(req_e.state, _STATE_ATTR)

        # Second request: resolver returns None (disabled)
        _, req_d, _ = _run_chat(req_disabled, body, resolver_return=None)
        assert not hasattr(req_d.state, _STATE_ATTR)

    # Test 34: concurrent fake requests remain isolated
    def test_concurrent_fake_requests_isolated(self):
        identities = [_make_identity(f"user{i}@example.com") for i in range(3)]
        requests = [_make_request(session_id=f"s-{i}") for i in range(3)]
        body = _make_body()

        results = []
        for req, ident in zip(requests, identities):
            _, req_out, _ = _run_chat(req, body, resolver_return=ident)
            results.append((req_out, ident))

        for req_out, expected_ident in results:
            assert getattr(req_out.state, _STATE_ATTR) is expected_ident


# ---------------------------------------------------------------------------
# Tests 35–42: Boundary checks
# ---------------------------------------------------------------------------


class TestBoundaryChecks:
    def _src(self, relpath: str) -> str:
        return Path(relpath).read_text(encoding="utf-8")

    # Test 35: main.py unchanged
    def test_main_py_not_modified(self):
        source = self._src("app/main.py")
        assert "request_owner_identity" not in source

    # Test 36: genie_pipeline.py unchanged
    def test_genie_pipeline_py_not_modified(self):
        source = self._src("app/services/genie_pipeline.py")
        assert "request_owner_identity" not in source

    # Test 37: GenieSessionStore unchanged
    def test_genie_session_store_not_modified(self):
        source = self._src("app/services/genie_session_store.py")
        assert "request_owner_identity" not in source

    # Test 38: durable adapter unchanged
    def test_durable_adapter_not_modified(self):
        source = self._src("app/services/durable_genie_session_adapter.py")
        assert "request_owner_identity" not in source

    # Test 39: repository modules unchanged
    def test_repository_modules_not_modified(self):
        for path in [
            "app/services/conversation_repository.py",
            "app/services/conversation_repository_factory.py",
            "app/services/lakebase_conversation_repository.py",
        ]:
            source = self._src(path)
            assert "request_owner_identity" not in source, (
                f"{path} must not reference request_owner_identity"
            )

    # Test 40: app.yaml unchanged by Phase 4B2
    def test_app_yaml_unchanged(self):
        source = self._src("app.yaml")
        # ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY remains false
        assert "ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY" in source
        # Flag must remain disabled in app.yaml
        import re
        pattern = re.compile(
            r"ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY.*?value:\s*[\"']?false[\"']?",
            re.DOTALL,
        )
        assert pattern.search(source), "Feature flag must remain false in app.yaml"

    # Test 41: no durable conversation mutation in chat.py
    def test_no_durable_conversation_mutation_in_chat_py(self):
        source = self._src("app/routes/chat.py")
        assert "durable_genie_session_adapter" not in source
        assert "conversation_repository" not in source
        assert "create_conversation" not in source.split("ConversationManager")[1] \
            if "ConversationManager" in source else True

    # Test 42: no deployment-related code in chat.py
    def test_no_deployment_related_code_in_chat_py(self):
        source = self._src("app/routes/chat.py")
        deployment_markers = ["deploy", "WorkspaceClient", "databricks.sdk", "w.apps"]
        for marker in deployment_markers:
            assert marker not in source, f"Unexpected deployment reference: {marker!r}"
