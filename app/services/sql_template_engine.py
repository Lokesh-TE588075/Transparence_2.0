"""Deterministic SQL Template Engine for the TransparencE chatbot.

Generates Databricks SQL from CanonicalQuery objects for common,
high-confidence shipment queries without requiring LLM intervention.

Benefits:
- Consistent SQL for identical business questions
- No LLM latency for common patterns
- Auditable, predictable output
- Enforces business rules (revenue defaults, delay logic, etc.)

Templates:
- DIRECT_LOOKUP: Search by shipment ID across all ID columns
- SHIPMENT_SEARCH: Filter by source/dest/mode/status/date
- IN_TRANSIT/COMPLETED/DELAYED: Status-based filters
- UPCOMING_DELIVERY: ETA-based date range queries
- TOP_REVENUE: Revenue-sorted detail queries
- COUNT_BY_MODE/COUNT_BY_BU: Grouped aggregates
- LANE_ANALYSIS: Source-destination lane grouping
- SUMMARY_AGGREGATE: Count/sum/avg/min/max metrics
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from app.models.canonical_query import CanonicalQuery, DateRange, QueryFilters, QueryIntent

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================

# Default table (can be overridden via constructor/parameter)
DEFAULT_TABLE = "onedata_fn_ion_dev.ion_l0_raw.lbn_with_scorecard"

# All ID columns to search for direct lookup
ID_SEARCH_COLUMNS = [
    "shipment_number_id",
    "waybill",
    "delivery_document_id",
    "customer_purchase_order_id",
    "sales_order_number",
    "hbl",
]

# Approved detail columns (never SELECT *)
DETAIL_COLUMNS = [
    "shipment_number_id",
    "part_number",
    "source_",
    "destination",
    "transportation_mode_desc",
    "execution_status",
    "business_unit_id",
    "actual_pgi_date",
    "eta",
    "ata",
    "final_gr_date",
    "sales_functional_currency_amount",
    "chargeable_weight",
    "currency_code",
]

# Extended detail columns for direct lookup (show more info)
DIRECT_LOOKUP_COLUMNS = [
    "shipment_number_id",
    "waybill",
    "delivery_document_id",
    "customer_purchase_order_id",
    "sales_order_number",
    "hbl",
    "part_number",
    "source_",
    "destination",
    "transportation_mode_desc",
    "execution_status",
    "business_unit_id",
    "actual_pgi_date",
    "eta",
    "ata",
    "final_gr_date",
    "sales_functional_currency_amount",
    "chargeable_weight",
    "currency_code",
    "ffw",
]

# Status groupings
STATUS_COMPLETED = ("Delivered", "Completed")

# Default row limits
DEFAULT_DETAIL_LIMIT = 500
DEFAULT_AGGREGATE_LIMIT = 100


# =============================================================================
# RESULT MODEL
# =============================================================================


@dataclass
class SQLTemplateResult:
    """Result of deterministic SQL generation."""

    sql: str
    template_name: str
    parameters: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    interpretation_notes: List[str] = field(default_factory=list)
    result_type: str = "detail"  # detail | aggregate | direct_lookup | summary
    confidence: float = 1.0
    requires_llm_fallback: bool = False


# =============================================================================
# DATE PHRASE INTERPRETER
# =============================================================================


def interpret_date_phrase(
    phrase: str, reference_date: Optional[date] = None
) -> Optional[DateRange]:
    """Convert a raw date phrase to a DateRange.

    Uses SQL expressions (CURRENT_DATE()) in production SQL,
    but supports reference_date for deterministic testing.

    Args:
        phrase: Raw date phrase (e.g., "last week", "next 30 days")
        reference_date: Optional reference date for testing.

    Returns:
        DateRange or None if phrase not recognized.
    """
    phrase_lower = phrase.lower().strip()
    ref = reference_date or date.today()

    # Week-based
    if phrase_lower in ("this week",):
        # Monday to Sunday of current week
        start = ref - timedelta(days=ref.weekday())
        end = start + timedelta(days=6)
        return DateRange(field="eta", start=start.isoformat(), end=end.isoformat())

    if phrase_lower in ("last week",):
        start = ref - timedelta(days=ref.weekday() + 7)
        end = start + timedelta(days=6)
        return DateRange(field="eta", start=start.isoformat(), end=end.isoformat())

    if phrase_lower in ("next week",):
        start = ref + timedelta(days=(7 - ref.weekday()))
        end = start + timedelta(days=6)
        return DateRange(field="eta", start=start.isoformat(), end=end.isoformat())

    # Day-based
    if phrase_lower in ("past 7 days", "last 7 days"):
        start = ref - timedelta(days=7)
        return DateRange(field="eta", start=start.isoformat(), end=ref.isoformat())

    if phrase_lower in ("last 30 days", "past 30 days"):
        start = ref - timedelta(days=30)
        return DateRange(field="eta", start=start.isoformat(), end=ref.isoformat())

    if phrase_lower in ("next 30 days",):
        end = ref + timedelta(days=30)
        return DateRange(field="eta", start=ref.isoformat(), end=end.isoformat())

    # Month-based
    if phrase_lower in ("this month",):
        start = ref.replace(day=1)
        if ref.month == 12:
            end = ref.replace(year=ref.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            end = ref.replace(month=ref.month + 1, day=1) - timedelta(days=1)
        return DateRange(field="eta", start=start.isoformat(), end=end.isoformat())

    if phrase_lower in ("last month",):
        first_of_this_month = ref.replace(day=1)
        end = first_of_this_month - timedelta(days=1)
        start = end.replace(day=1)
        return DateRange(field="eta", start=start.isoformat(), end=end.isoformat())

    return None


# =============================================================================
# MAIN ENGINE
# =============================================================================


def generate_sql_from_canonical(
    canonical_query: CanonicalQuery,
    table_name: str = DEFAULT_TABLE,
    reference_date: Optional[date] = None,
) -> SQLTemplateResult:
    """Generate deterministic Databricks SQL from a CanonicalQuery.

    Args:
        canonical_query: The structured query from Phase 2.
        table_name: Fully qualified table name.
        reference_date: Optional date override for testing.

    Returns:
        SQLTemplateResult with generated SQL and metadata.
    """
    intent = canonical_query.intent
    filters = canonical_query.filters or QueryFilters()

    # Route to appropriate template
    if intent == QueryIntent.DIRECT_LOOKUP or (
        filters.shipment_ids and len(filters.shipment_ids) > 0
    ):
        return _template_direct_lookup(filters, table_name, canonical_query)

    if intent in (QueryIntent.SHIPMENT_QUERY, QueryIntent.FOLLOW_UP_FILTER):
        return _route_shipment_query(canonical_query, filters, table_name, reference_date)

    # Non-SQL intents return fallback
    if intent in (
        QueryIntent.GREETING,
        QueryIntent.EXIT,
        QueryIntent.OFF_TOPIC,
        QueryIntent.ABUSIVE_OR_INAPPROPRIATE,
        QueryIntent.CLARIFICATION_NEEDED,
        QueryIntent.SUMMARIZE_LAST_RESULT,
        QueryIntent.DOWNLOAD_LAST_RESULT,
    ):
        return SQLTemplateResult(
            sql="",
            template_name="NO_SQL_REQUIRED",
            result_type="detail",
            confidence=1.0,
            requires_llm_fallback=False,
        )

    # Fallback for anything else
    return SQLTemplateResult(
        sql="",
        template_name="UNKNOWN",
        result_type="detail",
        confidence=0.0,
        requires_llm_fallback=True,
        warnings=[f"No template for intent: {intent}"],
    )


# =============================================================================
# TEMPLATE: DIRECT LOOKUP
# =============================================================================


def _template_direct_lookup(
    filters: QueryFilters, table_name: str, cq: CanonicalQuery
) -> SQLTemplateResult:
    """Search all ID columns for given shipment identifiers."""
    ids = filters.shipment_ids or []
    if not ids:
        return SQLTemplateResult(
            sql="",
            template_name="DIRECT_LOOKUP",
            requires_llm_fallback=True,
            warnings=["No shipment IDs provided for direct lookup"],
        )

    escaped_ids = [_escape_value(str(sid)) for sid in ids]
    id_list = ", ".join(f"'{v}'" for v in escaped_ids)

    # Build OR conditions across all ID columns
    or_conditions = []
    for col in ID_SEARCH_COLUMNS:
        or_conditions.append(f"CAST({col} AS STRING) IN ({id_list})")

    where_clause = " OR ".join(or_conditions)
    columns_str = ", ".join(DIRECT_LOOKUP_COLUMNS)

    sql = (
        f"SELECT {columns_str}\n"
        f"FROM {table_name}\n"
        f"WHERE {where_clause}\n"
        f"LIMIT {DEFAULT_DETAIL_LIMIT}"
    )

    return SQLTemplateResult(
        sql=sql,
        template_name="DIRECT_LOOKUP",
        parameters={"ids": id_list},
        result_type="direct_lookup",
        confidence=1.0,
    )


# =============================================================================
# ROUTING: SHIPMENT QUERY
# =============================================================================


def _route_shipment_query(
    cq: CanonicalQuery,
    filters: QueryFilters,
    table_name: str,
    reference_date: Optional[date],
) -> SQLTemplateResult:
    """Route SHIPMENT_QUERY to the best template based on metrics/group_by."""

    # Check if this is an aggregate query
    if cq.group_by:
        if "transportation_mode_desc" in cq.group_by:
            return _template_count_by_mode(filters, table_name, cq, reference_date)
        if "business_unit_id" in cq.group_by:
            return _template_count_by_bu(filters, table_name, cq, reference_date)
        if "source_" in cq.group_by and "destination" in cq.group_by:
            return _template_lane_analysis(filters, table_name, cq, reference_date)

    # Check for summary aggregate
    if "count" in cq.metrics or "summary" in cq.metrics:
        return _template_summary_aggregate(filters, table_name, cq, reference_date)

    # Check for top revenue
    _revenue_metric_names = {"revenue", "budget_revenue", "budget_rate", "top_revenue"}
    if (_revenue_metric_names & set(cq.metrics)) and any(
        s.get("direction") == "DESC" for s in cq.sort
    ):
        return _template_top_revenue(filters, table_name, cq, reference_date)

    # Default: detail search with filters
    return _template_shipment_search(filters, table_name, cq, reference_date)


# =============================================================================
# TEMPLATE: SHIPMENT SEARCH (detail)
# =============================================================================


def _template_shipment_search(
    filters: QueryFilters,
    table_name: str,
    cq: CanonicalQuery,
    reference_date: Optional[date],
) -> SQLTemplateResult:
    """Generate detail query with safe column list and filters."""
    where_parts, notes, params = _build_where_clause(filters, reference_date)

    columns_str = ", ".join(DETAIL_COLUMNS)
    where_str = "\n  AND ".join(where_parts) if where_parts else "1=1"
    limit = min(cq.limit or DEFAULT_DETAIL_LIMIT, DEFAULT_DETAIL_LIMIT)

    sql = (
        f"SELECT {columns_str}\n"
        f"FROM {table_name}\n"
        f"WHERE {where_str}\n"
        f"ORDER BY actual_pgi_date DESC NULLS LAST\n"
        f"LIMIT {limit}"
    )

    return SQLTemplateResult(
        sql=sql,
        template_name="SHIPMENT_SEARCH",
        parameters=params,
        interpretation_notes=notes,
        result_type="detail",
        confidence=0.9,
    )


# =============================================================================
# TEMPLATE: SUMMARY AGGREGATE
# =============================================================================


def _template_summary_aggregate(
    filters: QueryFilters,
    table_name: str,
    cq: CanonicalQuery,
    reference_date: Optional[date],
) -> SQLTemplateResult:
    """Generate summary aggregate with count, revenue, weight, ETA range."""
    where_parts, notes, params = _build_where_clause(filters, reference_date)
    where_str = "\n  AND ".join(where_parts) if where_parts else "1=1"

    sql = (
        f"SELECT\n"
        f"  COUNT(*) AS shipment_count,\n"
        f"  SUM(sales_functional_currency_amount) AS total_revenue,\n"
        f"  AVG(chargeable_weight) AS avg_chargeable_weight,\n"
        f"  MIN(eta) AS earliest_eta,\n"
        f"  MAX(eta) AS latest_eta\n"
        f"FROM {table_name}\n"
        f"WHERE {where_str}"
    )

    notes.append(
        "Aggregate query may return 1 row with shipment_count=0 if no matches. "
        "Treat shipment_count=0 as empty_result."
    )

    return SQLTemplateResult(
        sql=sql,
        template_name="SUMMARY_AGGREGATE",
        parameters=params,
        interpretation_notes=notes,
        result_type="aggregate",
        confidence=0.95,
    )


# =============================================================================
# TEMPLATE: TOP REVENUE
# =============================================================================


def _template_top_revenue(
    filters: QueryFilters,
    table_name: str,
    cq: CanonicalQuery,
    reference_date: Optional[date],
) -> SQLTemplateResult:
    """Generate top-revenue query sorted by revenue DESC."""
    where_parts, notes, params = _build_where_clause(filters, reference_date)
    where_str = "\n  AND ".join(where_parts) if where_parts else "1=1"

    # Determine revenue column
    revenue_col = _get_revenue_column(cq)
    if revenue_col == "sales_budget_rate_amount":
        notes.append("Using budget-rate revenue as explicitly requested.")

    limit = min(cq.limit or 20, DEFAULT_DETAIL_LIMIT)

    sql = (
        f"SELECT shipment_number_id, source_, destination, "
        f"transportation_mode_desc, {revenue_col}, currency_code\n"
        f"FROM {table_name}\n"
        f"WHERE {where_str}\n"
        f"ORDER BY {revenue_col} DESC NULLS LAST\n"
        f"LIMIT {limit}"
    )

    return SQLTemplateResult(
        sql=sql,
        template_name="TOP_REVENUE",
        parameters=params,
        interpretation_notes=notes,
        result_type="detail",
        confidence=0.9,
    )


# =============================================================================
# TEMPLATE: COUNT BY MODE
# =============================================================================


def _template_count_by_mode(
    filters: QueryFilters,
    table_name: str,
    cq: CanonicalQuery,
    reference_date: Optional[date],
) -> SQLTemplateResult:
    """Group shipments by transportation mode."""
    where_parts, notes, params = _build_where_clause(filters, reference_date)
    where_str = "\n  AND ".join(where_parts) if where_parts else "1=1"

    sql = (
        f"SELECT transportation_mode_desc,\n"
        f"  COUNT(*) AS shipment_count,\n"
        f"  SUM(sales_functional_currency_amount) AS total_revenue\n"
        f"FROM {table_name}\n"
        f"WHERE {where_str}\n"
        f"GROUP BY transportation_mode_desc\n"
        f"ORDER BY shipment_count DESC"
    )

    return SQLTemplateResult(
        sql=sql,
        template_name="COUNT_BY_MODE",
        parameters=params,
        interpretation_notes=notes,
        result_type="aggregate",
        confidence=0.95,
    )


# =============================================================================
# TEMPLATE: COUNT BY BU
# =============================================================================


def _template_count_by_bu(
    filters: QueryFilters,
    table_name: str,
    cq: CanonicalQuery,
    reference_date: Optional[date],
) -> SQLTemplateResult:
    """Group shipments by business unit."""
    where_parts, notes, params = _build_where_clause(filters, reference_date)
    where_str = "\n  AND ".join(where_parts) if where_parts else "1=1"

    sql = (
        f"SELECT business_unit_id,\n"
        f"  COUNT(*) AS shipment_count,\n"
        f"  SUM(sales_functional_currency_amount) AS total_revenue\n"
        f"FROM {table_name}\n"
        f"WHERE {where_str}\n"
        f"GROUP BY business_unit_id\n"
        f"ORDER BY shipment_count DESC"
    )

    return SQLTemplateResult(
        sql=sql,
        template_name="COUNT_BY_BU",
        parameters=params,
        interpretation_notes=notes,
        result_type="aggregate",
        confidence=0.95,
    )


# =============================================================================
# TEMPLATE: LANE ANALYSIS
# =============================================================================


def _template_lane_analysis(
    filters: QueryFilters,
    table_name: str,
    cq: CanonicalQuery,
    reference_date: Optional[date],
) -> SQLTemplateResult:
    """Group shipments by source-destination lane."""
    where_parts, notes, params = _build_where_clause(filters, reference_date)
    where_str = "\n  AND ".join(where_parts) if where_parts else "1=1"

    sql = (
        f"SELECT source_, destination,\n"
        f"  COUNT(*) AS shipment_count,\n"
        f"  SUM(sales_functional_currency_amount) AS total_revenue\n"
        f"FROM {table_name}\n"
        f"WHERE {where_str}\n"
        f"GROUP BY source_, destination\n"
        f"ORDER BY shipment_count DESC\n"
        f"LIMIT {DEFAULT_AGGREGATE_LIMIT}"
    )

    return SQLTemplateResult(
        sql=sql,
        template_name="LANE_ANALYSIS",
        parameters=params,
        interpretation_notes=notes,
        result_type="aggregate",
        confidence=0.9,
    )


# =============================================================================
# WHERE CLAUSE BUILDER
# =============================================================================


def _build_where_clause(
    filters: QueryFilters, reference_date: Optional[date] = None
) -> Tuple[List[str], List[str], Dict[str, str]]:
    """Build WHERE clause parts from QueryFilters.

    Returns:
        (where_parts, interpretation_notes, parameters)
    """
    parts: List[str] = []
    notes: List[str] = []
    params: Dict[str, str] = {}

    # Source country
    if filters.source_:
        escaped = _escape_value(filters.source_)
        parts.append(f"source_ = '{escaped}'")
        params["source_"] = filters.source_

    # Destination country
    if filters.destination:
        escaped = _escape_value(filters.destination)
        parts.append(f"destination = '{escaped}'")
        params["destination"] = filters.destination

    # Transportation mode
    if filters.transportation_mode_desc:
        escaped = _escape_value(filters.transportation_mode_desc)
        parts.append(f"transportation_mode_desc = '{escaped}'")
        params["mode"] = filters.transportation_mode_desc

    # Business unit
    if filters.business_unit_id:
        escaped = _escape_value(filters.business_unit_id)
        parts.append(f"business_unit_id = '{escaped}'")
        params["bu"] = filters.business_unit_id

    # Status category
    if filters.status_category:
        status_sql, status_note = _build_status_filter(filters.status_category)
        if status_sql:
            parts.append(status_sql)
        if status_note:
            notes.append(status_note)
        params["status_category"] = filters.status_category

    # Explicit execution status values
    if filters.execution_status_values:
        escaped_vals = [_escape_value(v) for v in filters.execution_status_values]
        in_list = ", ".join(f"'{v}'" for v in escaped_vals)
        parts.append(f"execution_status IN ({in_list})")

    # Date range
    if filters.date_range:
        date_parts, date_notes = _build_date_filter(filters.date_range)
        parts.extend(date_parts)
        notes.extend(date_notes)

    return parts, notes, params


def _build_status_filter(status_category: str) -> Tuple[str, str]:
    """Build WHERE clause for status category."""
    completed_list = ", ".join(f"'{s}'" for s in STATUS_COMPLETED)

    if status_category == "completed":
        return f"execution_status IN ({completed_list})", ""

    if status_category == "in_transit":
        return f"execution_status NOT IN ({completed_list})", ""

    if status_category == "delayed":
        # Active delayed: not completed AND eta < today
        return (
            f"execution_status NOT IN ({completed_list})\n"
            f"  AND eta IS NOT NULL\n"
            f"  AND DATE(eta) < CURRENT_DATE()",
            "Showing currently delayed shipments (ETA in the past, not yet delivered).",
        )

    return "", ""


def _build_date_filter(date_range: DateRange) -> Tuple[List[str], List[str]]:
    """Build date range WHERE clause parts."""
    parts: List[str] = []
    notes: List[str] = []
    field = date_range.field or "eta"

    if date_range.start:
        escaped = _escape_value(date_range.start)
        parts.append(f"DATE({field}) >= DATE('{escaped}')")

    if date_range.end:
        escaped = _escape_value(date_range.end)
        parts.append(f"DATE({field}) <= DATE('{escaped}')")

    if field == "eta":
        notes.append("I interpreted 'to be delivered' using ETA.")

    return parts, notes


# =============================================================================
# HELPERS
# =============================================================================


def _escape_value(value: str) -> str:
    """Escape single quotes in SQL string values."""
    if value is None:
        return ""
    return value.replace("'", "''")


def _get_revenue_column(cq: CanonicalQuery) -> str:
    """Determine which revenue column to use.

    Default: sales_functional_currency_amount
    Budget-rate: only if explicitly in metrics
    """
    if "budget_revenue" in cq.metrics or "budget_rate" in cq.metrics:
        return "sales_budget_rate_amount"
    return "sales_functional_currency_amount"
