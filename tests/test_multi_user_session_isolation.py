"""Tests for the P0 multi-user session isolation fix.

Scope kept intentionally narrow:
* backend key namespacing in chat.py
* frontend UUID generation in App.jsx
* user header preference in chat.py
* no download ownership validation changes
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch


sys.path.insert(
    0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app"
)

# chat.py imports business_rules.mappings, which imports rapidfuzz. The test
# environment used here does not include that package, so stub only the parts
# needed for module import.
_fake_rapidfuzz = types.ModuleType("rapidfuzz")
_fake_rapidfuzz.fuzz = SimpleNamespace(ratio=lambda *args, **kwargs: 100)
_fake_rapidfuzz.process = SimpleNamespace(extractOne=lambda *args, **kwargs: None)
sys.modules.setdefault("rapidfuzz", _fake_rapidfuzz)


class _FakeGeniePipeline:
    """Minimal stand-in for GeniePipeline that records conversation keys."""

    def __init__(self, *, response: Optional[Dict[str, Any]] = None):
        self.calls: list[str] = []
        self.response = response or {
            "status": "success",
            "message": "ok",
            "is_table": False,
            "fallback_recommended": False,
            "row_count": 0,
            "preview_row_count": 0,
            "returned_row_count": 0,
            "display_row_limit": 100,
        }

    def run(self, *, user_message: str, app_conversation_id: str, **_kwargs) -> Dict[str, Any]:
        self.calls.append(app_conversation_id)
        return dict(self.response)


def _make_request(
    *,
    session_id: Optional[str] = "test-session",
    x_forwarded_email: Optional[str] = None,
    x_user_email: Optional[str] = None,
):
    request = MagicMock()
    request.state = SimpleNamespace()
    if session_id is not None:
        request.state.session_id = session_id

    headers: Dict[str, str] = {}
    if x_forwarded_email is not None:
        headers["X-Forwarded-Email"] = x_forwarded_email
    if x_user_email is not None:
        headers["X-User-Email"] = x_user_email

    request.headers.get = lambda key, default=None: headers.get(key, default)
    return request


def _make_body(*, conversation_id: Optional[str], message: str = "show shipments"):
    body = MagicMock()
    body.message = message
    body.conversation_id = conversation_id
    return body


def _build_stub_context(*, generated_conversation_id: str = "server-generated-uuid"):
    conv_manager = MagicMock()
    conv_manager.create_conversation.return_value = generated_conversation_id
    conv_manager.add_message.return_value = None
    conv_manager.get_conversation.return_value = {"title": ""}
    conv_manager.generate_title.return_value = "Stub title"
    conv_manager.update_title.return_value = None
    conv_manager.get_llm_context.return_value = []

    audit = MagicMock()
    audit.log_query.return_value = ""

    settings = MagicMock()
    settings.USE_GENIE_BACKEND = True
    settings.GENIE_FALLBACK_TO_CUSTOM_PIPELINE = False
    settings.USE_NEW_ACCURACY_PIPELINE = False
    settings.GENIE_SHOW_SQL = False
    settings.GENIE_DEBUG = False
    settings.NEW_PIPELINE_DEBUG = False
    settings.NEW_PIPELINE_FALLBACK_TO_OLD = False

    return settings, conv_manager, audit


def _run_chat(
    *,
    session_id: Optional[str],
    conversation_id: Optional[str],
    genie_pipeline: _FakeGeniePipeline,
    message: str = "show shipments",
    x_forwarded_email: Optional[str] = None,
    x_user_email: Optional[str] = None,
    generated_conversation_id: str = "server-generated-uuid",
):
    import app.routes.chat as chat_mod

    request = _make_request(
        session_id=session_id,
        x_forwarded_email=x_forwarded_email,
        x_user_email=x_user_email,
    )
    body = _make_body(conversation_id=conversation_id, message=message)
    settings, conv_manager, audit = _build_stub_context(
        generated_conversation_id=generated_conversation_id
    )

    fake_factory = types.ModuleType("app.services.genie_backend_factory")
    fake_factory.get_genie_pipeline = lambda user_token=None: genie_pipeline

    with (
        patch.object(chat_mod, "_app_settings", settings),
        patch.object(
            chat_mod,
            "_get_services",
            return_value=(MagicMock(), MagicMock(), MagicMock(), conv_manager, audit),
        ),
        patch.dict(sys.modules, {"app.services.genie_backend_factory": fake_factory}),
    ):
        response = asyncio.run(chat_mod.chat(request=request, body=body))

    return response, conv_manager, audit, genie_pipeline


class TestServerConversationKeyComposition:
    def test_same_frontend_id_different_session_ids_get_different_backend_keys(self):
        pipeline = _FakeGeniePipeline()

        _run_chat(session_id="session-A", conversation_id="1", genie_pipeline=pipeline)
        _run_chat(session_id="session-B", conversation_id="1", genie_pipeline=pipeline)

        assert pipeline.calls == ["session-A:1", "session-B:1"]
        assert pipeline.calls[0] != pipeline.calls[1]

    def test_same_session_id_same_frontend_id_preserves_same_key(self):
        pipeline = _FakeGeniePipeline()

        _run_chat(session_id="session-A", conversation_id="conv-42", genie_pipeline=pipeline)
        _run_chat(session_id="session-A", conversation_id="conv-42", genie_pipeline=pipeline)

        assert pipeline.calls == ["session-A:conv-42", "session-A:conv-42"]

    def test_same_session_id_different_frontend_ids_produce_different_keys(self):
        pipeline = _FakeGeniePipeline()

        _run_chat(session_id="session-A", conversation_id="conv-1", genie_pipeline=pipeline)
        _run_chat(session_id="session-A", conversation_id="conv-2", genie_pipeline=pipeline)

        assert pipeline.calls == ["session-A:conv-1", "session-A:conv-2"]
        assert pipeline.calls[0] != pipeline.calls[1]

    def test_missing_frontend_conversation_id_still_uses_namespaced_generated_id(self):
        pipeline = _FakeGeniePipeline()

        _run_chat(
            session_id="session-X",
            conversation_id=None,
            generated_conversation_id="generated-123",
            genie_pipeline=pipeline,
        )

        assert pipeline.calls == ["session-X:generated-123"]

    def test_missing_session_id_uses_nosession_fallback(self):
        pipeline = _FakeGeniePipeline()

        _run_chat(
            session_id=None,
            conversation_id="conv-9",
            genie_pipeline=pipeline,
        )

        assert pipeline.calls == ["nosession:conv-9"]


class TestResponseConversationIdEcho:
    def test_response_echoes_frontend_id_not_server_key(self):
        response, _conv, _audit, pipeline = _run_chat(
            session_id="session-A",
            conversation_id="1",
            genie_pipeline=_FakeGeniePipeline(),
        )

        assert pipeline.calls == ["session-A:1"]
        assert response.conversation_id == "1"
        assert "session-A" not in (response.conversation_id or "")

    def test_response_echoes_custom_frontend_id(self):
        response, _conv, _audit, pipeline = _run_chat(
            session_id="session-Z",
            conversation_id="my-custom-conv",
            genie_pipeline=_FakeGeniePipeline(),
        )

        assert pipeline.calls == ["session-Z:my-custom-conv"]
        assert response.conversation_id == "my-custom-conv"


class TestExportIsolation:
    def test_user_b_download_request_does_not_receive_user_a_download_key(self):
        user_a_pipeline = _FakeGeniePipeline(
            response={
                "status": "success",
                "message": "download ready",
                "is_table": True,
                "fallback_recommended": False,
                "row_count": 5,
                "preview_row_count": 5,
                "returned_row_count": 5,
                "display_row_limit": 100,
                "download_key": "download-key-for-user-a",
            }
        )
        user_b_pipeline = _FakeGeniePipeline()

        response_a, _conv_a, _audit_a, _pipe_a = _run_chat(
            session_id="session-A",
            conversation_id="1",
            genie_pipeline=user_a_pipeline,
            message="download this data",
        )
        response_b, _conv_b, _audit_b, _pipe_b = _run_chat(
            session_id="session-B",
            conversation_id="1",
            genie_pipeline=user_b_pipeline,
            message="download this data",
        )

        assert response_a.download_key == "download-key-for-user-a"
        assert response_b.download_key is None
        assert user_a_pipeline.calls == ["session-A:1"]
        assert user_b_pipeline.calls == ["session-B:1"]


class TestUserHeaderExtraction:
    def test_x_forwarded_email_is_preferred_for_generated_conversation(self):
        _response, conv_manager, _audit, _pipeline = _run_chat(
            session_id="session-A",
            conversation_id=None,
            x_forwarded_email="alice@te.com",
            x_user_email="fallback@te.com",
            genie_pipeline=_FakeGeniePipeline(),
        )

        assert conv_manager.create_conversation.call_count == 1
        assert conv_manager.create_conversation.call_args.kwargs["user_id"] == "alice@te.com"

    def test_x_user_email_is_used_as_fallback(self):
        _response, conv_manager, _audit, _pipeline = _run_chat(
            session_id="session-A",
            conversation_id=None,
            x_user_email="bob@te.com",
            genie_pipeline=_FakeGeniePipeline(),
        )

        assert conv_manager.create_conversation.call_args.kwargs["user_id"] == "bob@te.com"

    def test_anonymous_is_used_when_no_headers_present(self):
        _response, conv_manager, _audit, _pipeline = _run_chat(
            session_id="session-A",
            conversation_id=None,
            genie_pipeline=_FakeGeniePipeline(),
        )

        assert conv_manager.create_conversation.call_args.kwargs["user_id"] == "anonymous"


class TestGenieSessionStoreIsolation:
    def test_same_frontend_id_different_session_prefixes_have_isolated_state(self):
        from app.services.genie_session_store import GenieSessionStore

        store = GenieSessionStore()
        store.set_genie_conversation_id("session-A:1", "genie-conv-A")
        store.update_context("session-A:1", last_intent="BROAD_LISTING", last_entities=["NB001"])

        snapshot_b = store.get_context_snapshot("session-B:1")
        assert snapshot_b.get("genie_conversation_id") is None
        assert snapshot_b.get("last_intent") is None
        assert not snapshot_b.get("last_entities")

    def test_same_key_preserves_context_for_follow_up(self):
        from app.services.genie_session_store import GenieSessionStore

        store = GenieSessionStore()
        key = "session-A:conv-99"
        store.set_genie_conversation_id(key, "genie-conv-xyz")
        store.update_context(key, last_intent="AGGREGATION", last_entities=["ADC"])

        snapshot = store.get_context_snapshot(key)
        assert snapshot.get("genie_conversation_id") == "genie-conv-xyz"
        assert snapshot.get("last_intent") == "AGGREGATION"
        assert snapshot.get("last_entities") == ["ADC"]

    def test_latest_table_result_isolated_by_key(self):
        from app.services.genie_session_store import GenieSessionStore, TableExportRecord

        store = GenieSessionStore()
        record_a = TableExportRecord(
            assistant_message_id="msg-A",
            download_key="download-key-A",
            export_id="export-A",
            export_status="ready",
            export_mode="returned_rows_only",
            export_row_count=1,
            query_description="test export",
            created_at=datetime.now(timezone.utc),
        )
        store.update_context("session-A:1", latest_table_result=record_a)

        snapshot_b = store.get_context_snapshot("session-B:1")
        snapshot_a = store.get_context_snapshot("session-A:1")

        assert snapshot_b.get("latest_table_result") is None
        latest_a = snapshot_a.get("latest_table_result")
        assert latest_a is not None
        assert latest_a["download_key"] == "download-key-A"
        assert latest_a["assistant_message_id"] == "msg-A"
        assert latest_a["export_status"] == "ready"


class TestFrontendConversationIds:
    def test_app_jsx_no_longer_uses_hardcoded_initial_id_one(self):
        app_path = (
            "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app/"
            "frontend/src/App.jsx"
        )
        with open(app_path) as f:
            content = f.read()

        assert '{ id: "1", title: "New conversation", messages: [] }' not in content
        assert 'const [activeConvId, setActiveConvId] = useState("1")' not in content

    def test_app_jsx_uses_uuid_helper_for_initial_and_new_conversations(self):
        app_path = (
            "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app/"
            "frontend/src/App.jsx"
        )
        with open(app_path) as f:
            content = f.read()

        assert "function _newConvId()" in content
        assert "crypto.randomUUID()" in content
        assert "const _initialId = _newConvId();" in content
        assert "const id = _newConvId();" in content
        assert 'const fresh = { id: _newConvId(), title: "New conversation", messages: [] };' in content
        assert "Date.now().toString()" not in content
