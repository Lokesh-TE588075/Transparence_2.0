"""Tests for the feature-flag routing logic (Phase 7B).

Tests the routing helper and result mapping in isolation — no FastAPI dependency.
Validates:
- Flag routing decision
- ChatPipelineResult → frontend response mapping
- Clarification handling
- Fallback behavior on failure
- Session ID pass-through
"""

import sys

sys.path.insert(0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app")

from app.services.chat_pipeline import ChatPipelineInput, ChatPipelineResult
from app.services.pipeline_adapter import (
    map_pipeline_result_to_response,
    should_use_new_pipeline,
)


# =============================================================================
# TEST 1: When flag is false, old pipeline path is selected
# =============================================================================


class TestFlagFalseOldPipeline:
    def test_flag_false(self):
        assert should_use_new_pipeline(False) is False


# =============================================================================
# TEST 2: When flag is true, new pipeline path is selected
# =============================================================================


class TestFlagTrueNewPipeline:
    def test_flag_true(self):
        assert should_use_new_pipeline(True) is True


# =============================================================================
# TEST 3: New pipeline result maps correctly to frontend response contract
# =============================================================================


class TestResultMapping:
    def test_success_maps_correctly(self):
        pr = ChatPipelineResult(
            status="success",
            message="Found 42 shipments.",
            intent="SHIPMENT_QUERY",
            is_table=True,
            table_data={"headers": ["id", "src"], "rows": [{"id": "1", "src": "US"}]},
            row_count=42,
            displayed_row_count=42,
            export_key="export-abc",
            summary_basis="full_result",
        )
        response = map_pipeline_result_to_response(pr, "conv-123", 150)

        assert response["status"] == "success"
        assert response["message"] == "Found 42 shipments."
        assert response["is_table"] is True
        assert response["table_data"]["headers"] == ["id", "src"]
        assert response["row_count"] == 42
        assert response["download_key"] == "export-abc"
        assert response["execution_time_ms"] == 150
        assert response["conversation_id"] == "conv-123"
        assert response["clarification"] is None


# =============================================================================
# TEST 4: Clarification maps to user-facing message
# =============================================================================


class TestClarificationMapping:
    def test_clarification_mapped(self):
        pr = ChatPipelineResult(
            status="success",
            message="Could you specify which country?",
            intent="SHIPMENT_QUERY",
            requires_clarification=True,
            clarification_question="Could you specify which country?",
            sql_generation_source="none",
        )
        response = map_pipeline_result_to_response(pr, "conv-456", 50)

        assert response["status"] == "clarification"
        assert response["clarification"] == "Could you specify which country?"
        assert response["message"] == "Could you specify which country?"


# =============================================================================
# TEST 5: Fallback scenario — failure maps to error status
# =============================================================================


class TestFailureFallback:
    def test_error_maps_to_error_status(self):
        """When fallback is false and pipeline fails, frontend gets clean error."""
        pr = ChatPipelineResult(
            status="error",
            message="An error occurred processing your request.",
            intent="ERROR",
        )
        response = map_pipeline_result_to_response(pr, "conv-789", 200)

        assert response["status"] == "error"
        assert "error" in response["message"].lower()
        # No stack trace exposed
        assert "Traceback" not in response["message"]
        assert "Exception" not in response["message"]


# =============================================================================
# TEST 6: No stack trace in error messages
# =============================================================================


class TestNoStackTrace:
    def test_no_internal_details(self):
        pr = ChatPipelineResult(
            status="error",
            message="An error occurred processing your request: connection timeout",
            intent="ERROR",
        )
        response = map_pipeline_result_to_response(pr, "conv-x", 100)

        assert "Traceback" not in response["message"]
        assert "File \"" not in response["message"]
        assert "line " not in response["message"]


# =============================================================================
# TEST 7: requires_llm_fallback maps appropriately
# =============================================================================


class TestLLMFallbackStatus:
    def test_requires_llm_fallback_maps_to_error(self):
        """When no fallback to old pipeline, returns error status to frontend."""
        pr = ChatPipelineResult(
            status="requires_llm_fallback",
            message="This query requires advanced SQL generation.",
            intent="SHIPMENT_QUERY",
            sql_generation_source="none",
        )
        response = map_pipeline_result_to_response(pr, "conv-llm", 75)

        assert response["status"] == "error"


# =============================================================================
# TEST 8: Session/conversation ID is passed through
# =============================================================================


class TestSessionIDPassthrough:
    def test_conversation_id_preserved(self):
        pr = ChatPipelineResult(
            status="success",
            message="Hello!",
            intent="GREETING",
        )
        response = map_pipeline_result_to_response(pr, "my-session-id-xyz", 30)

        assert response["conversation_id"] == "my-session-id-xyz"

    def test_pipeline_input_takes_conversation_id(self):
        """ChatPipelineInput correctly accepts conversation_id."""
        pinput = ChatPipelineInput(
            user_input="test query",
            conversation_id="session-abc-123",
        )
        assert pinput.conversation_id == "session-abc-123"


# =============================================================================
# TEST 9: Off-topic and greeting status mapping
# =============================================================================


class TestNonBusinessStatusMapping:
    def test_off_topic_maps_correctly(self):
        pr = ChatPipelineResult(
            status="no_sql_required",
            message="I specialize in shipment queries.",
            intent="OFF_TOPIC",
            sql_generation_source="none",
        )
        response = map_pipeline_result_to_response(pr, "conv-ot", 20)
        assert response["status"] == "off_topic"

    def test_greeting_maps_correctly(self):
        pr = ChatPipelineResult(
            status="no_sql_required",
            message="Hello! How can I help?",
            intent="GREETING",
            sql_generation_source="none",
        )
        response = map_pipeline_result_to_response(pr, "conv-gr", 10)
        assert response["status"] == "greeting"

    def test_empty_result_preserves_zero_row_count(self):
        pr = ChatPipelineResult(
            status="success",
            message="No shipments matched.",
            intent="SHIPMENT_QUERY",
            is_table=False,
            row_count=0,
            assistant_suggestion="broaden_date_range",
        )
        response = map_pipeline_result_to_response(pr, "conv-e", 80)
        assert response["row_count"] == 0
        assert response["is_table"] is False
