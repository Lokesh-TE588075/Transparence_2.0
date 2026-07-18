"""Tests for empty result detection and handling.

Validates:
- Detail empty results (no rows)
- Aggregate empty results (count=0 with null metrics)
- Filter explanation in empty results
- Suggestion generation based on applied filters
- Non-empty results not falsely classified
"""

import sys

sys.path.insert(0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app")

from app.models.canonical_query import CanonicalQuery, DateRange, QueryFilters, QueryIntent
from app.services.result_processor import process_query_result, ProcessedResult
from app.services.sql_template_engine import SQLTemplateResult


def _make_cq(filters=None, intent=QueryIntent.SHIPMENT_QUERY) -> CanonicalQuery:
    return CanonicalQuery(
        intent=intent,
        filters=filters or QueryFilters(),
    )


def _make_template(result_type="detail", template_name="SHIPMENT_SEARCH", notes=None):
    return SQLTemplateResult(
        sql="SELECT ...",
        template_name=template_name,
        result_type=result_type,
        interpretation_notes=notes or [],
    )


# =============================================================================
# TEST 1: Detail empty result returns is_empty=True and no table
# =============================================================================


class TestDetailEmpty:
    def test_empty_rows_detected(self):
        cq = _make_cq(filters=QueryFilters(source_="US"))
        result = process_query_result(
            rows=[],
            columns=["shipment_number_id", "source_"],
            canonical_query=cq,
            sql_used="SELECT ... WHERE source_ = 'US'",
        )
        assert result.is_empty is True
        assert result.metadata.result_type == "empty"
        assert result.metadata.total_matching_rows == 0
        assert result.metadata.displayed_row_count == 0
        assert result.display_rows == []


# =============================================================================
# TEST 2: Aggregate row with shipment_count=0 is classified as empty
# =============================================================================


class TestAggregateEmpty:
    def test_count_zero_is_empty(self):
        cq = _make_cq(filters=QueryFilters(source_="US"))
        template = _make_template(result_type="aggregate", template_name="SUMMARY_AGGREGATE")
        result = process_query_result(
            rows=[{"shipment_count": 0, "total_revenue": None, "avg_chargeable_weight": None}],
            columns=["shipment_count", "total_revenue", "avg_chargeable_weight"],
            canonical_query=cq,
            sql_used="SELECT COUNT(*) ...",
            template_result=template,
        )
        assert result.is_empty is True
        assert result.is_aggregate_empty is True
        assert result.metadata.result_type == "empty"


# =============================================================================
# TEST 3: Aggregate row with count=0 and null revenue not displayed as meaningful
# =============================================================================


class TestAggregateNullNotMeaningful:
    def test_null_metrics_not_treated_as_data(self):
        cq = _make_cq()
        template = _make_template(result_type="aggregate")
        result = process_query_result(
            rows=[{"count": 0, "total_revenue": None, "earliest_eta": None}],
            columns=["count", "total_revenue", "earliest_eta"],
            canonical_query=cq,
            sql_used="SELECT ...",
            template_result=template,
        )
        # Should be empty - no display_rows for user
        assert result.is_empty is True
        assert result.display_rows == []


# =============================================================================
# TEST 4: Empty result with source/mode/date explains all applied filters
# =============================================================================


class TestFiltersExplained:
    def test_all_filters_in_metadata(self):
        cq = _make_cq(
            filters=QueryFilters(
                source_="US",
                transportation_mode_desc="Air transport",
                date_range=DateRange(field="eta", start="2026-07-01", end="2026-07-07"),
            )
        )
        result = process_query_result(
            rows=[],
            columns=["shipment_number_id"],
            canonical_query=cq,
            sql_used="SELECT ...",
        )
        applied = result.metadata.filters_applied
        assert "source" in applied
        assert applied["source"] == "US"
        assert "mode" in applied
        assert applied["mode"] == "Air transport"
        assert "date_range" in applied
        assert "eta" in applied["date_range"]


# =============================================================================
# TEST 5: Empty result with ETA date range includes ETA interpretation note
# =============================================================================


class TestETAInterpretationNote:
    def test_eta_note_included(self):
        cq = _make_cq(
            filters=QueryFilters(
                date_range=DateRange(field="eta", start="2026-07-13", end="2026-07-19"),
            )
        )
        template = _make_template(
            notes=["I interpreted 'to be delivered' using ETA."]
        )
        result = process_query_result(
            rows=[],
            columns=["shipment_number_id"],
            canonical_query=cq,
            sql_used="SELECT ...",
            template_result=template,
        )
        assert any("ETA" in n for n in result.metadata.interpretation_notes)


# =============================================================================
# TEST 6: Empty result with date range creates broaden_date_range suggestion
# =============================================================================


class TestBroadenSuggestion:
    def test_date_range_triggers_broaden(self):
        cq = _make_cq(
            filters=QueryFilters(
                source_="MX",
                date_range=DateRange(field="eta", start="2026-07-01", end="2026-07-07"),
            )
        )
        result = process_query_result(
            rows=[],
            columns=["shipment_number_id"],
            canonical_query=cq,
            sql_used="SELECT ...",
        )
        assert result.metadata.assistant_suggestion == "broaden_date_range"


# =============================================================================
# TEST 7: Empty result with mode (no date) creates remove_mode_filter suggestion
# =============================================================================


class TestRemoveModeSuggestion:
    def test_mode_triggers_remove_mode(self):
        cq = _make_cq(
            filters=QueryFilters(
                source_="US",
                transportation_mode_desc="Air transport",
            )
        )
        result = process_query_result(
            rows=[],
            columns=["shipment_number_id"],
            canonical_query=cq,
            sql_used="SELECT ...",
        )
        assert result.metadata.assistant_suggestion == "remove_mode_filter"


# =============================================================================
# TEST 8: Empty result with no filters creates verify_filters suggestion
# =============================================================================


class TestVerifyFiltersSuggestion:
    def test_no_filters_triggers_verify(self):
        cq = _make_cq(filters=QueryFilters())
        result = process_query_result(
            rows=[],
            columns=["shipment_number_id"],
            canonical_query=cq,
            sql_used="SELECT ...",
        )
        assert result.metadata.assistant_suggestion == "verify_filters"


# =============================================================================
# TEST 9: Non-empty aggregate with shipment_count > 0 is NOT empty
# =============================================================================


class TestNonEmptyAggregate:
    def test_count_positive_not_empty(self):
        cq = _make_cq()
        template = _make_template(result_type="aggregate")
        result = process_query_result(
            rows=[{"shipment_count": 42, "total_revenue": 150000.0}],
            columns=["shipment_count", "total_revenue"],
            canonical_query=cq,
            sql_used="SELECT ...",
            template_result=template,
        )
        assert result.is_empty is False
        assert result.is_aggregate_empty is False
        assert result.metadata.result_type == "aggregate"


# =============================================================================
# TEST 10: Non-empty detail result is NOT classified as empty
# =============================================================================


class TestNonEmptyDetail:
    def test_rows_present_not_empty(self):
        cq = _make_cq(filters=QueryFilters(source_="US"))
        rows = [
            {"shipment_number_id": "123", "source_": "US"},
            {"shipment_number_id": "456", "source_": "US"},
        ]
        result = process_query_result(
            rows=rows,
            columns=["shipment_number_id", "source_"],
            canonical_query=cq,
            sql_used="SELECT ...",
        )
        assert result.is_empty is False
        assert result.metadata.total_matching_rows == 2
        assert result.metadata.displayed_row_count == 2
