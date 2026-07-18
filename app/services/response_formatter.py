"""Response formatter for the TransparencE chatbot.

Formats ProcessedResult into user-facing response dicts that the
frontend can render safely.

Key principles:
- Never make unsupported operational inferences
- Clearly distinguish total rows from displayed rows
- Explain empty results with applied filters in business language
- Include interpretation notes (e.g., ETA usage)
- Suggest safe next actions for empty results
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.models.canonical_query import CanonicalQuery, QueryIntent
from app.services.result_processor import ProcessedResult

# Phrases that must NEVER appear in responses unless the metric is explicit
_UNSUPPORTED_PHRASES = [
    "suggesting lead time",
    "indicating operational delay",
    "likely caused by",
    "trend shows",
    "roughly a week lead time",
    "approximately",
    "implies that",
    "this suggests",
]

# Human-readable filter labels
_FILTER_LABELS = {
    "source": "origin country",
    "destination": "destination country",
    "mode": "transport mode",
    "business_unit": "business unit",
    "status": "status",
    "date_range": "date range",
    "shipment_ids": "shipment IDs",
}

# Suggestion messages
_SUGGESTION_MESSAGES = {
    "broaden_date_range": "Try broadening the date range for more results.",
    "remove_mode_filter": "Try removing the transport mode filter.",
    "remove_destination_filter": "Try removing the destination filter.",
    "remove_status_filter": "Try removing the status filter.",
    "verify_filters": "Please verify the filter values and try again.",
}


# =============================================================================
# MAIN FORMATTER
# =============================================================================


def format_response(
    processed_result: ProcessedResult,
    canonical_query: CanonicalQuery,
) -> Dict[str, Any]:
    """Format a ProcessedResult into a user-facing response dict.

    Args:
        processed_result: Output from result_processor.process_query_result().
        canonical_query: The canonical query for context.

    Returns:
        Dict with message, table data, metadata, and suggestions.
    """
    meta = processed_result.metadata

    if processed_result.is_empty:
        return _format_empty_response(processed_result, canonical_query)

    if meta.result_type == "aggregate":
        return _format_aggregate_response(processed_result, canonical_query)

    if meta.result_type == "direct_lookup":
        return _format_direct_lookup_response(processed_result, canonical_query)

    # Default: detail result
    return _format_detail_response(processed_result, canonical_query)


# =============================================================================
# EMPTY RESPONSE
# =============================================================================


def _format_empty_response(
    result: ProcessedResult, cq: CanonicalQuery
) -> Dict[str, Any]:
    """Format response for empty results."""
    meta = result.metadata
    filters_applied = meta.filters_applied

    # Build filter description in business language
    filter_parts = []
    for key, value in filters_applied.items():
        label = _FILTER_LABELS.get(key, key)
        filter_parts.append(f"{label} = {value}")

    filter_desc = ", ".join(filter_parts) if filter_parts else "the applied filters"

    # Build message
    message = f"No shipments matched {filter_desc}."

    # Add interpretation notes
    if meta.interpretation_notes:
        message += " " + " ".join(meta.interpretation_notes)

    # Add suggestion
    suggestion_text = None
    if meta.assistant_suggestion:
        suggestion_text = _SUGGESTION_MESSAGES.get(
            meta.assistant_suggestion, meta.assistant_suggestion
        )
        message += f" {suggestion_text}"

    return {
        "message": message,
        "is_table": False,
        "table_data": None,
        "row_count": 0,
        "displayed_row_count": 0,
        "summary_basis": "none",
        "export_key": None,
        "interpretation_notes": meta.interpretation_notes,
        "warnings": meta.warnings,
        "assistant_suggestion": suggestion_text,
    }


# =============================================================================
# DETAIL RESPONSE
# =============================================================================


def _format_detail_response(
    result: ProcessedResult, cq: CanonicalQuery
) -> Dict[str, Any]:
    """Format response for detail results."""
    meta = result.metadata
    total = meta.total_matching_rows
    displayed = meta.displayed_row_count

    # Build message
    parts = [f"Found {total:,} matching shipment{'s' if total != 1 else ''}."]

    if displayed < total:
        parts.append(f"Showing {displayed:,} preview rows.")

    if meta.export_key:
        parts.append("Export is available for the full result.")

    if meta.summary_basis == "preview_only":
        parts.append("(Result is capped at the display limit.)")

    message = " ".join(parts)

    # Add interpretation notes to message if any
    if meta.interpretation_notes:
        message += " " + " ".join(meta.interpretation_notes)

    return {
        "message": message,
        "is_table": True,
        "table_data": {
            "headers": result.table_headers,
            "rows": result.display_rows,
        },
        "row_count": total,
        "displayed_row_count": displayed,
        "summary_basis": meta.summary_basis,
        "export_key": meta.export_key,
        "interpretation_notes": meta.interpretation_notes,
        "warnings": meta.warnings,
        "assistant_suggestion": meta.assistant_suggestion,
    }


# =============================================================================
# AGGREGATE RESPONSE
# =============================================================================


def _format_aggregate_response(
    result: ProcessedResult, cq: CanonicalQuery
) -> Dict[str, Any]:
    """Format response for aggregate results.

    States the aggregate metrics without unsupported interpretation.
    """
    meta = result.metadata

    # Build message from aggregate row values
    message = _build_aggregate_message(result)

    if meta.interpretation_notes:
        message += " " + " ".join(meta.interpretation_notes)

    return {
        "message": message,
        "is_table": True,
        "table_data": {
            "headers": result.table_headers,
            "rows": result.display_rows,
        },
        "row_count": meta.total_matching_rows,
        "displayed_row_count": meta.displayed_row_count,
        "summary_basis": "aggregate",
        "export_key": meta.export_key,
        "interpretation_notes": meta.interpretation_notes,
        "warnings": meta.warnings,
        "assistant_suggestion": meta.assistant_suggestion,
    }


def _build_aggregate_message(result: ProcessedResult) -> str:
    """Build a grounded aggregate message from result data.

    Only states what the numbers are — no interpretation.
    """
    if not result.display_rows:
        return "Aggregate query returned results."

    row = result.display_rows[0] if len(result.display_rows) == 1 else None
    if row is None:
        return f"Query returned {len(result.display_rows)} grouped results."

    # Single-row aggregate: state the metrics
    parts = []
    for col, val in row.items():
        if val is not None and val != "":
            col_label = col.replace("_", " ").title()
            if isinstance(val, (int, float)):
                parts.append(f"{col_label}: {val:,.2f}" if isinstance(val, float) else f"{col_label}: {val:,}")
            else:
                parts.append(f"{col_label}: {val}")

    if parts:
        return "Aggregate results: " + ", ".join(parts) + "."
    return "Aggregate query completed."


# =============================================================================
# DIRECT LOOKUP RESPONSE
# =============================================================================


def _format_direct_lookup_response(
    result: ProcessedResult, cq: CanonicalQuery
) -> Dict[str, Any]:
    """Format response for direct shipment lookup."""
    meta = result.metadata
    total = meta.total_matching_rows

    if total == 1:
        message = "Here are the details for the requested shipment."
    else:
        message = f"Found {total} record{'s' if total != 1 else ''} matching the provided identifier."

    if meta.interpretation_notes:
        message += " " + " ".join(meta.interpretation_notes)

    return {
        "message": message,
        "is_table": True,
        "table_data": {
            "headers": result.table_headers,
            "rows": result.display_rows,
        },
        "row_count": total,
        "displayed_row_count": meta.displayed_row_count,
        "summary_basis": meta.summary_basis,
        "export_key": meta.export_key,
        "interpretation_notes": meta.interpretation_notes,
        "warnings": meta.warnings,
        "assistant_suggestion": meta.assistant_suggestion,
    }
