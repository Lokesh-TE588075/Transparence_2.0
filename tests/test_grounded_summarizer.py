"""Tests for the grounded summarizer (Phase 6).

Validates:
- Empty/detail/aggregate/direct_lookup summaries are grounded
- Preview-only basis is stated clearly
- No unsupported inference phrases leak into summaries
- validate_summary_grounding catches violations
- LLM prompt builder includes anti-hallucination rules
- Quick-summary uses stored metadata without new SQL
"""

import sys

sys.path.insert(0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app")

from app.models.canonical_query import CanonicalQuery, DateRange, QueryFilters, QueryIntent
from app.services.result_processor import ProcessedResult, ResultMetadata, process_query_result
from app.services.result_summarizer import (
    build_llm_summarization_prompt,
    detect_unsupported_claims,
    format_filters_for_summary,
    summarize_processed_result,
    validate_summary_grounding,
)
from app.services.sql_template_engine import SQLTemplateResult


# =============================================================================
# HELPERS
# =============================================================================


def _make_cq(filters=None, intent=QueryIntent.SHIPMENT_QUERY) -> CanonicalQuery:
    return CanonicalQuery(intent=intent, filters=filters or QueryFilters())


def _make_template(result_type="detail", template_name="SHIPMENT_SEARCH", notes=None):
    return SQLTemplateResult(
        sql="SELECT ...", template_name=template_name,
        result_type=result_type, interpretation_notes=notes or [],
    )


def _make_empty_result(filters=None, notes=None, suggestion=None):
    """Create an empty ProcessedResult for testing."""
    f = filters or {}
    return ProcessedResult(
        metadata=ResultMetadata(
            total_matching_rows=0,
            displayed_row_count=0,
            columns_returned=["shipment_number_id"],
            filters_applied=f,
            sql_used="SELECT ...",
            result_type="empty",
            summary_basis="none",
            interpretation_notes=notes or [],
            assistant_suggestion=suggestion,
            empty_reason="No records matched the applied filters.",
        ),
        display_rows=[],
        table_headers=["shipment_number_id"],
        is_empty=True,
    )


def _make_detail_result(
    rows, columns, total=None, display_limit=100, export_key=None,
    filters=None, notes=None, warnings=None,
):
    """Create a detail ProcessedResult."""
    cq = _make_cq(filters=QueryFilters(**(filters or {})))
    template = _make_template(result_type="detail", notes=notes)
    return process_query_result(
        rows=rows, columns=columns, canonical_query=cq,
        sql_used="SELECT ...", template_result=template,
        total_row_count=total, display_limit=display_limit,
        export_key=export_key,
    )


def _make_aggregate_result(rows, columns, filters=None, notes=None):
    """Create an aggregate ProcessedResult."""
    cq = _make_cq(filters=QueryFilters(**(filters or {})))
    template = _make_template(result_type="aggregate", notes=notes)
    return process_query_result(
        rows=rows, columns=columns, canonical_query=cq,
        sql_used="SELECT ...", template_result=template,
    )


# =============================================================================
# TEST 1: Empty result summary states no records and includes filters
# =============================================================================


class TestEmptySummaryFilters:
    def test_includes_filters(self):
        result = _make_empty_result(
            filters={"source": "US", "mode": "Air transport"}
        )
        cq = _make_cq()
        summary = summarize_processed_result(result, cq)

        assert "no records" in summary.lower()
        assert "US" in summary
        assert "Air transport" in summary


# =============================================================================
# TEST 2: Empty result summary includes ETA interpretation note
# =============================================================================


class TestEmptySummaryETA:
    def test_eta_note_in_summary(self):
        result = _make_empty_result(
            filters={"date_range": "eta: 2026-07-13 to 2026-07-19"},
            notes=["I interpreted 'to be delivered' using ETA."],
        )
        cq = _make_cq()
        summary = summarize_processed_result(result, cq)

        assert "ETA" in summary


# =============================================================================
# TEST 3: Empty result summary includes safe suggestion
# =============================================================================


class TestEmptySuggestion:
    def test_broaden_suggestion(self):
        result = _make_empty_result(
            filters={"date_range": "eta: 2026-07-01 to 2026-07-07"},
            suggestion="broaden_date_range",
        )
        cq = _make_cq()
        summary = summarize_processed_result(result, cq)

        assert "broaden" in summary.lower() or "date range" in summary.lower()


# =============================================================================
# TEST 4: Detail summary mentions total rows and displayed rows
# =============================================================================


class TestDetailRowCounts:
    def test_total_and_displayed(self):
        rows = [{"id": str(i)} for i in range(200)]
        result = _make_detail_result(
            rows=rows, columns=["id"],
            total=500, display_limit=100,
        )
        cq = _make_cq()
        summary = summarize_processed_result(result, cq)

        assert "500" in summary
        assert "100" in summary


# =============================================================================
# TEST 5: Detail summary with preview_only explicitly says preview-only
# =============================================================================


class TestDetailPreviewOnly:
    def test_preview_stated(self):
        rows = [{"id": str(i)} for i in range(200)]
        result = _make_detail_result(
            rows=rows, columns=["id"],
            total=500, display_limit=100,
        )
        cq = _make_cq()
        summary = summarize_processed_result(result, cq)

        assert "preview" in summary.lower()
        assert "not necessarily the full" in summary.lower() or "preview rows" in summary.lower()


# =============================================================================
# TEST 6: Detail summary does not contain unsupported inference phrases
# =============================================================================


class TestDetailNoInference:
    def test_no_unsupported_phrases(self):
        rows = [
            {"id": "1", "eta": "2026-07-10", "ata": "2026-07-17"},
            {"id": "2", "eta": "2026-07-11", "ata": "2026-07-18"},
        ]
        result = _make_detail_result(rows=rows, columns=["id", "eta", "ata"])
        cq = _make_cq()
        summary = summarize_processed_result(result, cq)

        # Should NOT say "lead time", "suggesting", etc.
        is_valid, violations = validate_summary_grounding(summary, result)
        assert is_valid, f"Summary had violations: {violations}"


# =============================================================================
# TEST 7: Aggregate summary includes only explicit aggregate values
# =============================================================================


class TestAggregateExplicitValues:
    def test_includes_values(self):
        rows = [{"shipment_count": 42, "total_revenue": 150000.50}]
        result = _make_aggregate_result(
            rows=rows, columns=["shipment_count", "total_revenue"]
        )
        cq = _make_cq()
        summary = summarize_processed_result(result, cq)

        assert "42" in summary
        assert "150" in summary  # Contains the revenue value


# =============================================================================
# TEST 8: Aggregate summary says it is based on aggregate output
# =============================================================================


class TestAggregateBasis:
    def test_aggregate_basis_stated(self):
        rows = [{"shipment_count": 10}]
        result = _make_aggregate_result(
            rows=rows, columns=["shipment_count"]
        )
        cq = _make_cq()
        summary = summarize_processed_result(result, cq)

        assert "aggregate" in summary.lower()


# =============================================================================
# TEST 9: Direct lookup summary only summarizes provided fields
# =============================================================================


class TestDirectLookupFields:
    def test_only_provided_fields(self):
        cq = _make_cq(
            intent=QueryIntent.DIRECT_LOOKUP,
            filters=QueryFilters(shipment_ids=["4110279735"]),
        )
        template = _make_template(
            result_type="direct_lookup", template_name="DIRECT_LOOKUP"
        )
        rows = [{
            "shipment_number_id": "4110279735",
            "source_": "US",
            "destination": "DE",
            "execution_status": "In Transit",
        }]
        result = process_query_result(
            rows=rows,
            columns=["shipment_number_id", "source_", "destination", "execution_status"],
            canonical_query=cq, sql_used="SELECT ...",
            template_result=template,
        )
        summary = summarize_processed_result(result, cq)

        # Should mention actual field values
        assert "4110279735" in summary or "1 matching" in summary.lower()
        assert "US" in summary
        # Should NOT infer anything beyond fields
        is_valid, _ = validate_summary_grounding(summary, result)
        assert is_valid


# =============================================================================
# TEST 10: Summary with total > displayed mentions capped preview
# =============================================================================


class TestCappedPreview:
    def test_capped_stated(self):
        rows = [{"id": str(i)} for i in range(150)]
        result = _make_detail_result(
            rows=rows, columns=["id"],
            total=1245, display_limit=100,
        )
        cq = _make_cq()
        summary = summarize_processed_result(result, cq)

        assert "1,245" in summary or "1245" in summary
        assert "100" in summary


# =============================================================================
# TEST 11: Interpretation notes are included
# =============================================================================


class TestInterpretationNotesIncluded:
    def test_notes_in_summary(self):
        rows = [{"id": "1", "eta": "2026-07-15"}]
        result = _make_detail_result(
            rows=rows, columns=["id", "eta"],
            notes=["I interpreted 'next week delivery' using ETA."],
        )
        cq = _make_cq()
        summary = summarize_processed_result(result, cq)

        assert "ETA" in summary


# =============================================================================
# TEST 12: Warnings are included when user-facing
# =============================================================================


class TestWarningsIncluded:
    def test_warning_in_summary(self):
        cq = _make_cq()
        template = SQLTemplateResult(
            sql="SELECT ...", template_name="SHIPMENT_SEARCH",
            result_type="detail",
            warnings=["Date range exceeds 90 days; results may be large."],
        )
        rows = [{"id": "1"}]
        result = process_query_result(
            rows=rows, columns=["id"],
            canonical_query=cq, sql_used="SELECT ...",
            template_result=template,
        )
        summary = summarize_processed_result(result, cq)

        assert "90 days" in summary or "large" in summary


# =============================================================================
# TEST 13: validate_summary_grounding rejects unsupported claims
# =============================================================================


class TestGroundingRejects:
    def test_rejects_bad_summary(self):
        result = ProcessedResult(
            metadata=ResultMetadata(
                columns_returned=["id", "eta"],
                result_type="detail",
            ),
            table_headers=["id", "eta"],
        )
        bad_summary = (
            "The data suggests a lead time of roughly 7 days, "
            "likely caused by operational bottleneck."
        )
        is_valid, violations = validate_summary_grounding(bad_summary, result)

        assert is_valid is False
        assert len(violations) >= 3  # suggesting, lead time, likely, caused by, bottleneck


# =============================================================================
# TEST 14: validate_summary_grounding accepts factual summary
# =============================================================================


class TestGroundingAccepts:
    def test_accepts_good_summary(self):
        result = ProcessedResult(
            metadata=ResultMetadata(
                columns_returned=["shipment_number_id", "source_", "destination"],
                result_type="detail",
            ),
            table_headers=["shipment_number_id", "source_", "destination"],
        )
        good_summary = (
            "Found 5 matching shipments. "
            "Filters: origin = US, destination = DE."
        )
        is_valid, violations = validate_summary_grounding(good_summary, result)

        assert is_valid is True
        assert violations == []


# =============================================================================
# TEST 15: LLM prompt includes strict anti-hallucination rules
# =============================================================================


class TestLLMPromptRules:
    def test_anti_hallucination_rules_present(self):
        rows = [{"id": "1", "source_": "US"}]
        result = _make_detail_result(rows=rows, columns=["id", "source_"])
        cq = _make_cq()

        prompt = build_llm_summarization_prompt(result, cq, "show me shipments from US")

        # Must contain anti-hallucination instructions
        assert "Do not use prior knowledge" in prompt or "Do NOT use prior knowledge" in prompt
        assert "Do not infer" in prompt or "Do NOT infer" in prompt
        assert "suggesting" in prompt.lower()
        assert "lead time" in prompt.lower()
        assert "trend" in prompt.lower()
        assert "caused by" in prompt.lower()
        assert "concise" in prompt.lower()


# =============================================================================
# TEST 16: Quick-summary uses last ProcessedResult, no new SQL needed
# =============================================================================


class TestQuickSummaryFromMetadata:
    def test_uses_stored_metadata(self):
        """Simulates SUMMARIZE_LAST_RESULT: only needs stored ProcessedResult."""
        # Build a ProcessedResult as if it was stored from a previous query
        stored_result = ProcessedResult(
            metadata=ResultMetadata(
                total_matching_rows=250,
                displayed_row_count=100,
                columns_returned=["shipment_number_id", "source_", "destination"],
                filters_applied={"source": "MX", "mode": "Ocean Transport"},
                sql_used="SELECT ... WHERE source_ = 'MX'",
                result_type="detail",
                summary_basis="preview_only",
                export_key="export-xyz-789",
            ),
            display_rows=[{"shipment_number_id": str(i)} for i in range(100)],
            table_headers=["shipment_number_id", "source_", "destination"],
            is_empty=False,
        )

        # Summarize uses stored metadata — no SQL, no DB, no LLM
        cq = _make_cq(intent=QueryIntent.SUMMARIZE_LAST_RESULT)
        summary = summarize_processed_result(stored_result, cq, use_llm=False)

        # Must mention key facts from metadata
        assert "250" in summary
        assert "100" in summary
        assert "preview" in summary.lower()
        assert "export" in summary.lower()
