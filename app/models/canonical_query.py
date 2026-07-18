"""Canonical query representation for the TransparencE chatbot.

Defines structured models that represent user intent as a deterministic,
normalized object - independent of how the user phrased the question.

Semantically equivalent queries produce the same CanonicalQuery regardless
of punctuation, greetings, or phrasing variation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


# =============================================================================
# INTENT CONSTANTS
# =============================================================================


class QueryIntent(str, Enum):
    """All recognized intent categories for the chatbot."""

    SHIPMENT_QUERY = "shipment_query"
    DIRECT_LOOKUP = "direct_lookup"
    FOLLOW_UP_FILTER = "follow_up_filter"
    SUMMARIZE_LAST_RESULT = "summarize_last_result"
    DOWNLOAD_LAST_RESULT = "download_last_result"
    BROADEN_PREVIOUS_DATE_RANGE = "broaden_previous_date_range"
    GREETING = "greeting"
    EXIT = "exit"
    ABUSIVE_OR_INAPPROPRIATE = "abusive_or_inappropriate"
    OFF_TOPIC = "off_topic"
    CLARIFICATION_NEEDED = "clarification_needed"


# =============================================================================
# QUERY MODELS
# =============================================================================


@dataclass
class DateRange:
    """A date filter range tied to a specific column."""

    field: str  # eta, actual_pgi_date, final_gr_date, ata
    start: Optional[str] = None  # YYYY-MM-DD or None (open-ended)
    end: Optional[str] = None  # YYYY-MM-DD or None (open-ended)


@dataclass
class QueryFilters:
    """All supported filters for a shipment query."""

    source_: Optional[str] = None  # ISO country code (e.g., "MX")
    destination: Optional[str] = None  # ISO country code (e.g., "CZ")
    transportation_mode_desc: Optional[str] = None  # Exact DB value (e.g., "Air transport")
    business_unit_id: Optional[str] = None  # BU code (e.g., "ADC")
    status_category: Optional[str] = None  # "in_transit" | "completed" | "delayed"
    date_range: Optional[DateRange] = None
    shipment_ids: Optional[List[str]] = None  # Business identifiers
    execution_status_values: Optional[List[str]] = None  # Exact DB values


@dataclass
class CanonicalQuery:
    """Structured representation of a user's query intent.

    The same business question with different wording should produce
    the same CanonicalQuery object.
    """

    intent: str  # One of QueryIntent values
    filters: QueryFilters = field(default_factory=QueryFilters)
    metrics: List[str] = field(default_factory=list)  # e.g., ["revenue", "weight", "count"]
    group_by: List[str] = field(default_factory=list)
    sort: List[Dict[str, str]] = field(default_factory=list)  # [{"field": "x", "direction": "DESC"}]
    limit: int = 500
    confidence: float = 0.0  # 0.0-1.0 overall parse confidence
    source: str = "unknown"  # "deterministic" | "llm" | "direct_lookup" | "context_merge"
    requires_clarification: bool = False
    clarification_question: Optional[str] = None
    raw_input: str = ""
    normalized_input: str = ""


# =============================================================================
# CONVERSATION STATE (lightweight model for Phase 2; wired in Phase 3)
# =============================================================================


@dataclass
class ConversationState:
    """Business state tracked across conversation turns.

    Separate from raw chat transcript. Off-topic/abusive messages
    do NOT update this state.
    """

    last_canonical_query: Optional[CanonicalQuery] = None
    last_result_row_count: Optional[int] = None
    last_result_headers: Optional[List[str]] = None
    last_download_key: Optional[str] = None
    last_assistant_suggestion: Optional[str] = None
    last_date_range: Optional[DateRange] = None
    last_filters: Optional[QueryFilters] = None
