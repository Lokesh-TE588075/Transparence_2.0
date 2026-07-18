"""Input normalizer for the TransparencE chatbot.

Cleans raw user input, preserves business identifiers, and extracts
structured entities (country, mode, status, BU) with confidence scoring.

This module is deterministic (~0ms) and runs before any LLM call.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from rapidfuzz import fuzz, process

from app.business_rules.mappings import (
    BU_NAME_TO_CODE,
    COUNTRY_NAME_TO_ISO,
    QUERY_INDICATOR_KEYWORDS,
    TRANSPORT_MODE_SYNONYMS,
    STATUS_CATEGORY_SYNONYMS,
    META_INTENT_PATTERNS,
)

logger = logging.getLogger(__name__)

# Confidence threshold for fuzzy matching (conservative)
FUZZY_MATCH_THRESHOLD = 85


# =============================================================================
# DATA MODELS
# =============================================================================


@dataclass
class ExtractedEntities:
    """Entities extracted from normalized user input."""

    source_country: Optional[str] = None  # ISO code or None
    source_confidence: float = 0.0
    source_original: Optional[str] = None  # Original token before matching

    destination_country: Optional[str] = None  # ISO code or None
    destination_confidence: float = 0.0
    destination_original: Optional[str] = None

    transport_mode: Optional[str] = None  # Exact DB value or None
    status_category: Optional[str] = None  # "in_transit" | "completed" | "delayed"
    date_phrases: List[str] = field(default_factory=list)  # Raw date phrases

    business_unit: Optional[str] = None  # BU code or None
    bu_confidence: float = 0.0
    bu_original: Optional[str] = None

    shipment_ids: List[str] = field(default_factory=list)  # Preserved identifiers
    meta_intent_hint: Optional[str] = None  # "summarize" | "download" | "broaden" | None

    # Delivery-date intent (Phase 7D)
    preferred_date_field: Optional[str] = None  # "eta" for future delivery phrases

    # Clarification tracking
    requires_clarification: bool = False
    clarification_question: Optional[str] = None


@dataclass
class NormalizedInput:
    """Result of input normalization."""

    raw: str  # Original user input
    cleaned: str  # Cleaned text (noise removed, greetings stripped)
    entities: ExtractedEntities = field(default_factory=ExtractedEntities)


# =============================================================================
# PATTERNS
# =============================================================================

# Business identifier patterns (to protect during cleaning)
_ID_PATTERNS = [
    re.compile(r"\b[A-Z]{2,4}\d{5,}\b"),  # MEDU1234567, AB12345
    re.compile(r"\b\d{3,}-\d+\b"),  # 123-456
    re.compile(r"\b[A-Z0-9]{6,}\b"),  # ABCD1234 (6+ alphanumeric uppercase)
    re.compile(r"\b\d{3,}\b"),  # Any 3+ digit number
]

# Source/destination extraction patterns
_SOURCE_PATTERNS = [
    re.compile(r"\b(?:from|originating from|source|origin|shipped from)\s+([A-Za-z\s]+?)(?:\s+(?:to|via|by|since|last|this|next|between|\d)|[,;.!?]|$)", re.IGNORECASE),
]

_DEST_PATTERNS = [
    re.compile(r"\b(?:to|going to|destination|destined for|heading to)\s+([A-Za-z\s]+?)(?:\s+(?:from|via|by|since|last|this|next|between|\d)|[,;.!?]|$)", re.IGNORECASE),
]

# Delivery-verb prefixes that should NOT trigger destination extraction (Phase 7D)
# When destination regex captures "be delivered", "be shipped", etc., skip the match.
_DELIVERY_VERB_PREFIXES = frozenset({
    "be delivered",
    "be shipped",
    "be picked",
    "arrive",
    "reach",
    "depart",
    "deliver",
    "ship",
})

# Delivery-date phrase patterns (Phase 7D) — capture intent + raw date phrase
# These indicate future delivery timing, not a destination.
_DELIVERY_DATE_PATTERNS = [
    re.compile(
        r"\b(?:to be delivered|delivered|due|arriving|expected|delivery|eta)\s+"
        r"(next\s+(?:week|month)|this\s+(?:week|month)|last\s+(?:week|month))",
        re.IGNORECASE,
    ),
]

# Date phrase patterns (capture raw, don't interpret)
_DATE_PATTERNS = [
    re.compile(r"\b(next\s+(?:week|month|quarter|year))\b", re.IGNORECASE),
    re.compile(r"\b(last\s+\d+\s+(?:days?|weeks?|months?))\b", re.IGNORECASE),
    re.compile(r"\b(past\s+\d+\s+(?:days?|weeks?|months?))\b", re.IGNORECASE),
    re.compile(r"\b(this\s+(?:week|month|quarter|year))\b", re.IGNORECASE),
    re.compile(r"\b(since\s+(?:january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec))\b", re.IGNORECASE),
    re.compile(r"\b(between\s+\d{4}-\d{2}-\d{2}\s+and\s+\d{4}-\d{2}-\d{2})\b", re.IGNORECASE),
    re.compile(r"\b(last\s+(?:week|month|quarter|year))\b", re.IGNORECASE),
]

# Greeting prefixes to strip
_GREETING_PREFIXES = ("hi ", "hey ", "hello ", "yo ", "hii ", "hiii ")


# =============================================================================
# MAIN NORMALIZE FUNCTION
# =============================================================================


def normalize(raw_input: str) -> NormalizedInput:
    """Normalize raw user input into clean text with extracted entities.

    Steps:
    1. Preserve business identifiers (protect from cleaning)
    2. Strip meaningless noise punctuation
    3. Normalize whitespace
    4. Strip greeting prefix (if remainder has query content)
    5. Extract entities (country, mode, status, dates, BU, meta-intent)

    Args:
        raw_input: The raw user message string.

    Returns:
        NormalizedInput with cleaned text and extracted entities.
    """
    if not raw_input or not raw_input.strip():
        return NormalizedInput(raw=raw_input, cleaned="", entities=ExtractedEntities())

    text = raw_input.strip()
    entities = ExtractedEntities()

    # Step 1: Extract and protect business identifiers
    protected_ids, text_for_cleaning = _protect_identifiers(text)
    entities.shipment_ids = protected_ids

    # Step 2: Strip meaningless noise punctuation
    cleaned = _strip_noise_punctuation(text_for_cleaning)

    # Step 3: Normalize whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Step 4: Strip greeting prefix (only if remainder has query content)
    cleaned = _strip_greeting_prefix(cleaned)

    # Step 5: Extract entities from cleaned text
    entities.source_country, entities.source_confidence, entities.source_original = (
        _extract_country(cleaned, direction="source")
    )
    entities.destination_country, entities.destination_confidence, entities.destination_original = (
        _extract_country(cleaned, direction="destination")
    )
    entities.transport_mode = _extract_transport_mode(cleaned)
    entities.status_category = _extract_status_category(cleaned)
    entities.date_phrases = _extract_date_phrases(cleaned)
    # Phase 7D: Check for delivery-date phrases → set preferred_date_field
    entities.preferred_date_field = _extract_delivery_date_intent(cleaned)
    entities.business_unit, entities.bu_confidence, entities.bu_original = _extract_bu(cleaned)
    entities.meta_intent_hint = _extract_meta_intent(cleaned)

    # Check if any entity needs clarification
    if entities.source_original and entities.source_country is None and entities.source_confidence > 0:
        entities.requires_clarification = True
        entities.clarification_question = (
            f"Did you mean a specific country for '{entities.source_original}'? "
            "I wasn't confident enough in my match."
        )
    elif entities.destination_original and entities.destination_country is None and entities.destination_confidence > 0:
        entities.requires_clarification = True
        entities.clarification_question = (
            f"Did you mean a specific country for '{entities.destination_original}'? "
            "I wasn't confident enough in my match."
        )

    return NormalizedInput(raw=raw_input, cleaned=cleaned, entities=entities)


# =============================================================================
# INTERNAL HELPERS
# =============================================================================


def _protect_identifiers(text: str) -> Tuple[List[str], str]:
    """Extract business identifiers that should be preserved during cleaning.

    Returns (list_of_ids, text_unchanged). IDs are extracted but text is
    NOT modified — we just record which tokens are identifiers.
    """
    ids = []
    # Extract quoted strings
    for match in re.finditer(r'["\']([^"\']+)["\']', text):
        ids.append(match.group(1))

    # Extract identifier patterns (don't remove from text)
    for pattern in _ID_PATTERNS:
        for match in pattern.finditer(text):
            token = match.group(0)
            # Avoid matching common words that happen to be uppercase
            if token.isalpha() and len(token) < 6:
                continue
            if token not in ids:
                ids.append(token)

    return ids, text


def _strip_noise_punctuation(text: str) -> str:
    """Remove meaningless repeated punctuation noise.

    Rules:
    - Remove runs of 3+ repeated non-alphanumeric chars at END of message
    - Remove runs of 3+ repeated non-alphanumeric chars that are standalone
    - Do NOT strip punctuation within words, IDs, or normal sentences
    """
    # Strip trailing noise (3+ repeated non-alnum at end)
    text = re.sub(r"([^\w\s])\1{2,}\s*$", "", text)

    # Strip leading noise (3+ repeated non-alnum at start)
    text = re.sub(r"^\s*([^\w\s])\1{2,}", "", text)

    # Strip standalone noise clusters (surrounded by whitespace)
    text = re.sub(r"\s+([^\w\s])\1{2,}\s+", " ", text)

    return text.strip()


def _strip_greeting_prefix(text: str) -> str:
    """Strip greeting prefix if remainder contains query content."""
    lower = text.lower()
    for prefix in _GREETING_PREFIXES:
        if lower.startswith(prefix):
            remainder = text[len(prefix):].lstrip(", ")
            if len(remainder) > 3 and _has_query_content(remainder):
                return remainder

    # Handle "hi, can you..." with comma/exclamation
    comma_match = re.match(r"^(?:hi|hey|hello|yo)\s*[,!]\s*", text, re.IGNORECASE)
    if comma_match:
        remainder = text[comma_match.end():]
        if len(remainder) > 3 and _has_query_content(remainder):
            return remainder

    return text


def _has_query_content(text: str) -> bool:
    """Check if text contains query-indicating keywords."""
    lower = text.lower()
    words = set(re.findall(r"\b\w+\b", lower))
    if words & QUERY_INDICATOR_KEYWORDS:
        return True
    for phrase in ("how many", "how much", "business unit"):
        if phrase in lower:
            return True
    if re.search(r"\d{3,}", text):
        return True
    return False


def _extract_country(text: str, direction: str) -> Tuple[Optional[str], float, Optional[str]]:
    """Extract country from source/destination patterns.

    Returns (iso_code_or_None, confidence, original_token).
    Uses threshold-based fuzzy matching: if confidence < FUZZY_MATCH_THRESHOLD,
    returns None and logs the attempt.
    """
    patterns = _SOURCE_PATTERNS if direction == "source" else _DEST_PATTERNS

    for pattern in patterns:
        match = pattern.search(text)
        if match:
            raw_country = match.group(1).strip()
            if not raw_country or len(raw_country) < 2:
                continue

            # Phase 7D: Skip delivery-verb matches for destination extraction
            if direction == "destination":
                raw_lower = raw_country.lower()
                if any(raw_lower.startswith(prefix) for prefix in _DELIVERY_VERB_PREFIXES):
                    logger.debug(
                        "Skipping delivery-verb match for destination: '%s'", raw_country
                    )
                    continue

            # Exact match first (case-insensitive)
            for name, iso in COUNTRY_NAME_TO_ISO.items():
                if name.lower() == raw_country.lower():
                    logger.debug("Country exact match: '%s' → %s (confidence=1.0)", raw_country, iso)
                    return iso, 1.0, raw_country

            # Check if it's already an ISO code
            upper = raw_country.upper()
            if upper in COUNTRY_NAME_TO_ISO.values():
                return upper, 1.0, raw_country

            # Fuzzy match with conservative threshold
            best_match = process.extractOne(
                raw_country, COUNTRY_NAME_TO_ISO.keys(), scorer=fuzz.token_sort_ratio
            )
            if best_match:
                matched_name, score, _ = best_match
                logger.info(
                    "Country fuzzy match: '%s' → '%s' (ISO=%s, score=%.1f, threshold=%d)",
                    raw_country, matched_name, COUNTRY_NAME_TO_ISO[matched_name],
                    score, FUZZY_MATCH_THRESHOLD,
                )
                if score >= FUZZY_MATCH_THRESHOLD:
                    return COUNTRY_NAME_TO_ISO[matched_name], score / 100.0, raw_country
                else:
                    # Below threshold — return None but record confidence for clarification
                    return None, score / 100.0, raw_country

    return None, 0.0, None


def _extract_transport_mode(text: str) -> Optional[str]:
    """Extract transport mode using synonym mapping."""
    lower = text.lower()
    # Sort by length (longest first) to match multi-word phrases first
    for synonym in sorted(TRANSPORT_MODE_SYNONYMS.keys(), key=len, reverse=True):
        # Word-boundary-aware match
        pattern = rf"\b{re.escape(synonym)}\b"
        if re.search(pattern, lower):
            return TRANSPORT_MODE_SYNONYMS[synonym]
    return None


def _extract_status_category(text: str) -> Optional[str]:
    """Extract status category using synonym mapping.

    Phase 7D guard: Skips matches where the status word is part of a
    future-delivery phrase (e.g., 'to be delivered' should NOT mean 'completed').
    """
    lower = text.lower()
    # Phase 7D: Phrases that negate completed status detection
    _FUTURE_DELIVERY_CONTEXTS = ("to be delivered", "be delivered", "to deliver")

    for category, synonyms in STATUS_CATEGORY_SYNONYMS.items():
        for synonym in synonyms:
            if synonym in lower:
                # Guard: if "delivered" appears in a future-tense context, skip it
                if synonym == "delivered" and any(ctx in lower for ctx in _FUTURE_DELIVERY_CONTEXTS):
                    continue
                return category
    return None


def _extract_date_phrases(text: str) -> List[str]:
    """Extract raw date phrases (not interpreted — that's Phase 4)."""
    phrases = []
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            phrases.append(match.group(1))
    return phrases


def _extract_delivery_date_intent(text: str) -> Optional[str]:
    """Detect delivery-date phrases and return preferred date field.

    Phrases like 'to be delivered next week' indicate the user wants
    ETA-based filtering, not destination extraction.

    Returns:
        'eta' if a delivery-date phrase is detected, None otherwise.
    """
    for pattern in _DELIVERY_DATE_PATTERNS:
        if pattern.search(text):
            return "eta"
    return None


def _extract_bu(text: str) -> Tuple[Optional[str], float, Optional[str]]:
    """Extract business unit code with confidence scoring.

    Returns (bu_code_or_None, confidence, original_token).
    """
    # Check for exact BU code mentions (3-letter uppercase codes)
    bu_codes = set(BU_NAME_TO_CODE.values())
    words = re.findall(r"\b[A-Z]{3}\b", text)
    for word in words:
        if word in bu_codes:
            return word, 1.0, word

    # Check for BU name exact matches
    lower = text.lower()
    for bu_name, code in BU_NAME_TO_CODE.items():
        if bu_name.lower() in lower:
            return code, 1.0, bu_name

    # Fuzzy match multi-word tokens against BU names
    # Only try if there are capitalized words that might be BU names
    tokens = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", text)
    for token in tokens:
        if len(token) < 4:
            continue
        best_match = process.extractOne(token, BU_NAME_TO_CODE.keys(), scorer=fuzz.ratio)
        if best_match:
            matched_name, score, _ = best_match
            logger.info(
                "BU fuzzy match: '%s' → '%s' (code=%s, score=%.1f, threshold=%d)",
                token, matched_name, BU_NAME_TO_CODE[matched_name],
                score, FUZZY_MATCH_THRESHOLD,
            )
            if score >= FUZZY_MATCH_THRESHOLD:
                return BU_NAME_TO_CODE[matched_name], score / 100.0, token

    return None, 0.0, None


def _extract_meta_intent(text: str) -> Optional[str]:
    """Extract meta-intent hints (summarize, download, broaden)."""
    lower = text.lower()
    for intent_type, patterns in META_INTENT_PATTERNS.items():
        for pattern in patterns:
            if pattern in lower:
                return intent_type
    return None
