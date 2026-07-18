"""Tests for the query understanding module.

Validates:
- Basic canonical query building from entities
- Follow-up with and without context
- Broaden with deterministic date expansion
- Summarize/download with and without context
- Graceful degradation when context is None
"""

import sys
sys.path.insert(0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app")

from app.models.canonical_query import (
    CanonicalQuery,
    ConversationState,
    DateRange,
    QueryFilters,
    QueryIntent,
)
from app.services.input_normalizer import ExtractedEntities, NormalizedInput, normalize
from app.services.query_understanding import understand


def _make_normalized(cleaned: str, **entity_kwargs) -> NormalizedInput:
    """Helper to create NormalizedInput with specified entities."""
    entities = ExtractedEntities(**entity_kwargs)
    return NormalizedInput(raw=cleaned, cleaned=cleaned, entities=entities)


# =============================================================================
# TEST GROUP: Basic Query Building
# =============================================================================


class TestBasicQueryBuilding:
    """Verify canonical query is correctly built from entities."""

    def test_source_and_mode(self):
        norm = _make_normalized(
            "shipments from Mexico via air",
            source_country="MX",
            source_confidence=1.0,
            transport_mode="Air transport",
        )
        result = understand(norm, QueryIntent.SHIPMENT_QUERY)
        assert result.intent == QueryIntent.SHIPMENT_QUERY
        assert result.filters.source_ == "MX"
        assert result.filters.transportation_mode_desc == "Air transport"
        assert result.confidence >= 1.0

    def test_destination_and_status(self):
        norm = _make_normalized(
            "delayed shipments to Germany",
            destination_country="DE",
            destination_confidence=1.0,
            status_category="delayed",
        )
        result = understand(norm, QueryIntent.SHIPMENT_QUERY)
        assert result.filters.destination == "DE"
        assert result.filters.status_category == "delayed"

    def test_direct_lookup_from_ids(self):
        norm = _make_normalized(
            "details of shipment 4110279735",
            shipment_ids=["4110279735"],
        )
        result = understand(norm, QueryIntent.DIRECT_LOOKUP)
        assert result.intent == QueryIntent.DIRECT_LOOKUP
        assert result.filters.shipment_ids == ["4110279735"]
        assert result.confidence == 1.0

    def test_shipment_query_promotes_to_direct_lookup(self):
        """If SHIPMENT_QUERY has a single ID, it becomes DIRECT_LOOKUP."""
        norm = _make_normalized(
            "shipment 4110279735",
            shipment_ids=["4110279735"],
        )
        result = understand(norm, QueryIntent.SHIPMENT_QUERY)
        assert result.intent == QueryIntent.DIRECT_LOOKUP


# =============================================================================
# TEST GROUP: Follow-up with Context
# =============================================================================


class TestFollowUpWithContext:
    """Verify follow-up merges new entities with previous filters."""

    def test_merge_new_status_with_previous_filters(self):
        """context: source=US, mode=Air. New: status=in_transit."""
        prev_query = CanonicalQuery(
            intent=QueryIntent.SHIPMENT_QUERY,
            filters=QueryFilters(
                source_="US",
                transportation_mode_desc="Air transport",
            ),
        )
        context = ConversationState(last_canonical_query=prev_query)

        norm = _make_normalized(
            "which are in transit?",
            status_category="in_transit",
        )
        result = understand(norm, QueryIntent.FOLLOW_UP_FILTER, context)

        assert result.intent == QueryIntent.FOLLOW_UP_FILTER
        # Previous filters preserved
        assert result.filters.source_ == "US"
        assert result.filters.transportation_mode_desc == "Air transport"
        # New filter added
        assert result.filters.status_category == "in_transit"
        assert result.source == "context_merge"

    def test_new_entity_overrides_previous(self):
        """If user says new source, it overrides previous."""
        prev_query = CanonicalQuery(
            intent=QueryIntent.SHIPMENT_QUERY,
            filters=QueryFilters(source_="US"),
        )
        context = ConversationState(last_canonical_query=prev_query)

        norm = _make_normalized(
            "show from Germany instead",
            source_country="DE",
            source_confidence=1.0,
        )
        result = understand(norm, QueryIntent.FOLLOW_UP_FILTER, context)
        assert result.filters.source_ == "DE"


# =============================================================================
# TEST GROUP: Follow-up without Context
# =============================================================================


class TestFollowUpWithoutContext:
    """Verify graceful degradation when context is None."""

    def test_no_context_asks_clarification(self):
        norm = _make_normalized("which are in transit?", status_category="in_transit")
        result = understand(norm, QueryIntent.FOLLOW_UP_FILTER, context=None)
        assert result.requires_clarification is True
        assert "previous query" in result.clarification_question.lower()

    def test_none_context_object_also_handled(self):
        norm = _make_normalized("show delayed ones")
        result = understand(norm, QueryIntent.FOLLOW_UP_FILTER, context=ConversationState())
        # last_canonical_query is None in empty ConversationState
        assert result.requires_clarification is True


# =============================================================================
# TEST GROUP: Broaden with Context
# =============================================================================


class TestBroadenWithContext:
    """Verify deterministic date range broadening."""

    def test_7_day_range_expands_to_30(self):
        context = ConversationState(
            last_date_range=DateRange(field="eta", start="2026-07-07", end="2026-07-14"),
            last_filters=QueryFilters(source_="US", transportation_mode_desc="Air transport"),
        )

        norm = _make_normalized("check a broader range")
        result = understand(norm, QueryIntent.BROADEN_PREVIOUS_DATE_RANGE, context)

        assert result.intent == QueryIntent.BROADEN_PREVIOUS_DATE_RANGE
        assert result.filters.date_range is not None
        assert result.filters.date_range.start == "2026-07-07"
        assert result.filters.date_range.end == "2026-08-06"  # start + 30 days
        # Previous filters preserved
        assert result.filters.source_ == "US"
        assert result.filters.transportation_mode_desc == "Air transport"
        assert result.confidence == 1.0

    def test_30_day_range_expands_to_90(self):
        context = ConversationState(
            last_date_range=DateRange(field="eta", start="2026-06-01", end="2026-07-01"),
            last_filters=QueryFilters(source_="DE"),
        )
        norm = _make_normalized("expand range")
        result = understand(norm, QueryIntent.BROADEN_PREVIOUS_DATE_RANGE, context)
        assert result.filters.date_range.end == "2026-08-30"  # start + 90 days

    def test_90_day_range_expands_to_180(self):
        context = ConversationState(
            last_date_range=DateRange(field="eta", start="2026-01-01", end="2026-04-01"),
            last_filters=QueryFilters(),
        )
        norm = _make_normalized("wider period")
        result = understand(norm, QueryIntent.BROADEN_PREVIOUS_DATE_RANGE, context)
        assert result.filters.date_range.end == "2026-06-30"  # start + 180 days


# =============================================================================
# TEST GROUP: Broaden without Context
# =============================================================================


class TestBroadenWithoutContext:
    """Verify graceful handling when no previous date range exists."""

    def test_no_context_asks_clarification(self):
        norm = _make_normalized("check a broader range")
        result = understand(norm, QueryIntent.BROADEN_PREVIOUS_DATE_RANGE, context=None)
        assert result.requires_clarification is True
        assert "date range" in result.clarification_question.lower()

    def test_context_without_date_range(self):
        context = ConversationState(last_date_range=None)
        norm = _make_normalized("expand the period")
        result = understand(norm, QueryIntent.BROADEN_PREVIOUS_DATE_RANGE, context)
        assert result.requires_clarification is True


# =============================================================================
# TEST GROUP: Summarize with Context
# =============================================================================


class TestSummarizeWithContext:
    """Verify summarize intent when previous result exists."""

    def test_summarize_with_result_count(self):
        context = ConversationState(
            last_result_row_count=500,
            last_filters=QueryFilters(source_="MX"),
        )
        norm = _make_normalized("quick summary")
        result = understand(norm, QueryIntent.SUMMARIZE_LAST_RESULT, context)
        assert result.intent == QueryIntent.SUMMARIZE_LAST_RESULT
        assert result.requires_clarification is False
        assert result.confidence == 1.0


# =============================================================================
# TEST GROUP: Summarize without Context
# =============================================================================


class TestSummarizeWithoutContext:
    """Verify summarize gracefully asks for context."""

    def test_no_context_asks_clarification(self):
        norm = _make_normalized("quick summary")
        result = understand(norm, QueryIntent.SUMMARIZE_LAST_RESULT, context=None)
        assert result.requires_clarification is True
        assert "summarize" in result.clarification_question.lower()

    def test_empty_context_asks_clarification(self):
        context = ConversationState()  # last_result_row_count = None
        norm = _make_normalized("summarize please")
        result = understand(norm, QueryIntent.SUMMARIZE_LAST_RESULT, context)
        assert result.requires_clarification is True


# =============================================================================
# TEST GROUP: Non-query Intents
# =============================================================================


class TestNonQueryIntents:
    """Verify greeting/exit/off-topic produce minimal CanonicalQuery."""

    def test_greeting(self):
        norm = _make_normalized("hello")
        result = understand(norm, QueryIntent.GREETING)
        assert result.intent == QueryIntent.GREETING
        assert result.confidence == 1.0
        assert result.filters.source_ is None

    def test_off_topic(self):
        norm = _make_normalized("what's the weather today?")
        result = understand(norm, QueryIntent.OFF_TOPIC)
        assert result.intent == QueryIntent.OFF_TOPIC

    def test_abusive(self):
        norm = _make_normalized("you are useless")
        result = understand(norm, QueryIntent.ABUSIVE_OR_INAPPROPRIATE)
        assert result.intent == QueryIntent.ABUSIVE_OR_INAPPROPRIATE


# =============================================================================
# TEST GROUP: End-to-End (normalize + understand)
# =============================================================================


class TestEndToEnd:
    """Verify full pipeline from raw input to canonical query."""

    def test_noisy_input_to_canonical(self):
        """'shipments from Mexico?///////' → source_=MX with no noise."""
        norm = normalize("shipments from Mexico?///////")
        result = understand(norm, QueryIntent.SHIPMENT_QUERY)
        assert result.filters.source_ == "MX"
        assert "//////" not in result.normalized_input

    def test_greeting_prefix_stripped(self):
        norm = normalize("hello, show air shipments from Germany")
        result = understand(norm, QueryIntent.SHIPMENT_QUERY)
        assert result.filters.source_ == "DE"
        assert result.filters.transportation_mode_desc == "Air transport"
