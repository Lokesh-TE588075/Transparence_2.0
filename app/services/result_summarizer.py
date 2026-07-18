"""Grounded result summarizer for the TransparencE chatbot.

Produces summaries that are strictly grounded in executed SQL results
or stored result metadata. Never invents facts, infers trends,
estimated lead times, causes, or makes unsupported operational claims.

Two modes:
- Deterministic (default): Template-based summaries from ProcessedResult.
- LLM-assisted (opt-in): Uses a restrictive anti-hallucination prompt.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.models.canonical_query import CanonicalQuery, QueryIntent
from app.services.result_processor import ProcessedResult

logger = logging.getLogger(__name__)


# =============================================================================
# UNSUPPORTED INFERENCE DETECTION
# =============================================================================

# Phrases that indicate unsupported operational inference
_UNSUPPORTED_PHRASES = [
    "suggesting",
    "likely",
    "probably",
    "appears to indicate",
    "trend shows",
    "caused by",
    "due to",
    "lead time",
    "operational issue",
    "bottleneck",
    "roughly",
    "approximately",
    "implies that",
    "this suggests",
    "indicating",
]

# Phrases allowed ONLY when explicitly computed in result metrics
_CONDITIONALLY_ALLOWED = {
    "lead time": ["avg_lead_time", "lead_time", "tot. repl. lead time"],
    "trend shows": ["trend", "time_series", "month_over_month"],
}


def detect_unsupported_claims(summary: str) -> List[str]:
    """Detect unsupported inference phrases in a summary.

    Returns list of detected problematic phrases.
    """
    lower = summary.lower()
    violations = []
    for phrase in _UNSUPPORTED_PHRASES:
        if phrase in lower:
            violations.append(phrase)
    return violations


def validate_summary_grounding(
    summary: str, processed_result: ProcessedResult
) -> Tuple[bool, List[str]]:
    """Validate that a summary is grounded in the result data.

    Returns (is_valid, list_of_violations).
    A valid summary contains no unsupported claims unless the
    underlying data explicitly supports them.
    """
    violations = detect_unsupported_claims(summary)

    # Allow conditionally allowed phrases if backed by result columns
    columns_lower = {c.lower() for c in processed_result.table_headers}
    filtered_violations = []
    for v in violations:
        if v in _CONDITIONALLY_ALLOWED:
            allowed_cols = _CONDITIONALLY_ALLOWED[v]
            if any(col in columns_lower for col in allowed_cols):
                continue  # Phrase is supported by data
        filtered_violations.append(v)

    is_valid = len(filtered_violations) == 0
    return is_valid, filtered_violations


# =============================================================================
# MAIN SUMMARIZER
# =============================================================================


def summarize_processed_result(
    processed_result: ProcessedResult,
    canonical_query: CanonicalQuery,
    user_question: Optional[str] = None,
    use_llm: bool = False,
    llm_service: Optional[Any] = None,
) -> str:
    """Generate a grounded summary of a processed query result.

    Args:
        processed_result: Output from result_processor.
        canonical_query: The canonical query for context.
        user_question: Original user question (for LLM context).
        use_llm: If True, uses LLM with anti-hallucination prompt.
        llm_service: LLM service instance (required if use_llm=True).

    Returns:
        A grounded summary string.
    """
    if use_llm and llm_service is not None:
        return _summarize_with_llm(
            processed_result, canonical_query, user_question, llm_service
        )

    return _summarize_deterministic(processed_result, canonical_query)


# =============================================================================
# DETERMINISTIC SUMMARIZER
# =============================================================================


def _summarize_deterministic(
    result: ProcessedResult, cq: CanonicalQuery
) -> str:
    """Generate a template-based grounded summary."""
    meta = result.metadata

    if result.is_empty:
        return _summarize_empty(result)

    if meta.result_type == "aggregate":
        return _summarize_aggregate(result)

    if meta.result_type == "direct_lookup":
        return _summarize_direct_lookup(result)

    # Default: detail result
    return _summarize_detail(result)


def _summarize_empty(result: ProcessedResult) -> str:
    """Summarize an empty result."""
    meta = result.metadata
    parts = ["No records matched the applied filters."]

    # Include filters
    if meta.filters_applied:
        filter_desc = format_filters_for_summary(meta.filters_applied)
        parts.append(f"Filters: {filter_desc}.")

    # Include interpretation notes
    if meta.interpretation_notes:
        for note in meta.interpretation_notes:
            parts.append(note)

    # Include suggestion
    if meta.assistant_suggestion:
        suggestion_msg = _suggestion_to_text(meta.assistant_suggestion)
        parts.append(suggestion_msg)

    return " ".join(parts)


def _summarize_detail(result: ProcessedResult) -> str:
    """Summarize a detail result."""
    meta = result.metadata
    total = meta.total_matching_rows
    displayed = meta.displayed_row_count
    parts = []

    # Row counts
    parts.append(f"Found {total:,} matching shipment{'s' if total != 1 else ''}.")

    if displayed < total:
        parts.append(
            f"Only the first {displayed:,} rows are displayed "
            f"out of {total:,} matching rows."
        )

    # Summary basis warning
    if meta.summary_basis == "preview_only":
        parts.append(
            "This summary is based on the displayed preview rows, "
            "not necessarily the full matching result."
        )

    # Export availability
    if meta.export_key:
        parts.append("Full export is available.")

    # Filters
    if meta.filters_applied:
        filter_desc = format_filters_for_summary(meta.filters_applied)
        parts.append(f"Applied filters: {filter_desc}.")

    # Interpretation notes
    if meta.interpretation_notes:
        for note in meta.interpretation_notes:
            parts.append(note)

    # Warnings
    if meta.warnings:
        for w in meta.warnings:
            parts.append(f"Note: {w}")

    return " ".join(parts)


def _summarize_aggregate(result: ProcessedResult) -> str:
    """Summarize an aggregate result."""
    meta = result.metadata
    parts = ["This summary is based on aggregate query output."]

    # Include aggregate values
    if result.display_rows:
        if len(result.display_rows) == 1:
            row = result.display_rows[0]
            metric_parts = []
            for col, val in row.items():
                if val is not None and val != "":
                    col_label = col.replace("_", " ")
                    if isinstance(val, float):
                        metric_parts.append(f"{col_label}: {val:,.2f}")
                    elif isinstance(val, int):
                        metric_parts.append(f"{col_label}: {val:,}")
                    else:
                        metric_parts.append(f"{col_label}: {val}")
            if metric_parts:
                parts.append("Results: " + ", ".join(metric_parts) + ".")
        else:
            parts.append(f"Returned {len(result.display_rows)} grouped rows.")

    # Filters
    if meta.filters_applied:
        filter_desc = format_filters_for_summary(meta.filters_applied)
        parts.append(f"Filters: {filter_desc}.")

    # Interpretation notes
    if meta.interpretation_notes:
        for note in meta.interpretation_notes:
            parts.append(note)

    return " ".join(parts)


def _summarize_direct_lookup(result: ProcessedResult) -> str:
    """Summarize a direct lookup result."""
    meta = result.metadata
    total = meta.total_matching_rows
    parts = []

    if total == 1:
        parts.append("Found 1 matching record.")
        # Summarize key fields from the single row
        if result.display_rows:
            row = result.display_rows[0]
            key_fields = []
            for col in ["shipment_number_id", "source_", "destination",
                        "execution_status", "transportation_mode_desc"]:
                if col in row and row[col] is not None:
                    key_fields.append(f"{col.replace('_', ' ')}: {row[col]}")
            if key_fields:
                parts.append("Details: " + ", ".join(key_fields) + ".")
    elif total > 1:
        parts.append(
            f"Found {total} records matching the provided identifier. "
            "Multiple ID fields may have matched."
        )
    else:
        parts.append("No records found for the provided identifier.")

    # Interpretation notes
    if meta.interpretation_notes:
        for note in meta.interpretation_notes:
            parts.append(note)

    return " ".join(parts)


# =============================================================================
# LLM-ASSISTED SUMMARIZER
# =============================================================================


def build_llm_summarization_prompt(
    processed_result: ProcessedResult,
    canonical_query: CanonicalQuery,
    user_question: Optional[str] = None,
) -> str:
    """Build a restrictive LLM prompt for grounded summarization.

    This prompt enforces anti-hallucination rules.
    """
    meta = processed_result.metadata

    prompt_parts = [
        "You are a data summarization assistant. Summarize ONLY the provided query result.",
        "",
        "STRICT RULES:",
        "- Use ONLY the data provided below. Do not use prior knowledge.",
        "- Do NOT infer trends, causes, lead times, or operational impact.",
        "- Do NOT use words like 'suggesting', 'likely', 'probably', 'appears to indicate'.",
        "- Do NOT mention 'lead time' unless an explicit lead_time column is in the data.",
        "- Do NOT say 'trend shows' unless a time-series aggregation is present.",
        "- Do NOT say 'caused by', 'due to', 'bottleneck', or 'operational issue'.",
        "- If the data does not contain enough information, say so explicitly.",
        "- Preserve numerical values exactly as provided.",
        "- Be concise (3-5 sentences maximum).",
        "",
    ]

    # Summary basis
    if meta.summary_basis == "preview_only":
        prompt_parts.append(
            "IMPORTANT: This data is a preview (limited rows), NOT the full result. "
            "State this clearly in your summary."
        )
    elif meta.summary_basis == "aggregate":
        prompt_parts.append(
            "IMPORTANT: This data is from an aggregate query. "
            "State that the summary is based on aggregate output."
        )

    # Context
    prompt_parts.append(f"\nResult type: {meta.result_type}")
    prompt_parts.append(f"Total matching rows: {meta.total_matching_rows}")
    prompt_parts.append(f"Displayed rows: {meta.displayed_row_count}")
    prompt_parts.append(f"Summary basis: {meta.summary_basis}")

    if meta.filters_applied:
        prompt_parts.append(f"Filters applied: {meta.filters_applied}")

    if meta.interpretation_notes:
        prompt_parts.append(f"Interpretation notes: {meta.interpretation_notes}")

    # Data
    prompt_parts.append(f"\nColumns: {meta.columns_returned}")
    if result_rows := processed_result.display_rows[:5]:
        prompt_parts.append(f"Sample rows (up to 5): {result_rows}")

    if user_question:
        prompt_parts.append(f"\nUser question: {user_question}")

    prompt_parts.append("\nProvide a grounded summary:")

    return "\n".join(prompt_parts)


def _summarize_with_llm(
    result: ProcessedResult,
    cq: CanonicalQuery,
    user_question: Optional[str],
    llm_service: Any,
) -> str:
    """Use LLM with anti-hallucination prompt to summarize."""
    prompt = build_llm_summarization_prompt(result, cq, user_question)

    try:
        # Call LLM (assumes llm_service has a method like generate or complete)
        if hasattr(llm_service, "summarize_with_prompt"):
            summary = llm_service.summarize_with_prompt(prompt)
        elif hasattr(llm_service, "generate"):
            summary = llm_service.generate(prompt)
        else:
            logger.warning("LLM service has no recognized summarize method")
            return _summarize_deterministic(result, cq)

        # Validate the LLM output
        is_valid, violations = validate_summary_grounding(summary, result)
        if not is_valid:
            logger.warning(
                "LLM summary contained unsupported claims: %s. "
                "Falling back to deterministic.",
                violations,
            )
            return _summarize_deterministic(result, cq)

        return summary

    except Exception as e:
        logger.error("LLM summarization failed: %s. Falling back.", str(e)[:200])
        return _summarize_deterministic(result, cq)


# =============================================================================
# HELPERS
# =============================================================================


def format_filters_for_summary(filters_applied: Dict[str, str]) -> str:
    """Format filter dict into human-readable string for summaries."""
    if not filters_applied:
        return "none"

    _LABELS = {
        "source": "origin",
        "destination": "destination",
        "mode": "transport mode",
        "business_unit": "business unit",
        "status": "status",
        "date_range": "date range",
        "shipment_ids": "shipment IDs",
    }

    parts = []
    for key, value in filters_applied.items():
        label = _LABELS.get(key, key)
        parts.append(f"{label} = {value}")

    return ", ".join(parts)


def _suggestion_to_text(suggestion: str) -> str:
    """Convert suggestion code to user-facing text."""
    mapping = {
        "broaden_date_range": "Try broadening the date range for more results.",
        "remove_mode_filter": "Try removing the transport mode filter.",
        "remove_destination_filter": "Try removing the destination filter.",
        "remove_status_filter": "Try removing the status filter.",
        "verify_filters": "Please verify the filter values and try again.",
    }
    return mapping.get(suggestion, suggestion)
