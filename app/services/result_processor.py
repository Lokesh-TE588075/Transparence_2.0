"""Result processor for the TransparencE chatbot.

Processes raw SQL query results into structured ProcessedResult objects
that downstream response formatting can safely use.

Key responsibilities:
- Detect truly empty results (including aggregate rows with count=0)
- Distinguish preview rows from total matching rows
- Capture interpretation notes and filter metadata
- Generate safe suggestions for empty results
- Never treat null aggregate metrics as meaningful data
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.models.canonical_query import CanonicalQuery, QueryFilters, QueryIntent
from app.services.sql_template_engine import SQLTemplateResult

logger = logging.getLogger(__name__)

# Column names that indicate a count in aggregate results
_COUNT_COLUMN_NAMES = frozenset({
    "shipment_count", "count", "total_count", "cnt",
    "count(*)", "num_shipments", "record_count",
})


# =============================================================================
# DATA MODELS
# =============================================================================


@dataclass
class ResultMetadata:
    """Metadata about a processed query result."""

    total_matching_rows: int = 0
    displayed_row_count: int = 0
    columns_returned: List[str] = field(default_factory=list)
    filters_applied: Dict[str, str] = field(default_factory=dict)
    sql_used: str = ""
    result_type: str = "detail"  # detail | aggregate | summary | empty | direct_lookup
    summary_basis: str = "none"  # full_result | preview_only | aggregate | none
    export_key: Optional[str] = None
    execution_time_ms: Optional[int] = None
    interpretation_notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    assistant_suggestion: Optional[str] = None
    empty_reason: Optional[str] = None


@dataclass
class ProcessedResult:
    """Fully processed query result ready for response formatting."""

    metadata: ResultMetadata
    display_rows: List[Dict[str, Any]] = field(default_factory=list)
    table_headers: List[str] = field(default_factory=list)
    is_empty: bool = False
    is_aggregate_empty: bool = False
    user_message_hint: Optional[str] = None


# =============================================================================
# MAIN PROCESSOR
# =============================================================================


def process_query_result(
    rows: List[Dict[str, Any]],
    columns: List[str],
    canonical_query: CanonicalQuery,
    sql_used: str,
    template_result: Optional[SQLTemplateResult] = None,
    total_row_count: Optional[int] = None,
    display_limit: int = 100,
    export_key: Optional[str] = None,
    execution_time_ms: Optional[int] = None,
) -> ProcessedResult:
    """Process raw query results into a structured ProcessedResult.

    Args:
        rows: Raw result rows as list of dicts.
        columns: Column names from the result set.
        canonical_query: The canonical query that produced this result.
        sql_used: The SQL that was executed.
        template_result: Optional SQLTemplateResult metadata.
        total_row_count: Total matching rows (if known from separate COUNT).
        display_limit: Maximum rows to include in display_rows.
        export_key: Key for full result export (if available).
        execution_time_ms: Query execution time.

    Returns:
        ProcessedResult with classified result and metadata.
    """
    filters = canonical_query.filters or QueryFilters()
    result_type = _determine_result_type(template_result, canonical_query)
    filters_applied = _extract_filters_applied(filters)

    # Collect interpretation notes
    notes = []
    if template_result and template_result.interpretation_notes:
        notes.extend(template_result.interpretation_notes)

    warnings = []
    if template_result and template_result.warnings:
        warnings.extend(template_result.warnings)

    # --- EMPTY DETECTION ---

    # Case 1: No rows at all
    if not rows:
        return _build_empty_result(
            columns=columns,
            filters=filters,
            filters_applied=filters_applied,
            sql_used=sql_used,
            result_type=result_type,
            notes=notes,
            warnings=warnings,
            export_key=export_key,
            execution_time_ms=execution_time_ms,
            empty_reason="No records matched the applied filters.",
        )

    # Case 2: Aggregate with count = 0
    if result_type == "aggregate" and _is_aggregate_empty(rows, columns):
        return _build_empty_result(
            columns=columns,
            filters=filters,
            filters_applied=filters_applied,
            sql_used=sql_used,
            result_type="empty",
            notes=notes,
            warnings=warnings,
            export_key=export_key,
            execution_time_ms=execution_time_ms,
            empty_reason="Aggregate query returned zero matching records.",
            is_aggregate_empty=True,
        )

    # --- NON-EMPTY RESULT ---

    total = total_row_count if total_row_count is not None else len(rows)
    display_rows = rows[:display_limit]
    displayed_count = len(display_rows)

    # Determine summary basis
    if result_type == "aggregate":
        summary_basis = "aggregate"
    elif displayed_count < total:
        summary_basis = "preview_only"
    else:
        summary_basis = "full_result"

    metadata = ResultMetadata(
        total_matching_rows=total,
        displayed_row_count=displayed_count,
        columns_returned=columns,
        filters_applied=filters_applied,
        sql_used=sql_used,
        result_type=result_type,
        summary_basis=summary_basis,
        export_key=export_key,
        execution_time_ms=execution_time_ms,
        interpretation_notes=notes,
        warnings=warnings,
    )

    return ProcessedResult(
        metadata=metadata,
        display_rows=display_rows,
        table_headers=columns,
        is_empty=False,
        is_aggregate_empty=False,
    )


# =============================================================================
# EMPTY RESULT BUILDER
# =============================================================================


def _build_empty_result(
    *,
    columns: List[str],
    filters: QueryFilters,
    filters_applied: Dict[str, str],
    sql_used: str,
    result_type: str,
    notes: List[str],
    warnings: List[str],
    export_key: Optional[str],
    execution_time_ms: Optional[int],
    empty_reason: str,
    is_aggregate_empty: bool = False,
) -> ProcessedResult:
    """Build a ProcessedResult for an empty result set."""
    suggestion = _generate_empty_suggestion(filters)

    metadata = ResultMetadata(
        total_matching_rows=0,
        displayed_row_count=0,
        columns_returned=columns,
        filters_applied=filters_applied,
        sql_used=sql_used,
        result_type="empty",
        summary_basis="none",
        export_key=None,
        execution_time_ms=execution_time_ms,
        interpretation_notes=notes,
        warnings=warnings,
        assistant_suggestion=suggestion,
        empty_reason=empty_reason,
    )

    return ProcessedResult(
        metadata=metadata,
        display_rows=[],
        table_headers=columns,
        is_empty=True,
        is_aggregate_empty=is_aggregate_empty,
        user_message_hint=empty_reason,
    )


# =============================================================================
# HELPERS
# =============================================================================


def _determine_result_type(
    template_result: Optional[SQLTemplateResult],
    canonical_query: CanonicalQuery,
) -> str:
    """Determine the result type from template metadata."""
    if template_result:
        return template_result.result_type

    if canonical_query.intent == QueryIntent.DIRECT_LOOKUP:
        return "direct_lookup"

    return "detail"


def _is_aggregate_empty(rows: List[Dict[str, Any]], columns: List[str]) -> bool:
    """Detect if an aggregate result has count = 0.

    An aggregate query always returns at least one row even when no records match.
    We check if any count-like column is 0 or None.
    """
    if not rows or len(rows) != 1:
        # Multi-row aggregates (GROUP BY) with data are not empty
        return False

    row = rows[0]
    lower_columns = {col.lower(): col for col in columns}

    for count_name in _COUNT_COLUMN_NAMES:
        if count_name in lower_columns:
            actual_col = lower_columns[count_name]
            value = row.get(actual_col)
            if value is None or value == 0 or str(value) == "0":
                return True

    return False


def _extract_filters_applied(filters: QueryFilters) -> Dict[str, str]:
    """Extract applied filters into a human-readable dict."""
    applied = {}
    if filters.source_:
        applied["source"] = filters.source_
    if filters.destination:
        applied["destination"] = filters.destination
    if filters.transportation_mode_desc:
        applied["mode"] = filters.transportation_mode_desc
    if filters.business_unit_id:
        applied["business_unit"] = filters.business_unit_id
    if filters.status_category:
        applied["status"] = filters.status_category
    if filters.date_range:
        dr = filters.date_range
        date_str = f"{dr.field}: {dr.start or 'open'} to {dr.end or 'open'}"
        applied["date_range"] = date_str
    if filters.shipment_ids:
        applied["shipment_ids"] = ", ".join(filters.shipment_ids[:5])
    return applied


def _generate_empty_suggestion(filters: QueryFilters) -> str:
    """Generate a safe suggestion for empty results based on applied filters."""
    if filters.date_range:
        return "broaden_date_range"
    if filters.transportation_mode_desc:
        return "remove_mode_filter"
    if filters.destination:
        return "remove_destination_filter"
    if filters.status_category:
        return "remove_status_filter"
    return "verify_filters"
