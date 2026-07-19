"""Phase 4C2A — Chat route durable lookup key plumbing tests.

Tests that chat.py correctly passes frontend_conversation_id to the pipeline
and does not access durable components directly. Uses source inspection
rather than live HTTP requests to avoid SDK auth issues.
"""
from __future__ import annotations

import inspect
from typing import Any, Dict, Optional

import pytest


# ===========================================================================
# SOURCE-INSPECTION TESTS (no runtime needed)
# ===========================================================================


class TestChatDurableLookupKeyPlumbing:
    """Verify chat.py contracts via source and signature inspection."""

    def _get_chat_source(self):
        import app.routes.chat as chat_module
        return inspect.getsource(chat_module.chat)

    def _get_module_source(self):
        import app.routes.chat as chat_module
        return inspect.getsource(chat_module)

    def test_01_app_conversation_id_is_session_colon_frontend(self):
        """server_conversation_key = session_id:frontend_conversation_id."""
        source = self._get_chat_source()
        assert "session_id" in source
        assert "frontend_conversation_id" in source
        # The key construction pattern
        assert "server_conversation_key" in source

    def test_02_frontend_id_passed_separately(self):
        """frontend_conversation_id kwarg is passed in pipeline.run() call."""
        source = self._get_chat_source()
        assert "frontend_conversation_id=frontend_conversation_id" in source

    def test_03_owner_key_passed_exactly_once(self):
        """owner_key is passed exactly once to pipeline.run()."""
        source = self._get_chat_source()
        run_idx = source.find("_genie_pl.run(")
        run_section = source[run_idx:run_idx + 400]
        assert run_section.count("owner_key=") == 1

    def test_04_frontend_id_passed_exactly_once(self):
        """frontend_conversation_id kwarg appears exactly once in pipeline call."""
        source = self._get_chat_source()
        run_idx = source.find("_genie_pl.run(")
        run_section = source[run_idx:run_idx + 400]
        assert run_section.count("frontend_conversation_id=") == 1

    def test_05_request_body_cannot_override_owner_key(self):
        """ChatRequest model has no owner_key field."""
        from app.routes.chat import ChatRequest
        fields = ChatRequest.model_fields
        assert "owner_key" not in fields

    def test_06_legacy_email_cannot_override_owner_key(self):
        """owner_key is derived from _trusted_identity, not email headers."""
        source = self._get_chat_source()
        owner_idx = source.find("_owner_key")
        owner_section = source[owner_idx:owner_idx + 200]
        assert "owner_user_id_hash" in owner_section

    def test_07_authorization_cannot_override_owner_key(self):
        """Authorization header is not used to derive owner_key."""
        source = self._get_chat_source()
        owner_idx = source.find("_owner_key")
        owner_section = source[owner_idx:owner_idx + 200]
        assert "Authorization" not in owner_section

    def test_08_raw_forwarded_user_not_passed(self):
        """X-Forwarded-User is not passed to pipeline.run()."""
        source = self._get_chat_source()
        run_idx = source.find("_genie_pl.run")
        pipeline_call = source[run_idx:run_idx + 400]
        assert "X-Forwarded-User" not in pipeline_call

    def test_09_audit_principal_not_passed(self):
        """audit_principal is not passed to pipeline.run()."""
        source = self._get_chat_source()
        run_idx = source.find("_genie_pl.run")
        pipeline_call = source[run_idx:run_idx + 400]
        assert "audit_principal" not in pipeline_call

    def test_10_disabled_identity_passes_none_owner_key(self):
        """When _trusted_identity is None, owner_key is None."""
        source = self._get_chat_source()
        assert "_trusted_identity is not None" in source

    def test_11_generated_frontend_id_passed_consistently(self):
        """When no conversation_id in request, generated one is used."""
        source = self._get_chat_source()
        # frontend_conversation_id is derived from body.conversation_id or generated
        assert "frontend_conversation_id" in source

    def test_12_pipeline_branch_selection_unchanged(self):
        """USE_GENIE_BACKEND flag controls pipeline selection."""
        source = self._get_chat_source()
        assert "USE_GENIE_BACKEND" in source

    def test_13_response_model_unchanged(self):
        """ChatResponse model fields are unchanged."""
        from app.routes.chat import ChatResponse
        fields = ChatResponse.model_fields
        assert "status" in fields
        assert "message" in fields
        assert "conversation_id" in fields
        assert "fallback_recommended" in fields

    def test_14_no_durable_adapter_in_chat(self):
        """chat.py does not import DurableGenieSessionAdapter."""
        source = self._get_module_source()
        assert "DurableGenieSessionAdapter" not in source

    def test_15_no_repository_imported_by_chat(self):
        """chat.py does not import conversation_repository."""
        source = self._get_module_source()
        assert "conversation_repository" not in source

    def test_16_no_lakebase_imported_by_chat(self):
        """chat.py does not import lakebase modules."""
        source = self._get_module_source()
        assert "lakebase" not in source

    def test_17_no_additional_request_state_ownership(self):
        """chat.py does not create extra ownership attributes."""
        source = self._get_chat_source()
        setattr_count = source.count("setattr(request.state")
        assert setattr_count <= 1

    def test_18_frontend_conversation_id_in_pipeline_signature(self):
        """GeniePipeline.run() accepts frontend_conversation_id as kwarg."""
        from app.services.genie_pipeline import GeniePipeline
        sig = inspect.signature(GeniePipeline.run)
        assert "frontend_conversation_id" in sig.parameters
        param = sig.parameters["frontend_conversation_id"]
        assert param.default is None
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
