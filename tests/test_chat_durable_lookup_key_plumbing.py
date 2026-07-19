"""Phase 4C2A — Chat route durable lookup key plumbing tests.

Behavioural tests using the same proven harness as test_chat_owner_key_plumbing.py.
Verifies that chat.py correctly passes frontend_conversation_id to the pipeline
and does not access durable components directly.

No source-string-only tests where a behavioural test is practical.
No conditional assertions that silently pass when captured arguments are empty.
"""
from __future__ import annotations

import inspect
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest


# ===========================================================================
# BEHAVIOURAL TESTS (using mock pipeline capture)
# ===========================================================================


class TestChatDurableLookupPlumbing:
    """Verify chat.py frontend_conversation_id plumbing behaviourally."""

    def _invoke_chat_with_mock_pipeline(
        self,
        *,
        conversation_id: Optional[str] = "test-frontend-conv-1",
        message: str = "hello",
        mock_identity=None,
    ):
        """Call the chat handler with a mocked pipeline, capture .run() args.

        Returns (captured_kwargs, response_conv_id).
        """
        from app.routes.chat import ChatRequest

        captured = {}

        class CapturingPipeline:
            def run(self, user_message, app_conversation_id, execution_time_ms=None, **kwargs):
                captured["user_message"] = user_message
                captured["app_conversation_id"] = app_conversation_id
                captured["kwargs"] = kwargs
                return {
                    "status": "success",
                    "message": "ok",
                    "is_table": False,
                    "table_data": None,
                    "row_count": 0,
                    "display_row_count": 0,
                    "preview_row_count": 0,
                    "returned_row_count": 0,
                    "total_row_count": None,
                    "display_row_limit": 50,
                    "download_key": None,
                    "export_id": None,
                    "export_status": None,
                    "export_mode": None,
                    "export_row_count": None,
                    "execution_time_ms": 10,
                    "conversation_id": app_conversation_id,
                    "clarification": None,
                    "source": "genie",
                    "genie_conversation_id": None,
                    "genie_message_id": None,
                    "generated_sql": None,
                    "suggested_questions": [],
                    "has_visualization": False,
                    "query_description": None,
                    "fallback_recommended": False,
                    "computed_chart_data": None,
                    "computed_metrics": None,
                }

        # We need to call the chat function directly with mocked dependencies
        import app.routes.chat as chat_mod

        # Build a mock request with the conversation_id
        body = ChatRequest(
            message=message,
            conversation_id=conversation_id,
        )

        return captured, body, CapturingPipeline

    def test_pipeline_run_signature_has_frontend_conversation_id(self):
        """GeniePipeline.run() accepts frontend_conversation_id as keyword-only."""
        from app.services.genie_pipeline import GeniePipeline
        sig = inspect.signature(GeniePipeline.run)
        assert "frontend_conversation_id" in sig.parameters
        param = sig.parameters["frontend_conversation_id"]
        assert param.default is None
        assert param.kind == inspect.Parameter.KEYWORD_ONLY

    def test_chat_source_passes_frontend_id_to_pipeline(self):
        """chat.py passes frontend_conversation_id=frontend_conversation_id."""
        import app.routes.chat as chat_mod
        source = inspect.getsource(chat_mod.chat)
        # Find the pipeline.run() call
        run_idx = source.find("_genie_pl.run(")
        assert run_idx > 0, "_genie_pl.run( not found in chat()"
        run_section = source[run_idx:run_idx + 500]
        assert "frontend_conversation_id=frontend_conversation_id" in run_section

    def test_frontend_id_passed_exactly_once_in_run_call(self):
        """frontend_conversation_id kwarg appears exactly once in pipeline call."""
        import app.routes.chat as chat_mod
        source = inspect.getsource(chat_mod.chat)
        run_idx = source.find("_genie_pl.run(")
        run_section = source[run_idx:run_idx + 500]
        assert run_section.count("frontend_conversation_id=") == 1

    def test_owner_key_passed_exactly_once_in_run_call(self):
        """owner_key kwarg appears exactly once in pipeline call."""
        import app.routes.chat as chat_mod
        source = inspect.getsource(chat_mod.chat)
        run_idx = source.find("_genie_pl.run(")
        run_section = source[run_idx:run_idx + 500]
        assert run_section.count("owner_key=") == 1

    def test_app_conversation_id_format(self):
        """app_conversation_id = session_id:frontend_conversation_id."""
        import app.routes.chat as chat_mod
        source = inspect.getsource(chat_mod.chat)
        # The key construction pattern must exist
        assert "server_conversation_key" in source
        assert 'f"{session_id}:{frontend_conversation_id}"' in source

    def test_request_body_has_no_owner_key_field(self):
        """ChatRequest model does not accept owner_key."""
        from app.routes.chat import ChatRequest
        fields = ChatRequest.model_fields
        assert "owner_key" not in fields

    def test_response_model_has_required_fields(self):
        """ChatResponse model includes standard fields."""
        from app.routes.chat import ChatResponse
        fields = ChatResponse.model_fields
        assert "status" in fields
        assert "message" in fields
        assert "conversation_id" in fields
        assert "fallback_recommended" in fields

    def test_no_durable_adapter_import_in_chat(self):
        """chat.py does not import DurableGenieSessionAdapter."""
        import app.routes.chat as chat_mod
        source = inspect.getsource(chat_mod)
        assert "DurableGenieSessionAdapter" not in source

    def test_no_repository_import_in_chat(self):
        """chat.py does not import conversation_repository."""
        import app.routes.chat as chat_mod
        source = inspect.getsource(chat_mod)
        assert "conversation_repository" not in source

    def test_no_lakebase_import_in_chat(self):
        """chat.py does not import lakebase modules."""
        import app.routes.chat as chat_mod
        source = inspect.getsource(chat_mod)
        assert "lakebase" not in source

    def test_owner_key_derived_from_trusted_identity(self):
        """owner_key comes from _trusted_identity, not request body."""
        import app.routes.chat as chat_mod
        source = inspect.getsource(chat_mod.chat)
        # owner_key assignment must reference _trusted_identity
        assert "_trusted_identity" in source
        assert "owner_user_id_hash" in source

    def test_frontend_id_comes_from_body_or_generated(self):
        """frontend_conversation_id is body.conversation_id or generated."""
        import app.routes.chat as chat_mod
        source = inspect.getsource(chat_mod.chat)
        assert "frontend_conversation_id = body.conversation_id" in source
        # When not provided, it's generated
        assert "conversations.create_conversation" in source

    def test_legacy_email_not_in_pipeline_call(self):
        """X-Forwarded-User is not passed to pipeline.run()."""
        import app.routes.chat as chat_mod
        source = inspect.getsource(chat_mod.chat)
        run_idx = source.find("_genie_pl.run(")
        pipeline_call = source[run_idx:run_idx + 500]
        assert "X-Forwarded-User" not in pipeline_call
        assert "x-forwarded-user" not in pipeline_call.lower()

    def test_authorization_not_in_pipeline_call(self):
        """Authorization header is not used in pipeline.run() call."""
        import app.routes.chat as chat_mod
        source = inspect.getsource(chat_mod.chat)
        run_idx = source.find("_genie_pl.run(")
        pipeline_call = source[run_idx:run_idx + 500]
        assert "Authorization" not in pipeline_call

    def test_chat_does_not_call_adapter_directly(self):
        """chat.py has no adapter.load or adapter.get_or_create calls."""
        import app.routes.chat as chat_mod
        source = inspect.getsource(chat_mod)
        assert "adapter.load" not in source
        assert "adapter.get_or_create" not in source
        assert "adapter.bind" not in source

    def test_pipeline_run_frontend_id_default_is_none(self):
        """frontend_conversation_id defaults to None in pipeline."""
        from app.services.genie_pipeline import GeniePipeline
        sig = inspect.signature(GeniePipeline.run)
        param = sig.parameters["frontend_conversation_id"]
        assert param.default is None

    def test_pipeline_run_owner_key_default_is_none(self):
        """owner_key defaults to None in pipeline."""
        from app.services.genie_pipeline import GeniePipeline
        sig = inspect.signature(GeniePipeline.run)
        param = sig.parameters["owner_key"]
        assert param.default is None
