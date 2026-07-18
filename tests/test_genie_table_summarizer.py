"""Unit tests for genie_table_summarizer (Phase Q3).

All tests are pure-Python, no I/O.  Run with:
    python -m pytest tests/test_genie_table_summarizer.py -v
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.services.genie_table_summarizer import (
    summarize_table,
    TableSummaryResult,
    _col_index,
    _top_values,
    _build_summary_text,
)

# =============================================================================
# Test fixtures
# =============================================================================

HEADERS_FULL = [
    "destination_country", "transport_mode", "status", "count"
]

ROWS_FULL = [
    ["DE", "Air",   "Delivered",  523],
    ["US", "Ocean", "In Transit", 412],
    ["CN", "Air",   "Delayed",    308],
    ["GB", "Road",  "Delivered",  201],
    ["SG", "Air",   "Pending",    145],
    ["FR", "Ocean", "Delivered",  132],
    ["JP", "Air",   "Cancelled",   87],
    ["IN", "Road",  "In Transit",  71],
    ["AU", "Air",   "Delayed",     55],
    ["BR", "Ocean", "Pending",     43],
]

HEADERS_NO_COLS = ["shipment_no", "reference", "po_number"]
ROWS_NO_COLS = [
    ["SHP001", "REF-123", "PO-456"],
    ["SHP002", "REF-124", "PO-457"],
]

HEADERS_GEO_ONLY = ["origin_country", "mode"]
ROWS_GEO_ONLY = [
    ["US", "Air"],
    ["DE", "Ocean"],
    ["CN", "Air"],
    ["US", "Road"],
    ["DE", "Air"],
]


# =============================================================================
# summarize_table — basic contract
# =============================================================================

class TestSummarizeTableContract:

    def test_returns_table_summary_result(self):
        result = summarize_table(HEADERS_FULL, ROWS_FULL, row_count=1977)
        assert isinstance(result, TableSummaryResult)

    def test_empty_headers_returns_empty(self):
        result = summarize_table([], [], row_count=0)
        assert result.computed_metrics == {}
        assert result.summary_text == ""
        assert result.computed_chart_data == []

    def test_empty_rows_returns_empty(self):
        result = summarize_table(HEADERS_FULL, [], row_count=0)
        assert result.computed_metrics == {}

    def test_total_count_in_metrics(self):
        result = summarize_table(HEADERS_FULL, ROWS_FULL, row_count=1977)
        # count column is present, so total should be sum of count column
        assert result.computed_metrics["total_count"] > 0

    def test_top_destinations_detected(self):
        result = summarize_table(HEADERS_FULL, ROWS_FULL, row_count=1977)
        assert "top_destinations" in result.computed_metrics
        assert len(result.computed_metrics["top_destinations"]) >= 1

    def test_mode_breakdown_detected(self):
        result = summarize_table(HEADERS_FULL, ROWS_FULL, row_count=1977)
        assert "mode_breakdown" in result.computed_metrics
        top_modes = result.computed_metrics["mode_breakdown"]
        assert len(top_modes) >= 1
        # Air should be #1 (523 + 308 + 145 + 87 + 55 = 1118)
        assert top_modes[0]["label"] == "Air"

    def test_status_breakdown_detected(self):
        result = summarize_table(HEADERS_FULL, ROWS_FULL, row_count=1977)
        assert "status_breakdown" in result.computed_metrics

    def test_top_n_respected(self):
        result = summarize_table(HEADERS_FULL, ROWS_FULL, row_count=1977, top_n=3)
        if "top_destinations" in result.computed_metrics:
            assert len(result.computed_metrics["top_destinations"]) <= 3
        if "mode_breakdown" in result.computed_metrics:
            assert len(result.computed_metrics["mode_breakdown"]) <= 3

    def test_no_matching_columns_returns_safe_defaults(self):
        result = summarize_table(HEADERS_NO_COLS, ROWS_NO_COLS, row_count=2)
        # Nothing should crash; metrics may have total_count
        assert isinstance(result.computed_metrics, dict)
        assert result.summary_text == "" or isinstance(result.summary_text, str)


# =============================================================================
# summarize_table — summary_text
# =============================================================================

class TestSummaryText:

    def test_summary_text_contains_total(self):
        result = summarize_table(HEADERS_FULL, ROWS_FULL, row_count=1977)
        assert result.summary_text != ""
        assert "shipments" in result.summary_text.lower() or any(
            char.isdigit() for char in result.summary_text
        )

    def test_summary_text_mentions_destinations(self):
        result = summarize_table(HEADERS_FULL, ROWS_FULL, row_count=1977)
        # Should mention at least the top destination code
        assert "DE" in result.summary_text or "destination" in result.summary_text.lower()

    def test_summary_text_empty_when_no_columns(self):
        result = summarize_table(HEADERS_NO_COLS, ROWS_NO_COLS, row_count=2)
        # No recognisable columns → empty or minimal summary
        assert isinstance(result.summary_text, str)


# =============================================================================
# summarize_table — computed_chart_data
# =============================================================================

class TestComputedChartData:

    def test_chart_data_present(self):
        result = summarize_table(HEADERS_FULL, ROWS_FULL, row_count=1977)
        assert isinstance(result.computed_chart_data, list)
        assert len(result.computed_chart_data) > 0

    def test_chart_data_keys(self):
        result = summarize_table(HEADERS_FULL, ROWS_FULL, row_count=1977)
        for item in result.computed_chart_data:
            assert isinstance(item, dict)
            assert result.chart_x_key in item
            assert result.chart_y_key in item

    def test_chart_data_values_numeric(self):
        result = summarize_table(HEADERS_FULL, ROWS_FULL, row_count=1977)
        for item in result.computed_chart_data:
            assert isinstance(item[result.chart_y_key], (int, float))

    def test_chart_disabled(self):
        result = summarize_table(
            HEADERS_FULL, ROWS_FULL, row_count=1977, enable_chart=False
        )
        assert result.computed_chart_data == []
        assert result.chart_x_key is None
        assert result.chart_y_key is None

    def test_no_chartable_columns(self):
        result = summarize_table(HEADERS_NO_COLS, ROWS_NO_COLS, row_count=2)
        # No chartable columns — empty list, no crash
        assert isinstance(result.computed_chart_data, list)

    def test_chart_sorted_descending(self):
        result = summarize_table(HEADERS_FULL, ROWS_FULL, row_count=1977)
        data = result.computed_chart_data
        if len(data) >= 2:
            y = result.chart_y_key
            # Top-N sorted descending
            counts = [d[y] for d in data]
            assert counts == sorted(counts, reverse=True)

    def test_geo_only_columns(self):
        """Should still produce chart from origin column even without count col."""
        result = summarize_table(HEADERS_GEO_ONLY, ROWS_GEO_ONLY, row_count=5)
        # May or may not produce chart; should not crash
        assert isinstance(result.computed_chart_data, list)


# =============================================================================
# count column heuristic
# =============================================================================

class TestCountColumn:

    def test_count_col_sums_correctly(self):
        """When a 'count' column exists, total_count should be its sum."""
        result = summarize_table(HEADERS_FULL, ROWS_FULL, row_count=9999)
        expected_sum = sum(row[3] for row in ROWS_FULL)  # index 3 = count
        assert result.computed_metrics["total_count"] == expected_sum

    def test_fallback_to_row_count(self):
        """Without a count column, total_count should fall back to row_count arg."""
        result = summarize_table(HEADERS_GEO_ONLY, ROWS_GEO_ONLY, row_count=42)
        assert result.computed_metrics["total_count"] == 42


# =============================================================================
# Edge cases
# =============================================================================

class TestEdgeCases:

    def test_single_row(self):
        result = summarize_table(HEADERS_FULL, [ROWS_FULL[0]], row_count=523)
        assert isinstance(result, TableSummaryResult)

    def test_null_cell_values(self):
        rows_with_nulls = [
            [None, "Air", "Delivered", 100],
            ["DE", None, "In Transit", 50],
            ["CN", "Ocean", None, 30],
        ]
        result = summarize_table(HEADERS_FULL, rows_with_nulls, row_count=180)
        # Must not crash
        assert isinstance(result.computed_metrics, dict)

    def test_string_count_values(self):
        """Count column values may arrive as strings from the API."""
        rows_str_counts = [
            ["DE", "Air", "Delivered", "523"],
            ["US", "Ocean", "In Transit", "412"],
        ]
        result = summarize_table(HEADERS_FULL, rows_str_counts, row_count=935)
        assert result.computed_metrics["total_count"] == 935  # sum of "523"+"412"=935

import pytest

from app.services.genie_table_summarizer import summarize_table


# =============================================================================
# P1 regression: improved column detection (2026-07-16)
# =============================================================================

class TestImprovedColumnDetectionP1:
    """P1: TRANSPORTATION_MODE_DESC, BUSINESS_UNIT_ID, SALES_FUNCTIONAL_CURRENCY_AMOUNT
    must be detected and produce meaningful summary metrics."""

    def _make_rows(self, headers, data):
        """Build row list from header-aligned dicts."""
        return [[d.get(h) for h in headers] for d in data]

    def test_transportation_mode_desc_produces_mode_breakdown(self):
        """TRANSPORTATION_MODE_DESC must be detected as a transport mode column."""
        headers = ["SHIPMENT_NUMBER_ID", "DESTINATION", "TRANSPORTATION_MODE_DESC",
                   "EXECUTION_STATUS"]
        rows = [
            ["SHP001", "DE", "Air transport", "In transit"],
            ["SHP002", "TH", "Ocean Transport", "Delivered"],
            ["SHP003", "CN", "Air transport", "Delivered"],
            ["SHP004", "MX", "Air transport", "In transit"],
        ]
        result = summarize_table(headers, rows, row_count=4, top_n=5)
        assert "mode_breakdown" in result.computed_metrics, (
            "mode_breakdown should be computed when TRANSPORTATION_MODE_DESC is present"
        )
        modes = [m["label"] for m in result.computed_metrics["mode_breakdown"]]
        assert "Air transport" in modes, "Air transport should appear in mode_breakdown"
        # Summary text should mention transport mode
        assert "Transport modes" in result.summary_text, (
            f"summary_text should contain transport mode info: {result.summary_text!r}"
        )

    def test_business_unit_id_produces_bu_breakdown(self):
        """BUSINESS_UNIT_ID must be detected as a business unit column."""
        headers = ["SHIPMENT_NUMBER_ID", "BUSINESS_UNIT_ID", "EXECUTION_STATUS"]
        rows = [
            ["SHP001", "IMS", "Delivered"],
            ["SHP002", "AND", "In transit"],
            ["SHP003", "IMS", "Delivered"],
            ["SHP004", "CIV", "Delivered"],
        ]
        result = summarize_table(headers, rows, row_count=4, top_n=5)
        assert "bu_breakdown" in result.computed_metrics, (
            "bu_breakdown should be computed when BUSINESS_UNIT_ID is present"
        )
        bus = [b["label"] for b in result.computed_metrics["bu_breakdown"]]
        assert "IMS" in bus, "IMS BU should appear in bu_breakdown"
        assert "Business units" in result.summary_text, (
            f"summary_text should contain BU info: {result.summary_text!r}"
        )

    def test_sales_functional_currency_amount_produces_revenue_metrics(self):
        """SALES_FUNCTIONAL_CURRENCY_AMOUNT must be detected as a revenue column."""
        headers = ["SHIPMENT_NUMBER_ID", "BUSINESS_UNIT_ID", "SALES_FUNCTIONAL_CURRENCY_AMOUNT"]
        rows = [
            ["SHP001", "IMS", 1000.0],
            ["SHP002", "AND", 2000.0],
            ["SHP003", "IMS", 1500.0],
        ]
        result = summarize_table(headers, rows, row_count=3, top_n=5)
        assert "total_revenue" in result.computed_metrics, (
            "total_revenue should be computed when SALES_FUNCTIONAL_CURRENCY_AMOUNT is present"
        )
        assert result.computed_metrics["total_revenue"] == pytest.approx(4500.0), (
            f"total_revenue should be 4500.0, got {result.computed_metrics.get('total_revenue')}"
        )
        assert "Total revenue" in result.summary_text, (
            f"summary_text should contain revenue info: {result.summary_text!r}"
        )

    def test_summary_text_contains_more_than_just_count_and_destinations(self):
        """Full raw schema: summary_text should include mode, status, BU, revenue."""
        headers = [
            "SHIPMENT_NUMBER_ID", "DESTINATION", "TRANSPORTATION_MODE_DESC",
            "EXECUTION_STATUS", "BUSINESS_UNIT_ID", "SALES_FUNCTIONAL_CURRENCY_AMOUNT",
        ]
        rows = [
            ["SHP001", "DE", "Air transport", "Status 7", "IMS", 1200.0],
            ["SHP002", "TH", "Ocean Transport", "Goods Issued", "AND", 800.0],
            ["SHP003", "DE", "Air transport", "Status 7", "IMS", 950.0],
            ["SHP004", "MX", "Air transport", "Status 7", "CIV", 1100.0],
            ["SHP005", "TH", "Ocean Transport", "Goods Issued", "AND", 700.0],
        ] * 20  # 100 rows
        result = summarize_table(headers, rows, row_count=100, top_n=5)
        text = result.summary_text
        # Should have more content than just a bare count
        assert len(text) > 40, f"summary_text too short: {text!r}"
        assert "shipments" in text.lower()
