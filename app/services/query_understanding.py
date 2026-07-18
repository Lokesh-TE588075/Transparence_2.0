"""Query understanding module for the TransparencE chatbot.

Converts a NormalizedInput + intent + optional conversation state into
a CanonicalQuery object that downstream modules (SQL template engine or
LLM fallback) can use deterministically.

Handles:
- Building canonical queries from extracted entities
- Follow-up resolution (merge new entities with previous filters)
- Summarize/download/broaden intents with context awareness
- Graceful degradation when context is None
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from app.models.canonical_query import (
    CanonicalQuery,
    ConversationState,
    DateRange,
    QueryFilters,
    QueryIntent,
)
from app.services.input_normalizer import NormalizedInput
from app.services.sql_template_engine import interpret_date_phrase

logger = logging.getLogger(__name__)


def understand(
    normalized: NormalizedInput,
    intent: str,
    context: Optional[ConversationState] = None,
) -> CanonicalQuery:
    """Build a CanonicalQuery from normalized input, intent, and conversation state.

    Args:
        normalized: The output of input_normalizer.normalize().
        intent: Detected intent (one of QueryIntent values).
        context: Optional conversation state from previous turns.
            When None, context-dependent intents gracefully ask for clarification.

    Returns:
        CanonicalQuery with structured filters and metadata.
    """
    entities = normalized.entities

    # Route by intent
    if intent == QueryIntent.SHIPMENT_QUERY:
        return _build_shipment_query(normalized, entities)

    elif intent == QueryIntent.DIRECT_LOOKUP:
        return _build_direct_lookup(normalized, entities)

    elif intent == QueryIntent.FOLLOW_UP_FILTER:
        return _build_follow_up(normalized, entities, context)

    elif intent == QueryIntent.SUMMARIZE_LAST_RESULT:
        return _build_summarize(normalized, context)

    elif intent == QueryIntent.DOWNLOAD_LAST_RESULT:
        return _build_download(normalized, context)

    elif intent == QueryIntent.BROADEN_PREVIOUS_DATE_RANGE:
        return _build_broaden(normalized, context)

    elif intent in (
        QueryIntent.GREETING,
        QueryIntent.EXIT,
        QueryIntent.ABUSIVE_OR_INAPPROPRIATE,
        QueryIntent.OFF_TOPIC,
        QueryIntent.CLARIFICATION_NEEDED,
    ):
        return CanonicalQuery(
            intent=intent,
            filters=QueryFilters(),
            confidence=1.0,
            source="deterministic",
            raw_input=normalized.raw,
            normalized_input=normalized.cleaned,
        )

    else:
        # Unknown intent — treat as shipment query with low confidence
        logger.warning("Unknown intent '%s', defaulting to shipment_query", intent)
        return _build_shipment_query(normalized, entities)


# =============================================================================
# INTENT BUILDERS
# =============================================================================


def _build_shipment_query(normalized: NormalizedInput, entities) -> CanonicalQuery:
    """Build canonical query for a new shipment search."""
    filters = _entities_to_filters(entities)

    # Phase 7D: Interpret date phrases into filters.date_range
    if entities.date_phrases and not filters.date_range:
        for phrase in entities.date_phrases:
            date_range = interpret_date_phrase(phrase)
            if date_range:
                # Override field if normalizer detected delivery intent
                if entities.preferred_date_field:
                    date_range.field = entities.preferred_date_field
                filters.date_range = date_range
                break  # Use first successfully interpreted phrase

    # If shipment IDs found, this is actually a direct lookup
    if entities.shipment_ids and len(entities.shipment_ids) == 1:
        filters.shipment_ids = entities.shipment_ids
        return CanonicalQuery(
            intent=QueryIntent.DIRECT_LOOKUP,
            filters=filters,
            confidence=_compute_confidence(entities),
            source="deterministic",
            requires_clarification=entities.requires_clarification,
            clarification_question=entities.clarification_question,
            raw_input=normalized.raw,
            normalized_input=normalized.cleaned,
        )

    return CanonicalQuery(
        intent=QueryIntent.SHIPMENT_QUERY,
        filters=filters,
        confidence=_compute_confidence(entities),
        source="deterministic",
        requires_clarification=entities.requires_clarification,
        clarification_question=entities.clarification_question,
        raw_input=normalized.raw,
        normalized_input=normalized.cleaned,
    )


def _build_direct_lookup(normalized: NormalizedInput, entities) -> CanonicalQuery:
    """Build canonical query for a direct shipment ID lookup."""
    filters = QueryFilters(shipment_ids=entities.shipment_ids)

    return CanonicalQuery(
        intent=QueryIntent.DIRECT_LOOKUP,
        filters=filters,
        confidence=1.0,
        source="direct_lookup",
        raw_input=normalized.raw,
        normalized_input=normalized.cleaned,
    )


def _build_follow_up(
    normalized: NormalizedInput,
    entities,
    context: Optional[ConversationState],
) -> CanonicalQuery:
    """Build follow-up query by merging new entities with previous context."""
    if context is None or context.last_canonical_query is None:
        return CanonicalQuery(
            intent=QueryIntent.FOLLOW_UP_FILTER,
            filters=QueryFilters(),
            confidence=0.0,
            source="context_merge",
            requires_clarification=True,
            clarification_question=(
                "Could you provide more context? "
                "I don't have a previous query to refine."
            ),
            raw_input=normalized.raw,
            normalized_input=normalized.cleaned,
        )

    # Start from previous filters
    prev_filters = context.last_canonical_query.filters
    new_filters = _merge_filters(prev_filters, entities)

    return CanonicalQuery(
        intent=QueryIntent.FOLLOW_UP_FILTER,
        filters=new_filters,
        confidence=_compute_confidence(entities),
        source="context_merge",
        requires_clarification=entities.requires_clarification,
        clarification_question=entities.clarification_question,
        raw_input=normalized.raw,
        normalized_input=normalized.cleaned,
    )


def _build_summarize(
    normalized: NormalizedInput,
    context: Optional[ConversationState],
) -> CanonicalQuery:
    """Handle summarize-last-result intent."""
    if context is None or context.last_result_row_count is None:
        return CanonicalQuery(
            intent=QueryIntent.SUMMARIZE_LAST_RESULT,
            filters=QueryFilters(),
            confidence=0.0,
            source="deterministic",
            requires_clarification=True,
            clarification_question="What result would you like me to summarize?",
            raw_input=normalized.raw,
            normalized_input=normalized.cleaned,
        )

    return CanonicalQuery(
        intent=QueryIntent.SUMMARIZE_LAST_RESULT,
        filters=context.last_filters or QueryFilters(),
        confidence=1.0,
        source="deterministic",
        raw_input=normalized.raw,
        normalized_input=normalized.cleaned,
    )


def _build_download(
    normalized: NormalizedInput,
    context: Optional[ConversationState],
) -> CanonicalQuery:
    """Handle download-last-result intent."""
    if context is None or context.last_download_key is None:
        return CanonicalQuery(
            intent=QueryIntent.DOWNLOAD_LAST_RESULT,
            filters=QueryFilters(),
            confidence=0.0,
            source="deterministic",
            requires_clarification=True,
            clarification_question="There's no recent result available for download.",
            raw_input=normalized.raw,
            normalized_input=normalized.cleaned,
        )

    return CanonicalQuery(
        intent=QueryIntent.DOWNLOAD_LAST_RESULT,
        filters=context.last_filters or QueryFilters(),
        confidence=1.0,
        source="deterministic",
        raw_input=normalized.raw,
        normalized_input=normalized.cleaned,
    )


def _build_broaden(
    normalized: NormalizedInput,
    context: Optional[ConversationState],
) -> CanonicalQuery:
    """Handle broaden-previous-date-range intent.

    Deterministic broadening rules:
    - Original span <= 7 days → expand to 30 days
    - Original span 8-30 days → expand to 90 days
    - Original span 31-90 days → expand to 180 days
    - Original span > 90 days → expand to 365 days

    Preserves ALL other filters from previous context.
    """
    if context is None or context.last_date_range is None:
        return CanonicalQuery(
            intent=QueryIntent.BROADEN_PREVIOUS_DATE_RANGE,
            filters=QueryFilters(),
            confidence=0.0,
            source="deterministic",
            requires_clarification=True,
            clarification_question=(
                "I don't have a previous date range to broaden. "
                "Could you specify the time period?"
            ),
            raw_input=normalized.raw,
            normalized_input=normalized.cleaned,
        )

    # Compute new date range
    old_range = context.last_date_range
    new_range = _expand_date_range(old_range)

    # Preserve previous filters, update date range
    filters = context.last_filters or QueryFilters()
    filters.date_range = new_range

    return CanonicalQuery(
        intent=QueryIntent.BROADEN_PREVIOUS_DATE_RANGE,
        filters=filters,
        confidence=1.0,
        source="deterministic",
        raw_input=normalized.raw,
        normalized_input=normalized.cleaned,
    )


# =============================================================================
# HELPERS
# =============================================================================


def _entities_to_filters(entities) -> QueryFilters:
    """Convert extracted entities to QueryFilters."""
    return QueryFilters(
        source_=entities.source_country,
        destination=entities.destination_country,
        transportation_mode_desc=entities.transport_mode,
        status_category=entities.status_category,
        business_unit_id=entities.business_unit,
        shipment_ids=entities.shipment_ids if entities.shipment_ids else None,
    )


def _merge_filters(prev: QueryFilters, entities) -> QueryFilters:
    """Merge new entities on top of previous filters.

    New values OVERRIDE previous ones. Previous values are preserved
    if not contradicted by new entities.
    """
    return QueryFilters(
        source_=entities.source_country if entities.source_country else prev.source_,
        destination=entities.destination_country if entities.destination_country else prev.destination,
        transportation_mode_desc=(
            entities.transport_mode if entities.transport_mode else prev.transportation_mode_desc
        ),
        business_unit_id=(
            entities.business_unit if entities.business_unit else prev.business_unit_id
        ),
        status_category=(
            entities.status_category if entities.status_category else prev.status_category
        ),
        date_range=prev.date_range,  # Preserve unless explicitly changed
        shipment_ids=prev.shipment_ids,
        execution_status_values=prev.execution_status_values,
    )


def _compute_confidence(entities) -> float:
    """Compute overall confidence from entity confidences.

    Returns the minimum confidence of all entities that were extracted.
    If no entities were extracted, returns 0.5 (ambiguous).
    """
    confidences = []
    if entities.source_country:
        confidences.append(entities.source_confidence)
    if entities.destination_country:
        confidences.append(entities.destination_confidence)
    if entities.business_unit:
        confidences.append(entities.bu_confidence)
    if entities.transport_mode:
        confidences.append(1.0)  # Transport mode is always exact match
    if entities.status_category:
        confidences.append(1.0)  # Status is always exact match

    if not confidences:
        return 0.5
    return min(confidences)


def _expand_date_range(old_range: DateRange) -> DateRange:
    """Expand a date range deterministically based on its span.

    Rules:
    - <= 7 days → 30 days from original start
    - 8-30 days → 90 days from original start
    - 31-90 days → 180 days from original start
    - > 90 days → 365 days from original start
    """
    if not old_range.start or not old_range.end:
        # If open-ended, extend end by 30 days from today
        today = datetime.now().strftime("%Y-%m-%d")
        start = old_range.start or today
        end_date = datetime.strptime(start, "%Y-%m-%d") + timedelta(days=30)
        return DateRange(field=old_range.field, start=start, end=end_date.strftime("%Y-%m-%d"))

    start_dt = datetime.strptime(old_range.start, "%Y-%m-%d")
    end_dt = datetime.strptime(old_range.end, "%Y-%m-%d")
    span_days = (end_dt - start_dt).days

    if span_days <= 7:
        new_end = start_dt + timedelta(days=30)
    elif span_days <= 30:
        new_end = start_dt + timedelta(days=90)
    elif span_days <= 90:
        new_end = start_dt + timedelta(days=180)
    else:
        new_end = start_dt + timedelta(days=365)

    return DateRange(
        field=old_range.field,
        start=old_range.start,
        end=new_end.strftime("%Y-%m-%d"),
    )
