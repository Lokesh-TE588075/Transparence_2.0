"""Phase 8 Tests: Conversation state factory behavior.

Tests factory routing, fallback, and pipeline integration.
"""

import sys
import time

sys.path.insert(0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app")

from app.services.conversation_state import ConversationStateManager, BusinessContext
from app.services.conversation_state_factory import get_conversation_state_manager
from app.services.chat_pipeline import (
    ChatPipeline,
    ChatPipelineInput,
    SQLExecutionResult,
    SQLValidationResult,
)


class FakeSQLExecutor:
    def __init__(self, fail=False):
        self.fail = fail

    def execute(self, sql):
        if self.fail:
            return SQLExecutionResult(error="Connection refused")
        return SQLExecutionResult(rows=[], columns=[], total_row_count=0)


# =============================================================================
# TEST 7: Factory returns in-memory by default
# =============================================================================


class TestFactoryDefault:
    def test_returns_inmemory_by_default(self):
        """With USE_DELTA_CONVERSATION_STATE=False, returns in-memory."""
        from app.config import settings
        original = settings.USE_DELTA_CONVERSATION_STATE
        try:
            settings.USE_DELTA_CONVERSATION_STATE = False
            manager = get_conversation_state_manager()
            assert isinstance(manager, ConversationStateManager)
            assert type(manager).__name__ == "ConversationStateManager"
        finally:
            settings.USE_DELTA_CONVERSATION_STATE = original


# =============================================================================
# TEST 8: Factory returns Delta when flag is true
# =============================================================================


class TestFactoryDelta:
    def test_returns_delta_when_flag_true(self):
        """With USE_DELTA_CONVERSATION_STATE=True, returns Delta manager."""
        from app.config import settings
        original = settings.USE_DELTA_CONVERSATION_STATE
        try:
            settings.USE_DELTA_CONVERSATION_STATE = True
            executor = FakeSQLExecutor()
            manager = get_conversation_state_manager(sql_executor=executor)
            assert type(manager).__name__ == "DeltaConversationStateManager"
        finally:
            settings.USE_DELTA_CONVERSATION_STATE = original


# =============================================================================
# TEST 9: Factory falls back if Delta init fails
# =============================================================================


class TestFactoryFallback:
    def test_fallback_on_delta_failure(self):
        """If Delta init fails, factory returns working manager."""
        from app.config import settings
        original = settings.USE_DELTA_CONVERSATION_STATE
        try:
            settings.USE_DELTA_CONVERSATION_STATE = True
            executor = FakeSQLExecutor(fail=True)
            manager = get_conversation_state_manager(sql_executor=executor)
            assert isinstance(manager, ConversationStateManager)
            manager._sessions["test"] = BusinessContext(last_updated_at=time.time())
            assert manager.get_context("test") is not None
        finally:
            settings.USE_DELTA_CONVERSATION_STATE = original

    def test_fallback_when_no_executor(self):
        """If no SQL executor provided, factory returns in-memory."""
        from app.config import settings
        original = settings.USE_DELTA_CONVERSATION_STATE
        try:
            settings.USE_DELTA_CONVERSATION_STATE = True
            manager = get_conversation_state_manager(sql_executor=None)
            assert isinstance(manager, ConversationStateManager)
        finally:
            settings.USE_DELTA_CONVERSATION_STATE = original


# =============================================================================
# TEST 10: Pipeline can use factory-provided state manager
# =============================================================================


class TestPipelineIntegration:
    def test_pipeline_with_factory_manager(self):
        """ChatPipeline works with factory-created state manager."""

        class MockExecutor:
            def execute(self, sql):
                return SQLExecutionResult(
                    rows=[{"shipment_number_id": "SH001", "source_": "MX"}],
                    columns=["shipment_number_id", "source_"],
                    total_row_count=1,
                    execution_time_ms=50,
                )

        class MockValidator:
            def validate(self, sql):
                return SQLValidationResult(is_valid=True, sanitized_sql=sql)

        state_mgr = get_conversation_state_manager()
        pipeline = ChatPipeline(
            sql_executor=MockExecutor(),
            sql_validator=MockValidator(),
            llm_service=None,
            state_manager=state_mgr,
        )

        result = pipeline.run(ChatPipelineInput(
            user_input="shipments from Mexico",
            conversation_id="factory-test",
        ))
        assert result.status == "success"


# =============================================================================
# TEST 11: Deterministic follow-up still works with factory manager
# =============================================================================


class TestFollowUpWithFactory:
    def test_followup_preserves_context(self):
        """Deterministic follow-up still works with factory state manager."""

        class MockExecutor:
            def execute(self, sql):
                return SQLExecutionResult(
                    rows=[{"shipment_number_id": "SH001", "source_": "US",
                           "transportation_mode_desc": "Air transport",
                           "execution_status": "In Transit"}],
                    columns=["shipment_number_id", "source_",
                             "transportation_mode_desc", "execution_status"],
                    total_row_count=50,
                    execution_time_ms=80,
                )

        class MockValidator:
            def validate(self, sql):
                return SQLValidationResult(is_valid=True, sanitized_sql=sql)

        state_mgr = get_conversation_state_manager()
        pipeline = ChatPipeline(
            sql_executor=MockExecutor(),
            sql_validator=MockValidator(),
            llm_service=None,
            state_manager=state_mgr,
        )

        # First query
        r1 = pipeline.run(ChatPipelineInput(
            user_input="shipments from US via air",
            conversation_id="followup-factory",
        ))
        assert r1.status == "success"

        # Follow-up
        r2 = pipeline.run(ChatPipelineInput(
            user_input="which are in transit?",
            conversation_id="followup-factory",
        ))
        assert r2.status == "success"
        assert "source_ = 'US'" in (r2.sql_used or "")
        assert "Air transport" in (r2.sql_used or "")
