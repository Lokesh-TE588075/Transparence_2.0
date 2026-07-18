"""Tests for conversation state tracking and follow-up resolution.

Validates:
- Business context storage after successful queries
- Off-topic/abusive/greeting/exit message protection
- Follow-up filter merging with previous context
- Summarize/download/broaden resolution
- Row reference resolution ("first shipment")
- Session expiry and reset
- Clarification when context is missing
"""

import sys
import time

sys.path.insert(0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app")

from app.models.canonical_query import (
    CanonicalQuery,
    DateRange,
    QueryFilters,
    QueryIntent,
)
from app.services.conversation_state import (
    BusinessContext,
    ConversationStateManager,
    ResultMetadata,
)
from app.services.input_normalizer import NormalizedInput, ExtractedEntities, normalize
from app.services.query_understanding import understand


# =============================================================================
# HELPERS
# =============================================================================


def _create_manager(ttl: int = 86400) -> ConversationStateManager:
    """Create a fresh state manager with given TTL."""
    return ConversationStateManager(session_ttl_seconds=ttl)


def _store_us_air_query(mgr: ConversationStateManager, conv_id: str = "conv-1"):
    """Store a baseline query: US source, Air transport, 42 results."""
    cq = CanonicalQuery(
        intent=QueryIntent.SHIPMENT_QUERY,
        filters=QueryFilters(
            source_="US",
            transportation_mode_desc="Air transport",
        ),
        confidence=1.0,
        source="deterministic",
    )
    mgr.update_after_query(
        conv_id,
        user_input="show air shipments from US",
        normalized_input="show air shipments from US",
        canonical_query=cq,
        validated_sql="SELECT * FROM table WHERE source_ = 'US' AND transportation_mode_desc = 'Air transport'",
        result_metadata=ResultMetadata(
            total_row_count=42,
            displayed_rows=20,
            columns=["shipment_number_id", "source_", "destination", "execution_status"],
            has_aggregates=False,
            is_empty=False,
        ),
        displayed_rows=[
            {"shipment_number_id": "4110279735", "source_": "US", "destination": "DE", "execution_status": "In Transit"},
            {"shipment_number_id": "4110279736", "source_": "US", "destination": "CZ", "execution_status": "Delivered"},
            {"shipment_number_id": "4110279737", "source_": "US", "destination": "MX", "execution_status": "In Transit"},
        ],
        total_row_count=42,
        download_key="download-key-abc123",
        assistant_suggestion="Try a broader date range for more results.",
        result_summary="Found 42 air shipments from US.",
    )


def _store_with_date_range(mgr: ConversationStateManager, conv_id: str = "conv-1"):
    """Store a query with date range for broaden testing."""
    cq = CanonicalQuery(
        intent=QueryIntent.SHIPMENT_QUERY,
        filters=QueryFilters(
            source_="US",
            transportation_mode_desc="Air transport",
            date_range=DateRange(field="eta", start="2026-07-01", end="2026-07-07"),
        ),
        confidence=1.0,
        source="deterministic",
    )
    mgr.update_after_query(
        conv_id,
        user_input="air shipments from US this week",
        normalized_input="air shipments from US this week",
        canonical_query=cq,
        validated_sql="SELECT * FROM table WHERE eta BETWEEN '2026-07-01' AND '2026-07-07'",
        result_metadata=ResultMetadata(total_row_count=5, displayed_rows=5, is_empty=False),
        total_row_count=5,
        assistant_suggestion="Only 5 results found. Try a broader date range.",
    )


# =============================================================================
# TEST 1: Stores last canonical query and filters correctly
# =============================================================================


class TestStoreCanonicalQuery:
    def test_stores_query_and_filters(self):
        mgr = _create_manager()
        _store_us_air_query(mgr)

        ctx = mgr.get_context("conv-1")
        assert ctx is not None
        assert ctx.last_canonical_query is not None
        assert ctx.last_canonical_query.intent == QueryIntent.SHIPMENT_QUERY
        assert ctx.last_filters is not None
        assert ctx.last_filters.source_ == "US"
        assert ctx.last_filters.transportation_mode_desc == "Air transport"
        assert ctx.last_validated_sql is not None
        assert "source_" in ctx.last_validated_sql


# =============================================================================
# TEST 2: Stores result metadata and displayed rows correctly
# =============================================================================


class TestStoreResultMetadata:
    def test_stores_metadata_and_rows(self):
        mgr = _create_manager()
        _store_us_air_query(mgr)

        ctx = mgr.get_context("conv-1")
        assert ctx.last_result_metadata is not None
        assert ctx.last_result_metadata.total_row_count == 42
        assert ctx.last_result_metadata.displayed_rows == 20
        assert ctx.last_displayed_rows is not None
        assert len(ctx.last_displayed_rows) == 3
        assert ctx.last_total_row_count == 42
        assert ctx.last_download_key == "download-key-abc123"
        assert ctx.last_result_summary == "Found 42 air shipments from US."


# =============================================================================
# TEST 3: Off-topic message does not overwrite business context
# =============================================================================


class TestOffTopicProtection:
    def test_off_topic_does_not_overwrite(self):
        mgr = _create_manager()
        _store_us_air_query(mgr)

        # Now send an off-topic message
        off_topic_cq = CanonicalQuery(
            intent=QueryIntent.OFF_TOPIC,
            filters=QueryFilters(),
            confidence=1.0,
        )
        mgr.update_after_query(
            "conv-1",
            user_input="what's the weather?",
            normalized_input="what's the weather?",
            canonical_query=off_topic_cq,
        )

        # Business context should be unchanged
        ctx = mgr.get_context("conv-1")
        assert ctx.last_filters.source_ == "US"
        assert ctx.last_filters.transportation_mode_desc == "Air transport"
        assert ctx.last_total_row_count == 42


# =============================================================================
# TEST 4: Exit message does not overwrite business context
# =============================================================================


class TestExitProtection:
    def test_exit_does_not_overwrite(self):
        mgr = _create_manager()
        _store_us_air_query(mgr)

        exit_cq = CanonicalQuery(
            intent=QueryIntent.EXIT,
            filters=QueryFilters(),
            confidence=1.0,
        )
        mgr.update_after_query(
            "conv-1",
            user_input="bye",
            normalized_input="bye",
            canonical_query=exit_cq,
        )

        ctx = mgr.get_context("conv-1")
        assert ctx.last_filters.source_ == "US"
        assert ctx.last_total_row_count == 42


# =============================================================================
# TEST 5: Abusive message does not overwrite business context
# =============================================================================


class TestAbusiveProtection:
    def test_abusive_does_not_overwrite(self):
        mgr = _create_manager()
        _store_us_air_query(mgr)

        abusive_cq = CanonicalQuery(
            intent=QueryIntent.ABUSIVE_OR_INAPPROPRIATE,
            filters=QueryFilters(),
            confidence=1.0,
        )
        mgr.update_after_query(
            "conv-1",
            user_input="you are useless",
            normalized_input="you are useless",
            canonical_query=abusive_cq,
        )

        ctx = mgr.get_context("conv-1")
        assert ctx.last_filters.source_ == "US"
        assert ctx.last_canonical_query.intent == QueryIntent.SHIPMENT_QUERY


# =============================================================================
# TEST 6: "which are in transit?" preserves previous source and mode
# =============================================================================


class TestFollowUpInTransit:
    def test_preserves_source_and_mode(self):
        mgr = _create_manager()
        _store_us_air_query(mgr)

        # Simulate follow-up: "which are in transit?"
        state = mgr.get_conversation_state("conv-1")
        norm = normalize("which are in transit?")
        result = understand(norm, QueryIntent.FOLLOW_UP_FILTER, state)

        # Previous source and mode preserved
        assert result.filters.source_ == "US"
        assert result.filters.transportation_mode_desc == "Air transport"
        # New status added
        assert result.filters.status_category == "in_transit"


# =============================================================================
# TEST 7: "which of them are to Czech?" preserves source and adds destination
# =============================================================================


class TestFollowUpDestination:
    def test_preserves_source_adds_destination(self):
        mgr = _create_manager()
        _store_us_air_query(mgr)

        state = mgr.get_conversation_state("conv-1")
        norm = normalize("which of them are to Czech Republic?")
        result = understand(norm, QueryIntent.FOLLOW_UP_FILTER, state)

        assert result.filters.source_ == "US"
        assert result.filters.destination == "CZ"
        assert result.filters.transportation_mode_desc == "Air transport"


# =============================================================================
# TEST 8: "quick summary" uses previous result metadata
# =============================================================================


class TestSummarizeResolution:
    def test_quick_summary_resolves(self):
        mgr = _create_manager()
        _store_us_air_query(mgr)

        state = mgr.get_conversation_state("conv-1")
        assert state is not None
        assert state.last_result_row_count == 42

        norm = normalize("quick summary")
        result = understand(norm, QueryIntent.SUMMARIZE_LAST_RESULT, state)
        assert result.intent == QueryIntent.SUMMARIZE_LAST_RESULT
        assert result.requires_clarification is False


# =============================================================================
# TEST 9: "download this result" uses previous download key
# =============================================================================


class TestDownloadResolution:
    def test_download_uses_key(self):
        mgr = _create_manager()
        _store_us_air_query(mgr)

        assert mgr.can_download("conv-1") is True
        assert mgr.get_last_download_key("conv-1") == "download-key-abc123"

        state = mgr.get_conversation_state("conv-1")
        norm = normalize("download this result")
        result = understand(norm, QueryIntent.DOWNLOAD_LAST_RESULT, state)
        assert result.intent == QueryIntent.DOWNLOAD_LAST_RESULT
        assert result.requires_clarification is False


# =============================================================================
# TEST 10: "broader range" works only if previous assistant suggestion exists
# =============================================================================


class TestBroadenWithSuggestion:
    def test_broaden_works_with_date_range(self):
        mgr = _create_manager()
        _store_with_date_range(mgr)

        assert mgr.can_broaden("conv-1") is True
        state = mgr.get_conversation_state("conv-1")
        assert state.last_date_range is not None

        norm = normalize("yes check a broader range")
        result = understand(norm, QueryIntent.BROADEN_PREVIOUS_DATE_RANGE, state)
        assert result.intent == QueryIntent.BROADEN_PREVIOUS_DATE_RANGE
        assert result.requires_clarification is False
        # Should expand 7 days to 30 days
        assert result.filters.date_range.end == "2026-07-31"
        # Preserves other filters
        assert result.filters.source_ == "US"
        assert result.filters.transportation_mode_desc == "Air transport"


# =============================================================================
# TEST 11: "broader range" without previous suggestion asks clarification
# =============================================================================


class TestBroadenWithoutSuggestion:
    def test_broaden_without_date_range_asks_clarification(self):
        mgr = _create_manager()
        # Store query WITHOUT date range
        _store_us_air_query(mgr)

        assert mgr.can_broaden("conv-1") is False
        state = mgr.get_conversation_state("conv-1")
        norm = normalize("broader range please")
        result = understand(norm, QueryIntent.BROADEN_PREVIOUS_DATE_RANGE, state)
        assert result.requires_clarification is True


# =============================================================================
# TEST 12: "what about ocean?" replaces mode but preserves source/destination
# =============================================================================


class TestReplaceModeFollowUp:
    def test_replaces_mode_preserves_other_filters(self):
        mgr = _create_manager()
        _store_us_air_query(mgr)

        state = mgr.get_conversation_state("conv-1")
        norm = normalize("what about ocean?")
        result = understand(norm, QueryIntent.FOLLOW_UP_FILTER, state)

        # Mode replaced
        assert result.filters.transportation_mode_desc == "Ocean Transport"
        # Source preserved
        assert result.filters.source_ == "US"


# =============================================================================
# TEST 13: "give details of first shipment" uses first displayed row
# =============================================================================


class TestRowReference:
    def test_resolves_first_row(self):
        mgr = _create_manager()
        _store_us_air_query(mgr)

        first_row = mgr.resolve_row_reference("conv-1", ordinal=0)
        assert first_row is not None
        assert first_row["shipment_number_id"] == "4110279735"

    def test_resolves_second_row(self):
        mgr = _create_manager()
        _store_us_air_query(mgr)

        second_row = mgr.resolve_row_reference("conv-1", ordinal=1)
        assert second_row is not None
        assert second_row["shipment_number_id"] == "4110279736"

    def test_out_of_range_returns_none(self):
        mgr = _create_manager()
        _store_us_air_query(mgr)

        row = mgr.resolve_row_reference("conv-1", ordinal=99)
        assert row is None


# =============================================================================
# TEST 14: Missing context returns clarification, not guessed filters
# =============================================================================


class TestMissingContext:
    def test_follow_up_without_context_asks_clarification(self):
        mgr = _create_manager()
        # No previous query stored
        state = mgr.get_conversation_state("conv-1")
        assert state is None

        norm = normalize("which are in transit?")
        result = understand(norm, QueryIntent.FOLLOW_UP_FILTER, context=None)
        assert result.requires_clarification is True

    def test_summarize_without_context(self):
        mgr = _create_manager()
        state = mgr.get_conversation_state("conv-1")
        norm = normalize("quick summary")
        result = understand(norm, QueryIntent.SUMMARIZE_LAST_RESULT, context=state)
        assert result.requires_clarification is True

    def test_download_without_context(self):
        mgr = _create_manager()
        assert mgr.can_download("conv-1") is False


# =============================================================================
# TEST 15: Session reset clears structured business context
# =============================================================================


class TestSessionReset:
    def test_reset_clears_context(self):
        mgr = _create_manager()
        _store_us_air_query(mgr)

        # Verify context exists
        assert mgr.get_context("conv-1") is not None

        # Reset
        mgr.reset_context("conv-1")

        # Context should be gone
        assert mgr.get_context("conv-1") is None
        assert mgr.get_last_filters("conv-1") is None
        assert mgr.get_conversation_state("conv-1") is None


# =============================================================================
# TEST 16: Session expiry removes old context
# =============================================================================


class TestSessionExpiry:
    def test_expired_session_returns_none(self):
        # Use a very short TTL (1 second)
        mgr = _create_manager(ttl=1)
        _store_us_air_query(mgr)

        # Context exists immediately
        assert mgr.get_context("conv-1") is not None

        # Simulate time passing by manually setting last_updated_at
        ctx = mgr._sessions["conv-1"]
        ctx.last_updated_at = time.time() - 2  # 2 seconds ago (> 1s TTL)

        # Should be expired now
        assert mgr.get_context("conv-1") is None
        assert mgr.get_conversation_state("conv-1") is None


# =============================================================================
# BONUS: Verify greeting does not overwrite context
# =============================================================================


class TestGreetingProtection:
    def test_greeting_does_not_overwrite(self):
        mgr = _create_manager()
        _store_us_air_query(mgr)

        greeting_cq = CanonicalQuery(
            intent=QueryIntent.GREETING,
            filters=QueryFilters(),
            confidence=1.0,
        )
        mgr.update_after_query(
            "conv-1",
            user_input="hello",
            normalized_input="hello",
            canonical_query=greeting_cq,
        )

        ctx = mgr.get_context("conv-1")
        assert ctx.last_filters.source_ == "US"
        assert ctx.last_canonical_query.intent == QueryIntent.SHIPMENT_QUERY
