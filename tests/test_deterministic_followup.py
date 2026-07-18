"""Phase 7E Tests: Deterministic follow-up intent detection.

Tests that obvious follow-up messages like "which are in transit?" are
correctly classified as FOLLOW_UP_FILTER (merging with previous context)
without requiring LLM.
"""

import sys

sys.path.insert(0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app")

from app.services.chat_pipeline import (
    ChatPipeline,
    ChatPipelineInput,
    ChatPipelineResult,
    SQLExecutionResult,
    SQLValidationResult,
)


# =============================================================================
# TEST HELPERS
# =============================================================================


def _make_pipeline(llm_service=None):
    """Create pipeline with mock executor and NO LLM (default)."""

    class MockExecutor:
        def execute(self, sql):
            # Return realistic rows based on SQL content
            return SQLExecutionResult(
                rows=[
                    {"shipment_number_id": "SH001", "source_": "US",
                     "destination": "DE", "transportation_mode_desc": "Air transport",
                     "eta": "2026-07-15", "execution_status": "In Transit",
                     "business_unit_id": "ADC", "part_number": "PN1",
                     "actual_pgi_date": "2026-07-01", "ata": None,
                     "final_gr_date": None, "sales_functional_currency_amount": 5000,
                     "chargeable_weight": 100, "currency_code": "USD"},
                ],
                columns=["shipment_number_id", "source_", "destination",
                         "transportation_mode_desc", "eta", "execution_status",
                         "business_unit_id", "part_number", "actual_pgi_date",
                         "ata", "final_gr_date", "sales_functional_currency_amount",
                         "chargeable_weight", "currency_code"],
                total_row_count=50,
                execution_time_ms=80,
            )

    class MockValidator:
        def validate(self, sql):
            return SQLValidationResult(is_valid=True, sanitized_sql=sql)

    return ChatPipeline(
        sql_executor=MockExecutor(),
        sql_validator=MockValidator(),
        llm_service=llm_service,
    )


# =============================================================================
# TEST 1: US + Air → "which are in transit?" preserves filters
# =============================================================================


class TestFollowUpInTransit:
    """Previous: 'shipments from US via air'
    Follow-up: 'which are in transit?'
    Expected: source_='US', Air transport, NOT IN ('Delivered','Completed')"""

    def test_preserves_source_and_mode(self):
        pipeline = _make_pipeline()
        conv = "test-7e-1"

        # First query establishes context
        r1 = pipeline.run(ChatPipelineInput(user_input="shipments from US via air", conversation_id=conv))
        assert r1.status == "success"
        assert "source_ = 'US'" in r1.sql_used
        assert "Air transport" in r1.sql_used

        # Follow-up: should preserve US + Air and add in-transit filter
        r2 = pipeline.run(ChatPipelineInput(user_input="which are in transit?", conversation_id=conv))
        assert r2.status == "success"
        assert r2.sql_generation_source == "deterministic_template"
        assert "source_ = 'US'" in r2.sql_used, f"Missing US filter. SQL: {r2.sql_used}"
        assert "Air transport" in r2.sql_used, f"Missing Air filter. SQL: {r2.sql_used}"
        assert "NOT IN" in r2.sql_used, f"Missing in-transit filter. SQL: {r2.sql_used}"

    def test_intent_is_follow_up(self):
        pipeline = _make_pipeline()
        conv = "test-7e-1b"

        pipeline.run(ChatPipelineInput(user_input="shipments from US via air", conversation_id=conv))
        r2 = pipeline.run(ChatPipelineInput(user_input="which are in transit?", conversation_id=conv))
        assert r2.intent == "follow_up_filter"

    def test_no_clarification(self):
        pipeline = _make_pipeline()
        conv = "test-7e-1c"

        pipeline.run(ChatPipelineInput(user_input="shipments from US via air", conversation_id=conv))
        r2 = pipeline.run(ChatPipelineInput(user_input="which are in transit?", conversation_id=conv))
        assert r2.requires_clarification is False

    def test_works_without_llm(self):
        """Explicitly verify no LLM is needed."""
        pipeline = _make_pipeline(llm_service=None)
        conv = "test-7e-1d"

        pipeline.run(ChatPipelineInput(user_input="shipments from US via air", conversation_id=conv))
        r2 = pipeline.run(ChatPipelineInput(user_input="which are in transit?", conversation_id=conv))
        assert r2.status == "success"
        assert r2.intent == "follow_up_filter"


# =============================================================================
# TEST 2: Mexico + Ocean → "which are completed?" preserves filters
# =============================================================================


class TestFollowUpCompleted:
    """Previous: 'shipments from Mexico via ocean'
    Follow-up: 'which are completed?'
    Expected: source_='MX', Ocean Transport, IN ('Delivered','Completed')"""

    def test_preserves_source_and_mode_adds_completed(self):
        pipeline = _make_pipeline()
        conv = "test-7e-2"

        pipeline.run(ChatPipelineInput(user_input="shipments from Mexico via ocean", conversation_id=conv))
        r2 = pipeline.run(ChatPipelineInput(user_input="which are completed?", conversation_id=conv))
        assert r2.status == "success"
        assert r2.intent == "follow_up_filter"
        assert "source_ = 'MX'" in r2.sql_used, f"Missing MX. SQL: {r2.sql_used}"
        assert "Ocean Transport" in r2.sql_used, f"Missing Ocean. SQL: {r2.sql_used}"
        # Check completed status filter
        assert "IN ('Delivered', 'Completed')" in r2.sql_used or "IN ('Completed', 'Delivered')" in r2.sql_used, \
            f"Missing completed filter. SQL: {r2.sql_used}"


# =============================================================================
# TEST 3: Mode change follow-up — "what about ocean?"
# =============================================================================


class TestFollowUpModeChange:
    """Previous: 'shipments from US to Germany via air'
    Follow-up: 'what about ocean?'
    Expected: source_='US', destination='DE', mode=Ocean Transport (replaced)"""

    def test_mode_replaced(self):
        pipeline = _make_pipeline()
        conv = "test-7e-3"

        pipeline.run(ChatPipelineInput(
            user_input="shipments from US to Germany via air", conversation_id=conv))
        r2 = pipeline.run(ChatPipelineInput(
            user_input="what about ocean?", conversation_id=conv))
        assert r2.status == "success"
        assert r2.intent == "follow_up_filter"
        assert "source_ = 'US'" in r2.sql_used
        assert "destination = 'DE'" in r2.sql_used
        assert "Ocean Transport" in r2.sql_used
        # Air should NOT be in SQL anymore
        assert "Air transport" not in r2.sql_used, f"Air should be replaced. SQL: {r2.sql_used}"

    def test_same_for_air(self):
        """'same for air' should set mode to Air transport."""
        pipeline = _make_pipeline()
        conv = "test-7e-3b"

        pipeline.run(ChatPipelineInput(user_input="shipments from Mexico via ocean", conversation_id=conv))
        r2 = pipeline.run(ChatPipelineInput(user_input="same for air", conversation_id=conv))
        assert r2.status == "success"
        assert "source_ = 'MX'" in r2.sql_used
        assert "Air transport" in r2.sql_used
        assert "Ocean Transport" not in r2.sql_used


# =============================================================================
# TEST 4: Destination addition — "which of them are to Czech?"
# =============================================================================


class TestFollowUpDestination:
    """Previous: 'shipments from Mexico'
    Follow-up: 'which of them are to Czech?'
    Expected: source_='MX', destination='CZ'"""

    def test_destination_added(self):
        pipeline = _make_pipeline()
        conv = "test-7e-4"

        pipeline.run(ChatPipelineInput(user_input="shipments from Mexico", conversation_id=conv))
        r2 = pipeline.run(ChatPipelineInput(user_input="which of them are to Czech?", conversation_id=conv))
        assert r2.status == "success"
        assert r2.intent == "follow_up_filter"
        assert "source_ = 'MX'" in r2.sql_used
        assert "destination = 'CZ'" in r2.sql_used

    def test_short_destination_only(self):
        """'to Germany' alone should be a follow-up."""
        pipeline = _make_pipeline()
        conv = "test-7e-4b"

        pipeline.run(ChatPipelineInput(user_input="shipments from US via air", conversation_id=conv))
        r2 = pipeline.run(ChatPipelineInput(user_input="to Germany", conversation_id=conv))
        assert r2.status == "success"
        assert r2.intent == "follow_up_filter"
        assert "source_ = 'US'" in r2.sql_used
        assert "destination = 'DE'" in r2.sql_used


# =============================================================================
# TEST 5: No false follow-up on standalone queries (safety)
# =============================================================================


class TestNoFalseFollowUp:
    """A new query with source country should NOT be classified as follow-up."""

    def test_new_query_with_source_not_follow_up(self):
        pipeline = _make_pipeline()
        conv = "test-7e-5"

        # Establish context
        pipeline.run(ChatPipelineInput(user_input="shipments from US via air", conversation_id=conv))
        # New query with "from Mexico" — should be a new SHIPMENT_QUERY, not follow-up
        r2 = pipeline.run(ChatPipelineInput(user_input="shipments from Mexico", conversation_id=conv))
        assert r2.intent == "SHIPMENT_QUERY"
        assert "source_ = 'MX'" in r2.sql_used
        # Should NOT have US filter
        assert "source_ = 'US'" not in r2.sql_used

    def test_first_message_not_follow_up(self):
        """Without prior context, short queries should not be follow-up."""
        pipeline = _make_pipeline()
        conv = "test-7e-5b"

        # First query in conv — no context exists
        r1 = pipeline.run(ChatPipelineInput(user_input="which are in transit?", conversation_id=conv))
        # Should be SHIPMENT_QUERY (not follow-up) since no context
        assert r1.intent == "SHIPMENT_QUERY"
