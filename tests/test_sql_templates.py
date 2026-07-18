"""Tests for the deterministic SQL template engine.

Validates:
- Direct lookup across all 6 ID columns
- Source/destination/mode filters
- Status-based filters (in_transit, completed, delayed)
- Date range queries with interpretation notes
- Revenue column selection (default vs budget-rate)
- Grouped aggregates (mode, BU, lane)
- SQL safety (no SELECT *, always LIMIT, escaping)
- LLM fallback for unknown intents
"""

import sys
from datetime import date

sys.path.insert(0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app")

from app.models.canonical_query import CanonicalQuery, DateRange, QueryFilters, QueryIntent
from app.services.sql_template_engine import (
    generate_sql_from_canonical,
    interpret_date_phrase,
    DEFAULT_TABLE,
    SQLTemplateResult,
)

TEST_TABLE = "test_catalog.test_schema.shipments"


def _make_cq(
    intent=QueryIntent.SHIPMENT_QUERY,
    filters=None,
    metrics=None,
    group_by=None,
    sort=None,
    limit=500,
) -> CanonicalQuery:
    """Helper to build CanonicalQuery for tests."""
    return CanonicalQuery(
        intent=intent,
        filters=filters or QueryFilters(),
        metrics=metrics or [],
        group_by=group_by or [],
        sort=sort or [],
        limit=limit,
    )


# =============================================================================
# TEST 1: Direct lookup searches all six ID columns
# =============================================================================


class TestDirectLookup:
    def test_searches_all_id_columns(self):
        cq = _make_cq(
            intent=QueryIntent.DIRECT_LOOKUP,
            filters=QueryFilters(shipment_ids=["4110279735"]),
        )
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert result.template_name == "DIRECT_LOOKUP"
        assert result.result_type == "direct_lookup"
        assert "shipment_number_id" in result.sql
        assert "waybill" in result.sql
        assert "delivery_document_id" in result.sql
        assert "customer_purchase_order_id" in result.sql
        assert "sales_order_number" in result.sql
        assert "hbl" in result.sql
        assert "'4110279735'" in result.sql
        assert "LIMIT" in result.sql


# =============================================================================
# TEST 2: Mexico source generates source_ = 'MX'
# =============================================================================


class TestSourceFilter:
    def test_mexico_source(self):
        cq = _make_cq(filters=QueryFilters(source_="MX"))
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert "source_ = 'MX'" in result.sql
        assert result.template_name == "SHIPMENT_SEARCH"


# =============================================================================
# TEST 3: Mexico to Czech generates source_ and destination
# =============================================================================


class TestSourceAndDestination:
    def test_mexico_to_czech(self):
        cq = _make_cq(filters=QueryFilters(source_="MX", destination="CZ"))
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert "source_ = 'MX'" in result.sql
        assert "destination = 'CZ'" in result.sql


# =============================================================================
# TEST 4: US via air generates source + mode
# =============================================================================


class TestSourceAndMode:
    def test_us_via_air(self):
        cq = _make_cq(
            filters=QueryFilters(
                source_="US",
                transportation_mode_desc="Air transport",
            )
        )
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert "source_ = 'US'" in result.sql
        assert "transportation_mode_desc = 'Air transport'" in result.sql


# =============================================================================
# TEST 5: In-transit follow-up preserves source/mode from merged canonical
# =============================================================================


class TestFollowUpPreservesFilters:
    def test_in_transit_with_previous_source_mode(self):
        """Simulates merged canonical query from follow-up resolution."""
        cq = _make_cq(
            filters=QueryFilters(
                source_="US",
                transportation_mode_desc="Air transport",
                status_category="in_transit",
            )
        )
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert "source_ = 'US'" in result.sql
        assert "transportation_mode_desc = 'Air transport'" in result.sql
        assert "NOT IN ('Delivered', 'Completed')" in result.sql


# =============================================================================
# TEST 6: Completed filter
# =============================================================================


class TestCompletedFilter:
    def test_completed_uses_in_delivered_completed(self):
        cq = _make_cq(filters=QueryFilters(status_category="completed"))
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert "execution_status IN ('Delivered', 'Completed')" in result.sql


# =============================================================================
# TEST 7: In-transit filter
# =============================================================================


class TestInTransitFilter:
    def test_in_transit_uses_not_in(self):
        cq = _make_cq(filters=QueryFilters(status_category="in_transit"))
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert "execution_status NOT IN ('Delivered', 'Completed')" in result.sql


# =============================================================================
# TEST 8: Delayed active shipments
# =============================================================================


class TestDelayedFilter:
    def test_delayed_uses_eta_and_current_date(self):
        cq = _make_cq(filters=QueryFilters(status_category="delayed"))
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert "NOT IN ('Delivered', 'Completed')" in result.sql
        assert "eta IS NOT NULL" in result.sql
        assert "DATE(eta) < CURRENT_DATE()" in result.sql


# =============================================================================
# TEST 9: Next week delivery uses ETA and includes interpretation note
# =============================================================================


class TestUpcomingDelivery:
    def test_next_week_uses_eta_range(self):
        ref = date(2026, 7, 9)  # Wednesday
        dr = interpret_date_phrase("next week", reference_date=ref)
        assert dr is not None
        assert dr.field == "eta"

        cq = _make_cq(filters=QueryFilters(date_range=dr))
        result = generate_sql_from_canonical(cq, TEST_TABLE, reference_date=ref)

        assert "DATE(eta) >=" in result.sql
        assert "DATE(eta) <=" in result.sql
        # Interpretation note about ETA
        assert any("ETA" in note for note in result.interpretation_notes)


# =============================================================================
# TEST 10: Top revenue uses sales_functional_currency_amount by default
# =============================================================================


class TestTopRevenue:
    def test_default_revenue_column(self):
        cq = _make_cq(
            metrics=["revenue"],
            sort=[{"field": "revenue", "direction": "DESC"}],
            limit=10,
        )
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert "sales_functional_currency_amount" in result.sql
        assert "sales_budget_rate_amount" not in result.sql
        assert result.template_name == "TOP_REVENUE"


# =============================================================================
# TEST 11: Budget revenue only if explicitly requested
# =============================================================================


class TestBudgetRevenue:
    def test_budget_revenue_explicit(self):
        cq = _make_cq(
            metrics=["budget_revenue"],
            sort=[{"field": "revenue", "direction": "DESC"}],
            limit=10,
        )
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert "sales_budget_rate_amount" in result.sql
        assert any("budget" in n.lower() for n in result.interpretation_notes)


# =============================================================================
# TEST 12: Count by mode groups by transportation_mode_desc
# =============================================================================


class TestCountByMode:
    def test_groups_by_mode(self):
        cq = _make_cq(group_by=["transportation_mode_desc"])
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert result.template_name == "COUNT_BY_MODE"
        assert "GROUP BY transportation_mode_desc" in result.sql
        assert "shipment_count" in result.sql
        assert result.result_type == "aggregate"


# =============================================================================
# TEST 13: Count by BU groups by business_unit_id
# =============================================================================


class TestCountByBU:
    def test_groups_by_bu(self):
        cq = _make_cq(group_by=["business_unit_id"])
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert result.template_name == "COUNT_BY_BU"
        assert "GROUP BY business_unit_id" in result.sql
        assert "shipment_count" in result.sql


# =============================================================================
# TEST 14: Lane analysis groups by source_, destination
# =============================================================================


class TestLaneAnalysis:
    def test_groups_by_source_destination(self):
        cq = _make_cq(group_by=["source_", "destination"])
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert result.template_name == "LANE_ANALYSIS"
        assert "GROUP BY source_, destination" in result.sql
        assert "shipment_count" in result.sql
        assert "total_revenue" in result.sql


# =============================================================================
# TEST 15: Detail queries never use SELECT *
# =============================================================================


class TestNoSelectStar:
    def test_detail_no_select_star(self):
        cq = _make_cq(filters=QueryFilters(source_="US"))
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert "SELECT *" not in result.sql
        assert "SELECT" in result.sql

    def test_direct_lookup_no_select_star(self):
        cq = _make_cq(
            intent=QueryIntent.DIRECT_LOOKUP,
            filters=QueryFilters(shipment_ids=["123456"]),
        )
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert "SELECT *" not in result.sql


# =============================================================================
# TEST 16: Detail queries always include LIMIT
# =============================================================================


class TestAlwaysLimit:
    def test_detail_has_limit(self):
        cq = _make_cq(filters=QueryFilters(source_="MX"))
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert "LIMIT" in result.sql

    def test_direct_lookup_has_limit(self):
        cq = _make_cq(
            intent=QueryIntent.DIRECT_LOOKUP,
            filters=QueryFilters(shipment_ids=["999"]),
        )
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert "LIMIT" in result.sql


# =============================================================================
# TEST 17: SQL escapes single quotes safely
# =============================================================================


class TestSQLEscaping:
    def test_single_quote_escaped(self):
        cq = _make_cq(
            intent=QueryIntent.DIRECT_LOOKUP,
            filters=QueryFilters(shipment_ids=["O'Brien"]),
        )
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        # Should be escaped as O''Brien, not raw O'Brien
        assert "O''Brien" in result.sql
        assert "O'Brien'" not in result.sql  # No unescaped quote

    def test_source_with_quote(self):
        cq = _make_cq(filters=QueryFilters(source_="test'value"))
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert "test''value" in result.sql


# =============================================================================
# TEST 18: Unknown intent returns requires_llm_fallback
# =============================================================================


class TestLLMFallback:
    def test_unknown_intent_fallback(self):
        cq = _make_cq(intent="SOME_UNKNOWN_INTENT")
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert result.requires_llm_fallback is True
        assert result.sql == ""


# =============================================================================
# TEST 19: Summary aggregate marks result_type = aggregate
# =============================================================================


class TestSummaryAggregate:
    def test_result_type_aggregate(self):
        cq = _make_cq(
            metrics=["count"],
            filters=QueryFilters(source_="US"),
        )
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert result.template_name == "SUMMARY_AGGREGATE"
        assert result.result_type == "aggregate"


# =============================================================================
# TEST 20: Summary aggregate includes COUNT(*) AS shipment_count
# =============================================================================


class TestSummaryAggregateCount:
    def test_includes_count(self):
        cq = _make_cq(
            metrics=["summary"],
            filters=QueryFilters(destination="DE"),
        )
        result = generate_sql_from_canonical(cq, TEST_TABLE)

        assert "COUNT(*) AS shipment_count" in result.sql
        assert "destination = 'DE'" in result.sql


# =============================================================================
# BONUS: Date phrase interpretation tests
# =============================================================================


class TestDateInterpretation:
    def test_next_week(self):
        ref = date(2026, 7, 9)  # Wednesday
        dr = interpret_date_phrase("next week", reference_date=ref)
        assert dr is not None
        assert dr.start == "2026-07-13"  # Next Monday
        assert dr.end == "2026-07-19"  # Next Sunday

    def test_last_30_days(self):
        ref = date(2026, 7, 9)
        dr = interpret_date_phrase("last 30 days", reference_date=ref)
        assert dr is not None
        assert dr.start == "2026-06-09"
        assert dr.end == "2026-07-09"

    def test_this_month(self):
        ref = date(2026, 7, 9)
        dr = interpret_date_phrase("this month", reference_date=ref)
        assert dr is not None
        assert dr.start == "2026-07-01"
        assert dr.end == "2026-07-31"

    def test_unknown_phrase_returns_none(self):
        dr = interpret_date_phrase("some random text")
        assert dr is None
