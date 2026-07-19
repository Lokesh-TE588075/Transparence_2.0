"""Phase 4C2A — Chat route durable lookup key plumbing tests.

Behavioural tests using the same direct async chat-handler harness as
test_chat_owner_key_plumbing.py.  Proves that chat.py correctly passes
frontend_conversation_id and owner_key to the pipeline by executing
chat() and capturing the pipeline.run() arguments.

No live SDK calls.  No TestClient.  No source-string-only proof of
runtime argument values.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import types
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_HASH = "a" * 64
_SESSION_ID = "session-123"
_EXPLICIT_FRONTEND_ID = "frontend-456"
_GENERATED_FRONTEND_ID = "generated-frontend-789"


# ---------------------------------------------------------------------------
# Helpers (same pattern as test_chat_owner_key_plumbing.py)
# ---------------------------------------------------------------------------


def _make_identity(owner_hash: str = _VALID_HASH):
    """Create a fake identity with the given owner hash."""
    return SimpleNamespace(
        owner_user_id_hash=owner_hash,
        audit_principal="user@example.com",
        source="x-forwarded-user",
    )


def _make_request(
    *,
    session_id: str = _SESSION_ID,
    x_forwarded_email: Optional[str] = "user@example.com",
    extra_headers: Optional[Dict[str, str]] = None,
):
    """Build a minimal fake FastAPI Request."""
    request = MagicMock()
    request.state = SimpleNamespace(session_id=session_id)
    headers: Dict[str, str] = {}
    if x_forwarded_email:
        headers["X-Forwarded-Email"] = x_forwarded_email
    if extra_headers:
        headers.update(extra_headers)
    request.headers.get = lambda k, d=None: headers.get(k, d)
    return request


def _make_body(message: str = "show shipments", conversation_id: Optional[str] = _EXPLICIT_FRONTEND_ID):
    """Build a mock body matching ChatRequest interface."""
    body = MagicMock()
    body.message = message
    body.conversation_id = conversation_id
    return body


def _build_stub_services(*, generated_conv_id: str = _GENERATED_FRONTEND_ID):
    """Build stub services tuple matching _get_services() return shape."""
    conv = MagicMock()
    conv.create_conversation.return_value = generated_conv_id
    conv.add_message.return_value = None
    conv.get_conversation.return_value = {"title": ""}
    conv.generate_title.return_value = "Title"
    conv.update_title.return_value = None
    conv.get_llm_context.return_value = []
    audit = MagicMock()
    audit.log_query.return_value = None
    return MagicMock(), MagicMock(), MagicMock(), conv, audit


def _build_genie_settings():
    """Build mock app settings with USE_GENIE_BACKEND=True."""
    s = MagicMock()
    s.USE_GENIE_BACKEND = True
    s.USE_NEW_ACCURACY_PIPELINE = False
    s.GENIE_FALLBACK_TO_CUSTOM_PIPELINE = False
    s.GENIE_SHOW_SQL = False
    s.GENIE_DEBUG = False
    s.NEW_PIPELINE_DEBUG = False
    s.NEW_PIPELINE_FALLBACK_TO_OLD = False
    return s


class _CapturingGenie:
    """Genie pipeline stub that captures run() kwargs."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "status": "success",
            "message": "ok",
            "is_table": False,
            "fallback_recommended": False,
            "row_count": 0,
            "preview_row_count": 0,
            "returned_row_count": 0,
            "display_row_limit": 100,
            "table_data": None,
            "total_row_count": None,
            "download_key": None,
            "export_id": None,
            "export_status": None,
            "export_mode": None,
            "export_row_count": None,
            "execution_time_ms": 10,
            "conversation_id": kwargs.get("app_conversation_id", ""),
            "genie_conversation_id": None,
            "genie_message_id": None,
            "generated_sql": None,
            "suggested_questions": [],
            "has_visualization": False,
            "query_description": None,
            "clarification": None,
            "source": "genie",
            "computed_chart_data": None,
            "computed_metrics": None,
        }


def _run_chat(
    request,
    body,
    *,
    resolver_return=None,
    resolver_side_effect=None,
    genie_instance=None,
    generated_conv_id: str = _GENERATED_FRONTEND_ID,
):
    """Run chat() with mocked dependencies.  Returns (response, genie_instance)."""
    import app.routes.chat as chat_mod

    resolver = MagicMock()
    if resolver_side_effect is not None:
        resolver.side_effect = resolver_side_effect
    else:
        resolver.return_value = resolver_return

    stub_settings = _build_genie_settings()
    genie = genie_instance or _CapturingGenie()
    fake_genie_mod = types.ModuleType("app.services.genie_backend_factory")
    fake_genie_mod.get_genie_pipeline = lambda user_token=None: genie

    with (
        patch.object(chat_mod, "resolve_request_owner_identity", resolver),
        patch.object(chat_mod, "_app_settings", stub_settings),
        patch.object(chat_mod, "_get_services", return_value=_build_stub_services(generated_conv_id=generated_conv_id)),
        patch.dict(sys.modules, {"app.services.genie_backend_factory": fake_genie_mod}),
    ):
        response = asyncio.run(chat_mod.chat(request=request, body=body))
    return response, genie


# ===========================================================================
# EXPLICIT FRONTEND CONVERSATION ID
# ===========================================================================


class TestExplicitFrontendId:
    """When body.conversation_id is provided, it is passed through directly."""

    def test_pipeline_called_exactly_once(self):
        genie = _CapturingGenie()
        request = _make_request()
        body = _make_body(conversation_id=_EXPLICIT_FRONTEND_ID)
        _run_chat(request, body, resolver_return=_make_identity(), genie_instance=genie)
        assert len(genie.calls) == 1

    def test_app_conversation_id_is_opaque_owner_scoped_key(self):
        """Phase 4C4B3A: When identity is enabled, app_conversation_id is the opaque plc_v1_ key."""
        from app.services.process_local_conversation_key import (
            build_process_local_conversation_key,
            is_valid_process_local_key,
        )
        genie = _CapturingGenie()
        request = _make_request(session_id=_SESSION_ID)
        body = _make_body(conversation_id=_EXPLICIT_FRONTEND_ID)
        _run_chat(request, body, resolver_return=_make_identity(), genie_instance=genie)
        app_conv_id = genie.calls[0]["app_conversation_id"]
        assert is_valid_process_local_key(app_conv_id)
        # Must not be the raw session:frontend composite
        assert app_conv_id != f"{_SESSION_ID}:{_EXPLICIT_FRONTEND_ID}"
        # Must equal the independently computed expected key
        expected = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_HASH,
            session_id=_SESSION_ID,
            frontend_conversation_id=_EXPLICIT_FRONTEND_ID,
        )
        assert app_conv_id == expected

    def test_frontend_conversation_id_passed_separately(self):
        genie = _CapturingGenie()
        request = _make_request()
        body = _make_body(conversation_id=_EXPLICIT_FRONTEND_ID)
        _run_chat(request, body, resolver_return=_make_identity(), genie_instance=genie)
        assert genie.calls[0]["frontend_conversation_id"] == _EXPLICIT_FRONTEND_ID

    def test_response_conversation_id_is_frontend_id(self):
        request = _make_request()
        body = _make_body(conversation_id=_EXPLICIT_FRONTEND_ID)
        response, _ = _run_chat(request, body, resolver_return=_make_identity())
        assert response.conversation_id == _EXPLICIT_FRONTEND_ID


# ===========================================================================
# GENERATED FRONTEND CONVERSATION ID
# ===========================================================================


class TestGeneratedFrontendId:
    """When body.conversation_id is None/empty, a generated ID is used."""

    def test_generated_id_passed_to_pipeline(self):
        genie = _CapturingGenie()
        request = _make_request(session_id=_SESSION_ID)
        body = _make_body(conversation_id=None)
        _run_chat(request, body, resolver_return=_make_identity(), genie_instance=genie,
                  generated_conv_id=_GENERATED_FRONTEND_ID)
        assert len(genie.calls) == 1
        assert genie.calls[0]["frontend_conversation_id"] == _GENERATED_FRONTEND_ID

    def test_app_conversation_id_uses_generated(self):
        from app.services.process_local_conversation_key import (
            build_process_local_conversation_key,
            is_valid_process_local_key,
        )
        genie = _CapturingGenie()
        request = _make_request(session_id=_SESSION_ID)
        body = _make_body(conversation_id=None)
        _run_chat(request, body, resolver_return=_make_identity(), genie_instance=genie,
                  generated_conv_id=_GENERATED_FRONTEND_ID)
        # Phase 4C4B3A: app_conversation_id is now the opaque owner-scoped local key
        app_conv_id = genie.calls[0]["app_conversation_id"]
        assert is_valid_process_local_key(app_conv_id)
        expected = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_HASH,
            session_id=_SESSION_ID,
            frontend_conversation_id=_GENERATED_FRONTEND_ID,
        )
        assert app_conv_id == expected

    def test_response_uses_generated_id(self):
        request = _make_request()
        body = _make_body(conversation_id=None)
        response, _ = _run_chat(request, body, resolver_return=_make_identity(),
                                generated_conv_id=_GENERATED_FRONTEND_ID)
        assert response.conversation_id == _GENERATED_FRONTEND_ID

    def test_same_generated_id_used_consistently(self):
        """Generated ID is used for both pipeline and response."""
        genie = _CapturingGenie()
        request = _make_request(session_id=_SESSION_ID)
        body = _make_body(conversation_id=None)
        response, _ = _run_chat(request, body, resolver_return=_make_identity(),
                                genie_instance=genie, generated_conv_id=_GENERATED_FRONTEND_ID)
        # Pipeline received the generated ID
        assert genie.calls[0]["frontend_conversation_id"] == _GENERATED_FRONTEND_ID
        # Response returned the same generated ID
        assert response.conversation_id == _GENERATED_FRONTEND_ID
        # Phase 4C4B3A: app_conversation_id is opaque plc_v1_ key — does not contain raw ID
        assert genie.calls[0]["app_conversation_id"].startswith("plc_v1_")
        assert len(genie.calls[0]["app_conversation_id"]) == 7 + 64


# ===========================================================================
# TRUSTED OWNER KEY
# ===========================================================================


class TestTrustedOwnerKey:
    """owner_key comes from trusted identity only."""

    def test_owner_hash_passed_as_owner_key(self):
        genie = _CapturingGenie()
        request = _make_request()
        body = _make_body()
        identity = _make_identity(owner_hash=_VALID_HASH)
        _run_chat(request, body, resolver_return=identity, genie_instance=genie)
        assert len(genie.calls) == 1
        assert genie.calls[0]["owner_key"] == _VALID_HASH

    def test_audit_principal_not_passed(self):
        genie = _CapturingGenie()
        request = _make_request()
        body = _make_body()
        _run_chat(request, body, resolver_return=_make_identity(), genie_instance=genie)
        assert "audit_principal" not in genie.calls[0]

    def test_source_not_passed(self):
        genie = _CapturingGenie()
        request = _make_request()
        body = _make_body()
        _run_chat(request, body, resolver_return=_make_identity(), genie_instance=genie)
        assert "source" not in genie.calls[0]

    def test_full_identity_object_not_passed(self):
        genie = _CapturingGenie()
        request = _make_request()
        body = _make_body()
        identity = _make_identity()
        _run_chat(request, body, resolver_return=identity, genie_instance=genie)
        # The identity object itself must not appear in any kwarg value
        for key, val in genie.calls[0].items():
            assert val is not identity, f"Identity object passed as '{key}'"


# ===========================================================================
# OVERRIDE ATTEMPTS
# ===========================================================================


class TestOverrideAttempts:
    """No header/body can override the trusted owner_key."""

    def test_body_cannot_override_owner_key(self):
        genie = _CapturingGenie()
        request = _make_request()
        body = _make_body()
        body.owner_key = "injected-key"
        _run_chat(request, body, resolver_return=_make_identity(owner_hash=_VALID_HASH), genie_instance=genie)
        assert genie.calls[0]["owner_key"] == _VALID_HASH

    def test_x_forwarded_email_cannot_override(self):
        genie = _CapturingGenie()
        request = _make_request(x_forwarded_email="attacker@evil.com")
        body = _make_body()
        _run_chat(request, body, resolver_return=_make_identity(owner_hash=_VALID_HASH), genie_instance=genie)
        assert genie.calls[0]["owner_key"] == _VALID_HASH

    def test_authorization_header_cannot_override(self):
        genie = _CapturingGenie()
        request = _make_request(extra_headers={"Authorization": "Bearer fake-token"})
        body = _make_body()
        _run_chat(request, body, resolver_return=_make_identity(owner_hash=_VALID_HASH), genie_instance=genie)
        assert genie.calls[0]["owner_key"] == _VALID_HASH

    def test_cookie_header_cannot_override(self):
        genie = _CapturingGenie()
        request = _make_request(extra_headers={"Cookie": "session=evil"})
        body = _make_body()
        _run_chat(request, body, resolver_return=_make_identity(owner_hash=_VALID_HASH), genie_instance=genie)
        assert genie.calls[0]["owner_key"] == _VALID_HASH


# ===========================================================================
# DISABLED PATH
# ===========================================================================


class TestDisabledPath:
    """When identity resolver returns None, owner_key is None."""

    def test_owner_key_is_none(self):
        genie = _CapturingGenie()
        request = _make_request()
        body = _make_body(conversation_id=_EXPLICIT_FRONTEND_ID)
        _run_chat(request, body, resolver_return=None, genie_instance=genie)
        assert len(genie.calls) == 1
        assert genie.calls[0]["owner_key"] is None

    def test_frontend_id_still_passed(self):
        genie = _CapturingGenie()
        request = _make_request()
        body = _make_body(conversation_id=_EXPLICIT_FRONTEND_ID)
        _run_chat(request, body, resolver_return=None, genie_instance=genie)
        assert genie.calls[0]["frontend_conversation_id"] == _EXPLICIT_FRONTEND_ID

    def test_generated_id_still_works(self):
        genie = _CapturingGenie()
        request = _make_request(session_id=_SESSION_ID)
        body = _make_body(conversation_id=None)
        _run_chat(request, body, resolver_return=None, genie_instance=genie,
                  generated_conv_id=_GENERATED_FRONTEND_ID)
        assert genie.calls[0]["frontend_conversation_id"] == _GENERATED_FRONTEND_ID
        assert genie.calls[0]["app_conversation_id"] == f"{_SESSION_ID}:{_GENERATED_FRONTEND_ID}"

    def test_response_contract_unchanged(self):
        request = _make_request()
        body = _make_body(conversation_id=_EXPLICIT_FRONTEND_ID)
        response, _ = _run_chat(request, body, resolver_return=None)
        assert hasattr(response, "status")
        assert hasattr(response, "message")
        assert hasattr(response, "conversation_id")
        assert hasattr(response, "fallback_recommended")
        assert response.conversation_id == _EXPLICIT_FRONTEND_ID


# ===========================================================================
# FAILURE PATHS
# ===========================================================================


class TestFailurePaths:
    """HTTP 401 and 503 prevent pipeline execution."""

    def test_401_prevents_pipeline(self):
        from app.services.request_owner_identity_runtime import RequestOwnerIdentityRuntimeResolutionError
        genie = _CapturingGenie()
        request = _make_request()
        body = _make_body()
        with pytest.raises(Exception) as exc_info:
            _run_chat(
                request, body,
                resolver_side_effect=RequestOwnerIdentityRuntimeResolutionError("missing"),
                genie_instance=genie,
            )
        assert "401" in str(exc_info.value) or "401" in str(exc_info.value.status_code)
        assert len(genie.calls) == 0

    def test_503_prevents_pipeline(self):
        from app.services.request_owner_identity_runtime import RequestOwnerIdentityRuntimeConfigurationError
        genie = _CapturingGenie()
        request = _make_request()
        body = _make_body()
        with pytest.raises(Exception) as exc_info:
            _run_chat(
                request, body,
                resolver_side_effect=RequestOwnerIdentityRuntimeConfigurationError("config"),
                genie_instance=genie,
            )
        assert "503" in str(exc_info.value) or "503" in str(exc_info.value.status_code)
        assert len(genie.calls) == 0


# ===========================================================================
# BOUNDARY BEHAVIOUR
# ===========================================================================


class TestBoundaryBehaviour:
    """chat.py must not instantiate durable adapter, repository, or Lakebase."""

    def test_no_durable_adapter_import(self):
        import app.routes.chat as chat_mod
        source = inspect.getsource(chat_mod)
        assert "DurableGenieSessionAdapter" not in source

    def test_no_repository_import(self):
        import app.routes.chat as chat_mod
        source = inspect.getsource(chat_mod)
        assert "conversation_repository" not in source

    def test_no_lakebase_import(self):
        import app.routes.chat as chat_mod
        source = inspect.getsource(chat_mod)
        assert "lakebase" not in source
