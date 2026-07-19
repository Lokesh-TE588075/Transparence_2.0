"""Tests for Phase 4C4B3A chat-route owner-scoped process-local key.

Behavioural route tests that exercise chat() end-to-end and capture the
arguments passed to GeniePipeline.run(), proving that:

  1.  Same owner + same cookie + same frontend ID → same local key.
  2.  Different owner + same cookie + same frontend ID → different local key.
  3.  Same owner + different cookie + same frontend ID → different local key.
  4.  Same owner + same cookie + different frontend ID → different local key.
  5.  Pipeline receives raw frontend ID separately (frontend_conversation_id).
  6.  Pipeline receives exact trusted owner hash separately (owner_key).
  7.  Pipeline receives opaque local key as app_conversation_id.
  8.  Request-body owner override is ignored.
  9.  Legacy email header cannot alter the owner-scoped key.
  10. Local key not returned in response.
  11. Owner hash not returned in response.
  12. Session ID not returned in response.
  13. No raw values in logs.
  14. Trusted-identity-disabled mode preserves approved legacy behaviour.
  15. Missing trusted identity follows existing 401 contract when enabled.
  16. Identity-runtime failure follows existing 503 contract.
  17. Same-cookie/different-owner test proves disjoint GenieSessionStore entries.

No live infrastructure.  No HTTP client.  Source-text assertions are avoided:
all key-format assertions use the actual captured runtime argument values.
"""

from __future__ import annotations

import asyncio
import logging
import os as _os
import sys
import threading
import types
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
del _os, _REPO_ROOT

from app.services.process_local_conversation_key import (
    build_process_local_conversation_key,
    is_valid_process_local_key,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_HASH = "a" * 64
_VALID_HASH_2 = "b" * 64
_SESSION_A = "session-aaa"
_SESSION_B = "session-bbb"
_FRONTEND_1 = "frontend-conv-001"
_FRONTEND_2 = "frontend-conv-002"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_identity(owner_hash: str = _VALID_HASH):
    return SimpleNamespace(
        owner_user_id_hash=owner_hash,
        audit_principal="user@example.com",
        source="x-forwarded-user",
    )


def _make_request(
    *,
    session_id: str = _SESSION_A,
    x_forwarded_email: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
):
    request = MagicMock()
    request.state = SimpleNamespace(session_id=session_id)
    headers: Dict[str, str] = {}
    if x_forwarded_email:
        headers["X-Forwarded-Email"] = x_forwarded_email
    if extra_headers:
        headers.update(extra_headers)
    request.headers.get = lambda k, d=None: headers.get(k, d)
    return request


def _make_body(
    message: str = "show delayed shipments",
    conversation_id: Optional[str] = _FRONTEND_1,
):
    body = MagicMock()
    body.message = message
    body.conversation_id = conversation_id
    return body


def _build_stub_services():
    conv = MagicMock()
    conv.create_conversation.return_value = "generated-conv-id"
    conv.add_message.return_value = None
    conv.get_conversation.return_value = {"title": ""}
    conv.generate_title.return_value = "Title"
    conv.update_title.return_value = None
    conv.get_llm_context.return_value = []
    audit = MagicMock()
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
            "total_row_count": None,
            "export_row_count": None,
            "table_data": None,
            "download_key": None,
            "export_id": None,
            "export_status": None,
            "export_mode": None,
            "execution_time_ms": 10,
            "genie_conversation_id": None,
            "genie_message_id": None,
            "generated_sql": None,
            "query_description": None,
            "suggested_questions": [],
            "has_visualization": False,
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
):
    """Run chat() with mocked dependencies. Returns (response, genie_instance)."""
    import app.routes.chat as chat_mod

    resolver = MagicMock()
    if resolver_side_effect is not None:
        resolver.side_effect = resolver_side_effect
    else:
        resolver.return_value = resolver_return

    genie = genie_instance or _CapturingGenie()
    fake_genie_mod = types.ModuleType("app.services.genie_backend_factory")
    fake_genie_mod.get_genie_pipeline = lambda user_token=None: genie

    with (
        patch.object(chat_mod, "resolve_request_owner_identity", resolver),
        patch.object(chat_mod, "_app_settings", _build_genie_settings()),
        patch.object(chat_mod, "_get_services", return_value=_build_stub_services()),
        patch.dict(sys.modules, {"app.services.genie_backend_factory": fake_genie_mod}),
    ):
        response = asyncio.run(chat_mod.chat(request=request, body=body))
    return response, genie


# ===========================================================================
# Tests 1-4 — Key isolation by input variation
# ===========================================================================


class TestKeyIsolation:
    """Tests 1-4: Owner-scoped key changes with each distinct input."""

    def test_01_same_owner_same_cookie_same_frontend_same_key(self):
        """Identical inputs always produce the same local key."""
        genie_a = _CapturingGenie()
        genie_b = _CapturingGenie()
        req = _make_request(session_id=_SESSION_A)
        body = _make_body(conversation_id=_FRONTEND_1)
        identity = _make_identity(_VALID_HASH)

        _run_chat(req, body, resolver_return=identity, genie_instance=genie_a)
        _run_chat(req, body, resolver_return=identity, genie_instance=genie_b)

        assert genie_a.calls[0]["app_conversation_id"] == genie_b.calls[0]["app_conversation_id"]

    def test_02_different_owner_same_cookie_same_frontend_different_key(self):
        """Different trusted owners MUST produce different local keys."""
        genie_a = _CapturingGenie()
        genie_b = _CapturingGenie()
        req = _make_request(session_id=_SESSION_A)
        body = _make_body(conversation_id=_FRONTEND_1)

        _run_chat(req, body, resolver_return=_make_identity(_VALID_HASH), genie_instance=genie_a)
        _run_chat(req, body, resolver_return=_make_identity(_VALID_HASH_2), genie_instance=genie_b)

        key_a = genie_a.calls[0]["app_conversation_id"]
        key_b = genie_b.calls[0]["app_conversation_id"]

        assert key_a != key_b
        # Both must be opaque keys
        assert is_valid_process_local_key(key_a)
        assert is_valid_process_local_key(key_b)

    def test_03_same_owner_different_cookie_same_frontend_different_key(self):
        """Different session cookies MUST produce different local keys."""
        genie_a = _CapturingGenie()
        genie_b = _CapturingGenie()
        body = _make_body(conversation_id=_FRONTEND_1)
        identity = _make_identity(_VALID_HASH)

        req_a = _make_request(session_id=_SESSION_A)
        req_b = _make_request(session_id=_SESSION_B)

        _run_chat(req_a, body, resolver_return=identity, genie_instance=genie_a)
        _run_chat(req_b, body, resolver_return=identity, genie_instance=genie_b)

        key_a = genie_a.calls[0]["app_conversation_id"]
        key_b = genie_b.calls[0]["app_conversation_id"]
        assert key_a != key_b

    def test_04_same_owner_same_cookie_different_frontend_different_key(self):
        """Different frontend IDs MUST produce different local keys."""
        genie_a = _CapturingGenie()
        genie_b = _CapturingGenie()
        req = _make_request(session_id=_SESSION_A)
        identity = _make_identity(_VALID_HASH)

        _run_chat(req, _make_body(conversation_id=_FRONTEND_1), resolver_return=identity, genie_instance=genie_a)
        _run_chat(req, _make_body(conversation_id=_FRONTEND_2), resolver_return=identity, genie_instance=genie_b)

        key_a = genie_a.calls[0]["app_conversation_id"]
        key_b = genie_b.calls[0]["app_conversation_id"]
        assert key_a != key_b


# ===========================================================================
# Tests 5-7 — Pipeline receives correct argument values
# ===========================================================================


class TestPipelineArgumentValues:
    """Tests 5-7: Verify exact argument separation."""

    def test_05_pipeline_receives_raw_frontend_id_separately(self):
        """frontend_conversation_id kwarg must equal the raw ID from body."""
        genie = _CapturingGenie()
        req = _make_request(session_id=_SESSION_A)
        body = _make_body(conversation_id=_FRONTEND_1)
        _run_chat(req, body, resolver_return=_make_identity(_VALID_HASH), genie_instance=genie)

        assert genie.calls[0]["frontend_conversation_id"] == _FRONTEND_1

    def test_06_pipeline_receives_exact_trusted_owner_hash(self):
        """owner_key kwarg must equal the exact owner hash from trusted identity."""
        genie = _CapturingGenie()
        req = _make_request(session_id=_SESSION_A)
        body = _make_body(conversation_id=_FRONTEND_1)
        _run_chat(req, body, resolver_return=_make_identity(_VALID_HASH), genie_instance=genie)

        assert genie.calls[0]["owner_key"] == _VALID_HASH

    def test_07_pipeline_receives_opaque_local_key_as_app_conversation_id(self):
        """app_conversation_id must be the opaque plc_v1_ key, not a raw composite."""
        genie = _CapturingGenie()
        req = _make_request(session_id=_SESSION_A)
        body = _make_body(conversation_id=_FRONTEND_1)
        _run_chat(req, body, resolver_return=_make_identity(_VALID_HASH), genie_instance=genie)

        app_conv_id = genie.calls[0]["app_conversation_id"]
        assert is_valid_process_local_key(app_conv_id)
        # Must not be the raw session:frontend composite
        assert app_conv_id != f"{_SESSION_A}:{_FRONTEND_1}"

    def test_07b_opaque_key_matches_expected_deterministic_value(self):
        """app_conversation_id matches the independently computed expected digest."""
        genie = _CapturingGenie()
        req = _make_request(session_id=_SESSION_A)
        body = _make_body(conversation_id=_FRONTEND_1)
        _run_chat(req, body, resolver_return=_make_identity(_VALID_HASH), genie_instance=genie)

        expected = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_HASH,
            session_id=_SESSION_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        assert genie.calls[0]["app_conversation_id"] == expected


# ===========================================================================
# Tests 8-9 — Override attempts
# ===========================================================================


class TestOverrideAttempts:
    """Tests 8-9: Body and legacy headers cannot alter the owner-scoped key."""

    def test_08_request_body_owner_override_ignored(self):
        """body.owner_key must not affect the local key computation."""
        genie = _CapturingGenie()
        req = _make_request(session_id=_SESSION_A)
        body = _make_body(conversation_id=_FRONTEND_1)
        body.owner_key = _VALID_HASH_2  # attempt injection via body
        _run_chat(req, body, resolver_return=_make_identity(_VALID_HASH), genie_instance=genie)

        # owner_key must come from identity, not body
        assert genie.calls[0]["owner_key"] == _VALID_HASH
        # local key must be derived from _VALID_HASH, not _VALID_HASH_2
        expected = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_HASH,
            session_id=_SESSION_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        unexpected = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_HASH_2,
            session_id=_SESSION_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        app_conv_id = genie.calls[0]["app_conversation_id"]
        assert app_conv_id == expected
        assert app_conv_id != unexpected

    def test_09_legacy_email_cannot_alter_owner_scoped_key(self):
        """X-Forwarded-Email must not affect the local key or owner_key."""
        genie = _CapturingGenie()
        # X-Forwarded-Email contains the VALID_HASH_2 value (attacker sets email=hash)
        req = _make_request(session_id=_SESSION_A, x_forwarded_email=f"{_VALID_HASH_2}@test.com")
        body = _make_body(conversation_id=_FRONTEND_1)
        _run_chat(req, body, resolver_return=_make_identity(_VALID_HASH), genie_instance=genie)

        assert genie.calls[0]["owner_key"] == _VALID_HASH
        expected = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_HASH,
            session_id=_SESSION_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        assert genie.calls[0]["app_conversation_id"] == expected


# ===========================================================================
# Tests 10-13 — Nothing sensitive in response or logs
# ===========================================================================


class TestNoLeakage:
    """Tests 10-13: Sensitive values do not appear in response fields or logs."""

    def test_10_local_key_not_returned_in_response(self):
        genie = _CapturingGenie()
        req = _make_request(session_id=_SESSION_A)
        body = _make_body(conversation_id=_FRONTEND_1)
        response, genie = _run_chat(req, body, resolver_return=_make_identity(_VALID_HASH), genie_instance=genie)

        local_key = genie.calls[0]["app_conversation_id"]
        response_str = str(vars(response))
        assert local_key not in response_str

    def test_11_owner_hash_not_returned_in_response(self):
        req = _make_request(session_id=_SESSION_A)
        body = _make_body(conversation_id=_FRONTEND_1)
        response, _ = _run_chat(req, body, resolver_return=_make_identity(_VALID_HASH))

        response_str = str(vars(response))
        assert _VALID_HASH not in response_str

    def test_12_session_id_not_returned_in_response(self):
        req = _make_request(session_id=_SESSION_A)
        body = _make_body(conversation_id=_FRONTEND_1)
        response, _ = _run_chat(req, body, resolver_return=_make_identity(_VALID_HASH))

        response_str = str(vars(response))
        assert _SESSION_A not in response_str

    def test_13_no_raw_values_in_logs(self):
        import io
        handler = logging.StreamHandler(io.StringIO())
        handler.setLevel(logging.DEBUG)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        level_before = root_logger.level
        root_logger.setLevel(logging.DEBUG)

        req = _make_request(session_id=_SESSION_A)
        body = _make_body(conversation_id=_FRONTEND_1)
        try:
            _run_chat(req, body, resolver_return=_make_identity(_VALID_HASH))
        finally:
            root_logger.removeHandler(handler)
            root_logger.setLevel(level_before)

        log_output = handler.stream.getvalue()
        # The opaque local key itself should not appear in logs
        expected_key = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_HASH,
            session_id=_SESSION_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        assert expected_key not in log_output
        # Owner hash must not appear in logs
        assert _VALID_HASH not in log_output


# ===========================================================================
# Test 14 — Legacy disabled path
# ===========================================================================


class TestLegacyDisabledPath:
    """Test 14: When identity is disabled, legacy session:frontend behaviour preserved."""

    def test_14_disabled_identity_uses_legacy_session_colon_frontend_key(self):
        """With resolver_return=None, app_conversation_id stays session_id:frontend_id."""
        genie = _CapturingGenie()
        req = _make_request(session_id=_SESSION_A)
        body = _make_body(conversation_id=_FRONTEND_1)
        _run_chat(req, body, resolver_return=None, genie_instance=genie)

        app_conv_id = genie.calls[0]["app_conversation_id"]
        # Must be legacy composite, not opaque key
        assert app_conv_id == f"{_SESSION_A}:{_FRONTEND_1}"
        assert not is_valid_process_local_key(app_conv_id)

    def test_14b_disabled_identity_owner_key_is_none(self):
        genie = _CapturingGenie()
        req = _make_request(session_id=_SESSION_A)
        body = _make_body(conversation_id=_FRONTEND_1)
        _run_chat(req, body, resolver_return=None, genie_instance=genie)

        assert genie.calls[0]["owner_key"] is None

    def test_14c_disabled_identity_response_conversation_id_is_frontend_id(self):
        req = _make_request(session_id=_SESSION_A)
        body = _make_body(conversation_id=_FRONTEND_1)
        response, _ = _run_chat(req, body, resolver_return=None)

        assert response.conversation_id == _FRONTEND_1


# ===========================================================================
# Test 15 — Missing identity follows existing 401 contract
# ===========================================================================


class TestMissingIdentity401:
    """Test 15: Missing trusted identity → HTTP 401."""

    def test_15_missing_trusted_identity_raises_401(self):
        from app.services.request_owner_identity_runtime import (
            RequestOwnerIdentityRuntimeResolutionError,
        )
        from fastapi import HTTPException

        req = _make_request(session_id=_SESSION_A)
        body = _make_body(conversation_id=_FRONTEND_1)

        with pytest.raises(HTTPException) as exc_info:
            _run_chat(
                req, body,
                resolver_side_effect=RequestOwnerIdentityRuntimeResolutionError("required"),
            )
        assert exc_info.value.status_code == 401


# ===========================================================================
# Test 16 — Identity runtime failure follows existing 503 contract
# ===========================================================================


class TestIdentityRuntimeFailure503:
    """Test 16: Identity runtime failure → HTTP 503."""

    def test_16_identity_runtime_failure_raises_503(self):
        from app.services.request_owner_identity_runtime import (
            RequestOwnerIdentityRuntimeConfigurationError,
        )
        from fastapi import HTTPException

        req = _make_request(session_id=_SESSION_A)
        body = _make_body(conversation_id=_FRONTEND_1)

        with pytest.raises(HTTPException) as exc_info:
            _run_chat(
                req, body,
                resolver_side_effect=RequestOwnerIdentityRuntimeConfigurationError("config"),
            )
        assert exc_info.value.status_code == 503


# ===========================================================================
# Test 17 — Same cookie/different owner → disjoint GenieSessionStore entries
# ===========================================================================


class TestDisjointSessionStoreEntries:
    """Test 17: Different trusted owners under the same cookie use distinct session keys."""

    def test_17_different_owners_same_cookie_use_disjoint_session_entries(self):
        """Prove that the two app_conversation_id values are different and that
        entries stored under one key are not visible under the other."""
        from app.services.genie_session_store import GenieSessionStore

        store = GenieSessionStore()

        # Compute the expected local keys for both owners
        key_owner_a = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_HASH,
            session_id=_SESSION_A,
            frontend_conversation_id=_FRONTEND_1,
        )
        key_owner_b = build_process_local_conversation_key(
            owner_user_id_hash=_VALID_HASH_2,
            session_id=_SESSION_A,
            frontend_conversation_id=_FRONTEND_1,
        )

        # Keys must be different
        assert key_owner_a != key_owner_b

        # Store a Genie conversation under owner A's key
        store.set_genie_conversation_id(key_owner_a, "genie-conv-owner-a")
        store.update_context(key_owner_a, last_intent="AGGREGATION")

        # Owner B's key must be completely empty
        session_b = store.get_session(key_owner_b)
        assert session_b is None or session_b.genie_conversation_id is None

        # Owner A's entry must be intact
        session_a = store.get_session(key_owner_a)
        assert session_a is not None
        assert session_a.genie_conversation_id == "genie-conv-owner-a"

    def test_17b_pipeline_call_app_conversation_ids_are_disjoint_by_owner(self):
        """End-to-end: two chat() calls with same cookie+frontend but different owners
        produce different app_conversation_id values passed to the pipeline."""
        genie_a = _CapturingGenie()
        genie_b = _CapturingGenie()
        req = _make_request(session_id=_SESSION_A)
        body = _make_body(conversation_id=_FRONTEND_1)

        _run_chat(req, body, resolver_return=_make_identity(_VALID_HASH), genie_instance=genie_a)
        _run_chat(req, body, resolver_return=_make_identity(_VALID_HASH_2), genie_instance=genie_b)

        key_a = genie_a.calls[0]["app_conversation_id"]
        key_b = genie_b.calls[0]["app_conversation_id"]
        assert key_a != key_b
        # Both are valid opaque keys
        assert is_valid_process_local_key(key_a)
        assert is_valid_process_local_key(key_b)
        # Neither contains the other's owner hash
        assert _VALID_HASH not in key_a
        assert _VALID_HASH_2 not in key_b
