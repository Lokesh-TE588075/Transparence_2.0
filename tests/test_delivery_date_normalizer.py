"""Phase 7D Tests: Normalizer refinement for delivery-date language.

Tests that 'to be delivered' and similar phrases do NOT trigger
false destination-country extraction, and that ETA-based date filtering
is correctly applied.
"""

import sys
from datetime import date

sys.path.insert(0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app")

from app.services.input_normalizer import normalize
from app.services.query_understanding import understand
from app.services.sql_template_engine import generate_sql_from_canonical
from app.services.chat_pipeline import (
    ChatPipeline,
    ChatPipelineInput,
    ChatPipelineResult,
    SQLExecutionResult,
    SQLValidationResult,
)


# =============================================================================
# TEST A: No false destination from "to be delivered"
# =============================================================================


class TestNoFalseDestination:
    """'shipments from US via air to be delivered next week' should NOT
    treat 'be delivered' as a destination."""

    def test_no_destination_extracted(self):
        result = normalize("shipments from US via air to be delivered next week")
        assert result.entities.destination_country is None
        assert result.entities.source_country == "US"
        assert result.entities.transport_mode == "Air transport"

    def test_no_clarification_from_delivery(self):
        result = normalize("shipments from US via air to be delivered next week")
        assert result.entities.requires_clarification is False

    def test_date_phrase_extracted(self):
        result = normalize("shipments from US via air to be delivered next week")
        assert "next week" in result.entities.date_phrases

    def test_preferred_date_field_is_eta(self):
        result = normalize("shipments from US via air to be delivered next week")
        assert result.entities.preferred_date_field == "eta"

    def test_to_be_shipped_not_destination(self):
        result = normalize("shipments from Mexico to be shipped next month")
        assert result.entities.destination_country is None
        assert result.entities.source_country == "MX"

    def test_to_arrive_not_destination(self):
        result = normalize("shipments to arrive next week")
        assert result.entities.destination_country is None

    def test_to_deliver_not_destination(self):
        result = normalize("shipments to deliver this month")
        assert result.entities.destination_country is None


# =============================================================================
# TEST B: Delivery phrase extraction
# =============================================================================


class TestDeliveryPhraseExtraction:
    """'shipments to be delivered next week' should capture date phrase
    and set preferred_date_field."""

    def test_standalone_delivery_phrase(self):
        result = normalize("shipments to be delivered next week")
        assert result.entities.destination_country is None
        assert "next week" in result.entities.date_phrases
        assert result.entities.preferred_date_field == "eta"

    def test_due_next_week(self):
        result = normalize("shipments due next week")
        assert result.entities.preferred_date_field == "eta"
        assert "next week" in result.entities.date_phrases

    def test_arriving_next_month(self):
        result = normalize("shipments arriving next month")
        # "arriving next month" should detect as delivery-date intent
        # but _DATE_PATTERNS may not have "next month" — check preferred_date_field
        # Note: _DELIVERY_DATE_PATTERNS captures "arriving next week/month"
        assert result.entities.preferred_date_field == "eta"

    def test_eta_next_week(self):
        result = normalize("shipments ETA next week")
        assert result.entities.preferred_date_field == "eta"
        assert "next week" in result.entities.date_phrases

    def test_expected_this_week(self):
        result = normalize("shipments expected this week")
        assert result.entities.preferred_date_field == "eta"


# =============================================================================
# TEST C: Real destination "to Germany" still works
# =============================================================================


class TestRealDestinationStillWorks:
    """'shipments from US to Germany next week' should correctly extract
    destination = DE."""

    def test_us_to_germany(self):
        result = normalize("shipments from US to Germany next week")
        assert result.entities.source_country == "US"
        assert result.entities.destination_country == "DE"
        assert "next week" in result.entities.date_phrases

    def test_us_to_mexico(self):
        result = normalize("shipments from US to Mexico")
        assert result.entities.destination_country == "MX"
        assert result.entities.source_country == "US"

    def test_to_china(self):
        result = normalize("air shipments to China")
        assert result.entities.destination_country == "CN"


# =============================================================================
# TEST D: Czech destination still works
# =============================================================================


class TestCzechDestination:
    """'which of them are to Czech?' should still match CZ."""

    def test_to_czech(self):
        result = normalize("which of them are to Czech?")
        assert result.entities.destination_country == "CZ"

    def test_to_czech_republic(self):
        result = normalize("shipments to Czech Republic")
        assert result.entities.destination_country == "CZ"


# =============================================================================
# TEST E: ETA interpretation in query understanding
# =============================================================================


class TestETAInterpretation:
    """When delivery-date phrases are detected, query understanding should
    produce a date_range with field='eta'."""

    def test_eta_date_range_generated(self):
        normalized = normalize("US air shipments ETA next week")
        cq = understand(normalized, "SHIPMENT_QUERY")
        assert cq.filters.date_range is not None
        assert cq.filters.date_range.field == "eta"
        assert cq.filters.date_range.start is not None
        assert cq.filters.date_range.end is not None

    def test_delivery_phrase_sets_eta_field(self):
        normalized = normalize("shipments from US via air to be delivered next week")
        cq = understand(normalized, "SHIPMENT_QUERY")
        assert cq.filters.date_range is not None
        assert cq.filters.date_range.field == "eta"
        # Source and mode preserved
        assert cq.filters.source_ == "US"
        assert cq.filters.transportation_mode_desc == "Air transport"

    def test_no_date_phrase_no_date_range(self):
        """Without date phrases, date_range should be None."""
        normalized = normalize("shipments from US via air")
        cq = understand(normalized, "SHIPMENT_QUERY")
        assert cq.filters.date_range is None

    def test_date_phrase_without_delivery_uses_default_eta(self):
        """A date phrase like 'last week' without delivery verb still uses eta (template default)."""
        normalized = normalize("shipments from US last week")
        cq = understand(normalized, "SHIPMENT_QUERY")
        assert cq.filters.date_range is not None
        assert cq.filters.date_range.field == "eta"  # interpret_date_phrase defaults to eta


# =============================================================================
# TEST F: End-to-end pipeline test
# =============================================================================


class TestEndToEndPipeline:
    """Full pipeline: 'shipments from US via air to be delivered next week'
    should produce deterministic SQL with ETA filter."""

    def _make_pipeline(self):
        """Create pipeline with mock executor that returns sample data."""
        class MockExecutor:
            def execute(self, sql):
                return SQLExecutionResult(
                    rows=[{"shipment_number_id": "SH001", "source_": "US",
                           "destination": "DE", "transportation_mode_desc": "Air transport",
                           "eta": "2026-07-15", "execution_status": "In Transit"}],
                    columns=["shipment_number_id", "source_", "destination",
                             "transportation_mode_desc", "eta", "execution_status"],
                    total_row_count=1,
                    execution_time_ms=50,
                )

        class MockValidator:
            def validate(self, sql):
                return SQLValidationResult(is_valid=True, sanitized_sql=sql)

        return ChatPipeline(
            sql_executor=MockExecutor(),
            sql_validator=MockValidator(),
            llm_service=None,
        )

    def test_deterministic_sql_generated(self):
        pipeline = self._make_pipeline()
        result = pipeline.run(ChatPipelineInput(
            user_input="shipments from US via air to be delivered next week",
            conversation_id="test-7d",
        ))
        assert result.status == "success"
        assert result.sql_generation_source == "deterministic_template"
        assert result.sql_used is not None

    def test_sql_contains_source_filter(self):
        pipeline = self._make_pipeline()
        result = pipeline.run(ChatPipelineInput(
            user_input="shipments from US via air to be delivered next week",
            conversation_id="test-7d-src",
        ))
        assert "source_ = 'US'" in result.sql_used

    def test_sql_contains_mode_filter(self):
        pipeline = self._make_pipeline()
        result = pipeline.run(ChatPipelineInput(
            user_input="shipments from US via air to be delivered next week",
            conversation_id="test-7d-mode",
        ))
        assert "transportation_mode_desc = 'Air transport'" in result.sql_used

    def test_sql_contains_eta_date_filter(self):
        pipeline = self._make_pipeline()
        result = pipeline.run(ChatPipelineInput(
            user_input="shipments from US via air to be delivered next week",
            conversation_id="test-7d-eta",
        ))
        assert "DATE(eta)" in result.sql_used

    def test_response_includes_eta_note(self):
        pipeline = self._make_pipeline()
        result = pipeline.run(ChatPipelineInput(
            user_input="shipments from US via air to be delivered next week",
            conversation_id="test-7d-note",
        ))
        # The template engine adds this note when field == "eta"
        assert any("ETA" in note for note in result.interpretation_notes)

    def test_no_clarification_required(self):
        pipeline = self._make_pipeline()
        result = pipeline.run(ChatPipelineInput(
            user_input="shipments from US via air to be delivered next week",
            conversation_id="test-7d-no-clarif",
        ))
        assert result.requires_clarification is False
        assert result.status == "success"
