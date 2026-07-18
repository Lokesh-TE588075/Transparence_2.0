"""Tests for the response formatter.

Validates:
- Detail result with preview/capped messaging
- Export key mention in response
- Aggregate results without unsupported inference
- Empty result message with filters in business language
- Empty aggregate not showing null metrics
- Interpretation notes in response
- Summary basis exposure
- Direct lookup formatting
- No unsupported phrases in responses
"""

import sys

sys.path.insert(0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app")

from app.models.canonical_query import CanonicalQuery, DateRange, QueryFilters, QueryIntent
from app.services.result_processor import process_query_result, ProcessedResult
from app.services.response_formatter import format_response, _UNSUPPORTED_PHRASES
from app.services.sql_template_engine import SQLTemplateResult


def _make_cq(filters=None, intent=QueryIntent.SHIPMENT_QUERY) -> CanonicalQuery:
    return CanonicalQuery(intent=intent, filters=filters or QueryFilters())


def _make_template(result_type="detail", template_name="SHIPMENT_SEARCH", notes=None):
    return SQLTemplateResult(
        sql="SELECT ...", template_name=template_name,
        result_type=result_type, interpretation_notes=notes or [],
    )


# =============================================================================
# TEST 1: Detail result with 500 total and 100 displayed says preview/capped
# =============================================================================


class TestDetailPreview:
    def test_capped_message(self):
        cq = _make_cq(filters=QueryFilters(source_="US"))
        rows = [{"id": str(i)} for i in range(500)]
        result = process_query_result(
            rows=rows,
            columns=["id"],
            canonical_query=cq,
            sql_used="SELECT ...",
            total_row_count=500,
            display_limit=100,
        )
        response = format_response(result, cq)

        assert response["row_count"] == 500
        assert response["displayed_row_count"] == 100
        assert "500" in response["message"]
        assert "100" in response["message"]
        assert "preview" in response["message"].lower()
        assert response["is_table"] is True


# =============================================================================
# TEST 2: Detail result with export_key mentions export availability
# =============================================================================


class TestExportMention:
    def test_export_key_in_message(self):
        cq = _make_cq()
        rows = [{"id": str(i)} for i in range(200)]
        result = process_query_result(
            rows=rows,
            columns=["id"],
            canonical_query=cq,
            sql_used="SELECT ...",
            total_row_count=1245,
            display_limit=100,
            export_key="export-abc-123",
        )
        response = format_response(result, cq)

        assert "export" in response["message"].lower()
        assert response["export_key"] == "export-abc-123"


# =============================================================================
# TEST 3: Aggregate result formats without unsupported interpretation
# =============================================================================


class TestAggregateNoInference:
    def test_no_unsupported_phrases(self):
        cq = _make_cq()
        template = _make_template(result_type="aggregate")
        result = process_query_result(
            rows=[{"shipment_count": 42, "total_revenue": 150000.0, "avg_chargeable_weight": 25.3}],
            columns=["shipment_count", "total_revenue", "avg_chargeable_weight"],
            canonical_query=cq,
            sql_used="SELECT ...",
            template_result=template,
        )
        response = format_response(result, cq)

        msg_lower = response["message"].lower()
        for phrase in _UNSUPPORTED_PHRASES:
            assert phrase.lower() not in msg_lower, (
                f"Unsupported phrase '{phrase}' found in response"
            )


# =============================================================================
# TEST 4: Empty result message includes filters in business language
# =============================================================================


class TestEmptyFiltersBusinessLanguage:
    def test_business_language_filters(self):
        cq = _make_cq(
            filters=QueryFilters(
                source_="US",
                transportation_mode_desc="Air transport",
            )
        )
        result = process_query_result(
            rows=[], columns=["id"], canonical_query=cq, sql_used="SELECT ..."
        )
        response = format_response(result, cq)

        msg = response["message"]
        # Should mention the filter values
        assert "US" in msg
        assert "Air transport" in msg
        assert response["is_table"] is False


# =============================================================================
# TEST 5: Empty aggregate does not show misleading null metrics
# =============================================================================


class TestEmptyAggregateNoNulls:
    def test_no_table_for_empty_aggregate(self):
        cq = _make_cq()
        template = _make_template(result_type="aggregate")
        result = process_query_result(
            rows=[{"shipment_count": 0, "total_revenue": None}],
            columns=["shipment_count", "total_revenue"],
            canonical_query=cq,
            sql_used="SELECT ...",
            template_result=template,
        )
        response = format_response(result, cq)

        # Should NOT display a table with null values
        assert response["is_table"] is False
        assert response["table_data"] is None


# =============================================================================
# TEST 6: Response includes interpretation notes
# =============================================================================


class TestInterpretationNotes:
    def test_notes_in_response(self):
        cq = _make_cq(
            filters=QueryFilters(
                date_range=DateRange(field="eta", start="2026-07-13", end="2026-07-19")
            )
        )
        template = _make_template(notes=["I interpreted 'to be delivered' using ETA."])
        rows = [{"id": "1", "eta": "2026-07-15"}]
        result = process_query_result(
            rows=rows, columns=["id", "eta"],
            canonical_query=cq, sql_used="SELECT ...",
            template_result=template,
        )
        response = format_response(result, cq)

        assert "ETA" in response["message"]
        assert len(response["interpretation_notes"]) > 0


# =============================================================================
# TEST 7: Preview-only summary basis is exposed
# =============================================================================


class TestPreviewBasis:
    def test_preview_only_exposed(self):
        cq = _make_cq()
        rows = [{"id": str(i)} for i in range(200)]
        result = process_query_result(
            rows=rows, columns=["id"],
            canonical_query=cq, sql_used="SELECT ...",
            total_row_count=500, display_limit=100,
        )
        response = format_response(result, cq)

        assert response["summary_basis"] == "preview_only"


# =============================================================================
# TEST 8: Aggregate summary basis is exposed
# =============================================================================


class TestAggregateBasis:
    def test_aggregate_basis_exposed(self):
        cq = _make_cq()
        template = _make_template(result_type="aggregate")
        result = process_query_result(
            rows=[{"shipment_count": 100, "total_revenue": 50000}],
            columns=["shipment_count", "total_revenue"],
            canonical_query=cq, sql_used="SELECT ...",
            template_result=template,
        )
        response = format_response(result, cq)

        assert response["summary_basis"] == "aggregate"


# =============================================================================
# TEST 9: Direct lookup result formats as shipment detail
# =============================================================================


class TestDirectLookupFormat:
    def test_direct_lookup_response(self):
        cq = _make_cq(
            intent=QueryIntent.DIRECT_LOOKUP,
            filters=QueryFilters(shipment_ids=["4110279735"]),
        )
        template = _make_template(result_type="direct_lookup", template_name="DIRECT_LOOKUP")
        rows = [{"shipment_number_id": "4110279735", "source_": "US", "destination": "DE"}]
        result = process_query_result(
            rows=rows, columns=["shipment_number_id", "source_", "destination"],
            canonical_query=cq, sql_used="SELECT ...",
            template_result=template,
        )
        response = format_response(result, cq)

        assert response["is_table"] is True
        assert "detail" in response["message"].lower()
        assert response["row_count"] == 1


# =============================================================================
# TEST 10: No unsupported phrases in any response type
# =============================================================================


class TestNoUnsupportedPhrases:
    def test_detail_result_no_inference(self):
        cq = _make_cq()
        rows = [{"id": "1", "eta": "2026-07-10", "ata": "2026-07-12"}]
        result = process_query_result(
            rows=rows, columns=["id", "eta", "ata"],
            canonical_query=cq, sql_used="SELECT ...",
        )
        response = format_response(result, cq)

        msg_lower = response["message"].lower()
        for phrase in _UNSUPPORTED_PHRASES:
            assert phrase.lower() not in msg_lower

    def test_empty_result_no_inference(self):
        cq = _make_cq(filters=QueryFilters(source_="US"))
        result = process_query_result(
            rows=[], columns=["id"],
            canonical_query=cq, sql_used="SELECT ...",
        )
        response = format_response(result, cq)

        msg_lower = response["message"].lower()
        for phrase in _UNSUPPORTED_PHRASES:
            assert phrase.lower() not in msg_lower
