"""Genie prompt enrichment layer (Phase Q1 / E5).

Classifies user query intent and rewrites the prompt to steer Genie toward
the best possible response type for each query class.  Direct lookups and
download requests are never enriched.  Follow-ups in an existing Genie
conversation receive only minimal enrichment.

Key investigation finding:
  The Genie REST API is single-query-per-turn.  Prompt steering is the only
  available mechanism to improve output quality without additional LLM calls.
  The minimal template format ("Summarise X: count, top 5 destinations, mode,
  status") reliably produces aggregated 10-20 row results with rich text and
  a viz attachment, confirmed in live API tests.

Intent categories (evaluated top-to-bottom, first match wins)
-------------------------------------------------------------
DIRECT_LOOKUP    — exact numeric IDs, waybill, HBL, HAWB, MBL, MAWB,
                   delivery doc, PO, SO, tracking number, reference codes
                   → never enriched; Genie resolves the reference directly
DOWNLOAD_REQUEST — "download", "export csv", "give me csv"
                   → never enriched; existing download/session behavior
SUMMARY_REQUEST  — "quick summary", "summarize this", "give me summary"
                   → minimal context summary template
DETAIL_LISTING   — "list shipment numbers", "show raw records",
                   "give me all shipments"
                   → detail-oriented template (factual summary + table)
AGGREGATION      — "status distribution", "revenue by BU", "top 10 lanes",
                   "top lanes by volume", "busiest lanes", "top destinations"
                   → business analytics summary template (E5: evaluated BEFORE
                   BROAD_LISTING to avoid overlapping phrase misclassification)
BROAD_LISTING    — "shipments from US", "which are the shipments...",
                   "show delayed shipments from CN"
                   → proven analytical summary template (top counts + breakdown)
GENERAL          — everything else
                   → light context framing template

Follow-up handling:
  When is_follow_up=True (genie_conversation_id already exists), only
  SUMMARY_REQUEST is enriched; all other intents pass through unchanged to
  preserve Genie's conversation context.

Return value:
  enrich_prompt() returns a typed dict (EnrichmentResult) with:
    original_prompt    str   — raw user text, always preserved for UI display
    enriched_prompt    str   — text actually sent to Genie API
    intent             str   — detected intent class
    enrichment_applied bool  — True when enriched_prompt != original_prompt
    reason             str   — human-readable explanation

Feature flag: GENIE_ENABLE_PROMPT_ENRICHMENT=true (default)
"""

from __future__ import annotations

import re
from typing import TypedDict


# =============================================================================
# Return type
# =============================================================================

class EnrichmentResult(TypedDict):
    """Structured return from enrich_prompt()."""
    original_prompt:    str
    enriched_prompt:    str
    intent:             str
    enrichment_applied: bool
    reason:             str


# =============================================================================
# Intent detection patterns  (evaluated top-to-bottom)
# =============================================================================

# ── 1. DIRECT_LOOKUP ─────────────────────────────────────────────────────────
# Pure numeric ID (8+ digits standalone — e.g. "42570415")
_NUMERIC_ID_RE = re.compile(r"^\s*\d{7,}\s*$")

# Keyword-introduced reference (waybill/HBL/PO/SO/delivery doc + optional value)
# Note: non-verbose (no VERBOSE flag) to avoid # being treated as comment delimiter.
_REFERENCE_KEYWORD_RE = re.compile(
    r"\b("
    r"shipment[-_\s]?(?:number|no|id|[#])"
    r"|waybill|hbl|hawb|mbl|mawb"
    r"|delivery[-_\s]?doc(?:ument)?"
    r"|tracking[-_\s]?(?:number|no|id|[#])"
    r"|purchase[-_\s]?order"
    r"|\bpo\b(?=\s+\w)"
    r"|sales[-_\s]?order"
    r"|\bso\b(?=\s+\w)"
    r"|order[-_\s]?(?:number|no)"
    r")\b",
    re.IGNORECASE,
)

# Alphanumeric reference code (2-4 uppercase letters followed by 6+ digits)
_ALPHA_REF_RE = re.compile(r"\b[A-Z]{2,4}\d{6,}\b")

# ── 0. GREETING / SMALL-TALK ─────────────────────────────────────────────────
# Anchored full-string match — avoids false positives on real data queries.
# Checked FIRST in classify_intent() so greetings never reach Genie.
_GREETING_RE = re.compile(
    r"^(hi+|hello+|hola|hey(?:\s+there)?|howdy|yo|sup"
    r"|good\s+(?:morning|afternoon|evening|day)"
    r"|greetings?|cheers?|ciao|namaste|salut|bonjour"
    r"|thanks?\s*(?:you)?|thx|ty"
    r"|ok+a*y*|okay|sure|yep|yup|great|nice|cool|wow|got\s*it|noted)"
    r"\s*[!.?]*$",
    re.IGNORECASE,
)

# ── 1.5 PART_NUMBER_LOOKUP ────────────────────────────────────────────────────
# When the user explicitly says "part number(s)" / "part no" / "part #", the
# alphanumeric codes in the query are part numbers, NOT shipment IDs.  Must be
# checked BEFORE plain _ALPHA_REF_RE so the explicit keyword wins.
_PART_NUMBER_KEYWORD_RE = re.compile(
    r"\bpart\s*(?:numbers?|nos?\.?|#)\b",
    re.IGNORECASE,
)


# ── 2. DOWNLOAD_REQUEST ──────────────────────────────────────────────────────
_DOWNLOAD_RE = re.compile(
    r"\b(download|export)\b"
    r"|\b(give|get|send)\s+(me\s+)?(the\s+)?(full\s+)?csv\b"
    r"|\bdownload\s+csv\b"
    r"|\bexport\s+(this|all|result|data)\b",
    re.IGNORECASE,
)

# ── 3. SUMMARY_REQUEST ───────────────────────────────────────────────────────
_SUMMARY_RE = re.compile(
    r"\b(quick\s+summary|summarize|summarise|give\s+(me\s+)?a?\s*summary"
    r"|provide\s+a?\s*summary|summary\s+of\s+this|wrap\s+up)\b",
    re.IGNORECASE,
)

# ── 4. DETAIL_LISTING ────────────────────────────────────────────────────────
_DETAIL_LISTING_RE = re.compile(
    r"\b("
    r"list\s+(all|shipment|the)\b"
    r"|give\s+me\s+all\b"
    r"|show\s+me\s+all\b"
    r"|get\s+all\b"
    r"|show\s+(raw|all)\s+(records?|data|rows?)\b"
    r"|full\s+(list|data|records?)\b"
    r"|all\s+shipments?\s+(from|to|via|with|where)\b"
    r"|shipment[-\s]level\s+details?\b"
    r")\b",
    re.IGNORECASE,
)

# ── 5. BROAD_LISTING ─────────────────────────────────────────────────────────
_BROAD_LISTING_RE = re.compile(
    r"""\b(
        which\s+are
      | what\s+are\s+the\s+shipments?
      | show\s+(?:me\s+)?(?:the\s+)?shipments?
      | shipments?\s+from
      | shipments?\s+to
      | shipments?\s+via
      | shipments?\s+using
      | in[-\s]?transit\s+shipments?
      | delayed\s+shipments?
      | late\s+shipments?
      | on[-\s]?time\s+shipments?
      | pending\s+shipments?
      | delivered\s+shipments?
      | cancelled\s+shipments?
      | overdue\s+shipments?
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

# ── 6. AGGREGATION ───────────────────────────────────────────────────────────
# E5: Expanded to cover lane/destination/route/volume phrases without digits,
# and moved BEFORE BROAD_LISTING in classify_intent() to prevent overlapping
# phrases like "which are the top lanes" from routing incorrectly.
_AGGREGATION_RE = re.compile(
    r"\b("
    # Classic analytical keywords
    r"distribution|breakdown"
    # top N with digit, top ten
    r"|top\s+\d+|top\s+ten"
    # top X without digit — lanes, destinations, routes, modes, carriers (E5)
    r"|top\s+lanes?|top\s+destinations?|top\s+routes?|top\s+modes?|top\s+carriers?"
    r"|top\s+(?:transport|shipping)\s+lanes?"
    # busiest/slowest/highest/maximum volume (E5)
    r"|busiest|slowest"
    r"|busiest\s+lanes?|highest\s+volume|maximum\s+volume|most\s+active\s+lanes?"
    # aggregation functions
    r"|average|avg"
    r"|total\s+(?:revenue|count|volume|weight|amount)"
    r"|how\s+many|how\s+much"
    # business metrics
    r"|revenue|profit|margin"
    r"|percentage|ratio|trend|monthly\s+trend"
    r"|compare|versus|\bvs\.?\b"
    # by-clause grouping — extended with volume, lane, route (E5)
    r"|by\s+(?:mode|status|country|destination|origin|bu|business\s+unit|lane|lanes|route|carrier|month|year|week|volume)"
    # count/sum patterns (E5)
    r"|count(?:\s+of)?|count\s+by|number\s+of|shipment\s+count|amount\s+by"
    # time-based aggregation
    r"|monthly|quarterly|yearly|weekly"
    # sorting/ranking
    r"|ranking|rank|sorted\s+by\s+(?:revenue|count|volume)"
    # lane-specific patterns (E5)
    r"|lanes?\s+(?:by|breakdown|analysis|ranking|distribution|with)"
    r"|lanes?\s+with\s+(?:maximum|highest|most|top)"
    r"|lane\s+(?:volume|count|analysis|breakdown|ranking)"
    # volume-by patterns (E5)
    r"|volume\s+by|volume\s+breakdown|volume\s+analysis"
    # mode/status patterns (E5)
    r"|transport\s+mode\s+breakdown|mode\s+distribution|status\s+distribution"
    # E6: Analytical intent vocabulary — must route here (not BROAD_LISTING)
    r"|analysis|analyze|analyse"
    r"|insights?|overview|report\s+on|examine"
    r"|assessment|evaluation"
    r"|how\s+delayed"
    # P1 mirror: summary vocabulary — matches pre_genie_router _AGGREGATION_RE
    r"|summari[sz]e\b|summary\s+of\b|give\s+(?:me\s+)?a?\s*summary\b"
    r")\b",
    re.IGNORECASE,
)

# Lane detection for lane-specific aggregation template
_LANE_RE = re.compile(r"\blanes?\b", re.IGNORECASE)
_TOP_N_RE = re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE)
# Canonical prompt passthrough (contains 'Group by' or similar)
_CANONICAL_SIGNAL_RE = re.compile(
    r"\b(group\s+by|sort\s+by\b.*\bdescending|do\s+not\s+return.*(?:shipment[- ]level|individual\s+shipment))\b",
    re.IGNORECASE,
)


# =============================================================================
# Enrichment templates  (each verified to produce good Genie outputs)
# =============================================================================

# BROAD_LISTING — minimal proven format: produces 10–20 row aggregated table
# + rich text + viz attachment in live API tests.
_TMPL_BROAD = (
    "Summarise {question}: "
    "total count, top 5 destinations, transport mode breakdown, "
    "and status distribution."
)

# AGGREGATION — business analytics framing (generic)
# P1 fix: hardened from soft "unless necessary" to explicit hard contract.
_TMPL_AGGREGATION = (
    "Answer as a business analytics summary for: {question}. "
    "Return grouped or aggregated results suitable for charting. "
    "Include key observations. "
    "Do not return individual shipment-level rows."
)

# LANE AGGREGATION — explicit GROUP BY template (E5, Contract 1)
_TMPL_LANE_AGGREGATION = (
    "Show the top {top_n} shipment lanes by shipment count. "
    "Group by source_ and destination. "
    "Return source_, destination, shipment_count. "
    "Sort by shipment_count descending. "
    "Do not return individual shipment-level rows."
)

# DETAIL_LISTING — detail-oriented but with factual preamble
_TMPL_DETAIL = (
    "Return shipment-level details for: {question}. "
    "Include a concise factual summary (total count, key attributes) "
    "and keep the result table suitable for preview and CSV download."
)

# SUMMARY_REQUEST — used both as first-turn and follow-up enrichment
_TMPL_SUMMARY = (
    "Provide a concise business summary of the current result or "
    "conversation context. Include key numbers and observations."
)

# GENERAL — light framing, low risk of confusion
_TMPL_GENERAL = (
    "Answer clearly and concisely using the shipment dataset. "
    "Include business context where useful: {question}"
)

# PART_NUMBER_LOOKUP — guide Genie to filter on part_number column, not shipment_number_id
_TMPL_PART_NUMBER = (
    "Find all shipments matching these part numbers: {question}. "
    "IMPORTANT: filter using the `part_number` column — these are NOT shipment IDs. "
    "Return a shipment-level table with shipment_number_id, part_number, source_, "
    "destination, execution_status, transportation_mode_desc, and a brief count summary."
)


# =============================================================================
# Public API
# =============================================================================

def classify_intent(user_message: str) -> str:
    """Classify the query intent.  Returns one of:
        DIRECT_LOOKUP | DOWNLOAD_REQUEST | SUMMARY_REQUEST | DETAIL_LISTING |
        AGGREGATION | BROAD_LISTING | GENERAL

    Evaluation order matters — more specific patterns are checked first.
    E5: AGGREGATION is now evaluated BEFORE BROAD_LISTING so that overlapping
    phrases like "which are the top lanes" classify as AGGREGATION, not BROAD_LISTING.
    """
    msg = user_message.strip()

    # 0. Greeting / small-talk — shortcircuit before any data intent.
    #    Pipeline returns a static response; Genie is never called.
    if _GREETING_RE.match(msg):
        return "GREETING"

    # 1. Pure numeric ID (standalone number — no other words)
    if _NUMERIC_ID_RE.match(msg):
        return "DIRECT_LOOKUP"

    # 1.5 Explicit part-number keyword + alphanumeric codes — must be checked
    #     BEFORE plain _ALPHA_REF_RE to prevent part numbers being mistaken for
    #     shipment IDs (e.g. NB15524001 matches DIRECT_LOOKUP otherwise).
    if _PART_NUMBER_KEYWORD_RE.search(msg) and _ALPHA_REF_RE.search(msg):
        return "PART_NUMBER_LOOKUP"

    # 2. Keyword-introduced reference or alphanumeric code
    if _REFERENCE_KEYWORD_RE.search(msg) or _ALPHA_REF_RE.search(msg):
        return "DIRECT_LOOKUP"

    # 3. Download / export request
    if _DOWNLOAD_RE.search(msg):
        return "DOWNLOAD_REQUEST"

    # 4. Summary request  (checked before AGGREGATION to catch "quick summary")
    if _SUMMARY_RE.search(msg):
        return "SUMMARY_REQUEST"

    # 5. Detail listing  (checked before AGGREGATION to avoid misclassification)
    if _DETAIL_LISTING_RE.search(msg):
        return "DETAIL_LISTING"

    # 6. AGGREGATION — E5: evaluated BEFORE BROAD_LISTING
    #    Catches lane/destination/volume analytics phrases that previously fell
    #    through to BROAD_LISTING due to "which are..." leading text.
    if _AGGREGATION_RE.search(msg):
        return "AGGREGATION"

    # 7. Broad listing
    if _BROAD_LISTING_RE.search(msg):
        return "BROAD_LISTING"

    return "GENERAL"


def enrich_prompt(
    user_message: str,
    is_follow_up: bool = False,
) -> EnrichmentResult:
    """Return an EnrichmentResult for the given user message.

    Args:
        user_message: Raw user text (shown in UI unchanged).
        is_follow_up: True when a Genie conversation already exists for this
                      app conversation (genie_conv_id is not None in the
                      pipeline).  When True, only SUMMARY_REQUEST is enriched;
                      all other intents pass through unchanged to preserve
                      Genie's multi-turn context.

    Returns:
        EnrichmentResult dict with original_prompt, enriched_prompt, intent,
        enrichment_applied, and reason.  The caller sends enriched_prompt to
        Genie and displays original_prompt to the user.
    """
    original = user_message.strip()
    intent   = classify_intent(original)

    # ── Never enrich these intents, regardless of follow-up status ──────────
    # GREETING is handled in genie_pipeline.py with a static shortcircuit response
    if intent in ("DIRECT_LOOKUP", "DOWNLOAD_REQUEST", "GREETING"):
        return _result(original, original, intent, False,
                       f"{intent}: pass through unchanged")

    # ── Follow-up: only enrich SUMMARY_REQUEST ───────────────────────────────
    if is_follow_up and intent != "SUMMARY_REQUEST":
        return _result(original, original, intent, False,
                       "follow-up turn: enrichment suppressed to preserve context")

    # ── Apply intent-specific template ──────────────────────────────────────
    if intent == "BROAD_LISTING":
        enriched = _TMPL_BROAD.format(question=original)
        return _result(original, enriched, intent, True,
                       "broad listing query enriched with analytical summary template")

    if intent == "AGGREGATION":
        enriched = _build_aggregation_prompt_enricher(original)
        return _result(original, enriched, intent, True,
                       "aggregation query enriched with analytics framing template")

    if intent == "DETAIL_LISTING":
        enriched = _TMPL_DETAIL.format(question=original)
        return _result(original, enriched, intent, True,
                       "detail listing enriched with factual summary framing")

    if intent == "SUMMARY_REQUEST":
        return _result(original, _TMPL_SUMMARY, intent, True,
                       "summary request replaced with context summary template")

    if intent == "PART_NUMBER_LOOKUP":
        enriched = _TMPL_PART_NUMBER.format(question=original)
        return _result(original, enriched, intent, True,
                       "part number lookup enriched to target part_number column")

    # GENERAL
    enriched = _TMPL_GENERAL.format(question=original)
    return _result(original, enriched, intent, True,
                   "general query enriched with light business context framing")


# =============================================================================
# Internal helpers
# =============================================================================

def _build_aggregation_prompt_enricher(question: str) -> str:
    """Build the AGGREGATION enriched prompt.

    Mirrors the logic in pre_genie_router._build_aggregation_prompt:
    - Canonical prompts (containing 'Group by' etc.) pass through unchanged.
    - Lane queries use the explicit GROUP BY lane template.
    - All others use the generic analytics framing template.
    """
    # Pass through canonical prompts unchanged
    if _CANONICAL_SIGNAL_RE.search(question):
        return question

    # Detect top-N number
    n_match = _TOP_N_RE.search(question)
    top_n = n_match.group(1) if n_match else "10"

    # Lane-specific template
    if _LANE_RE.search(question):
        return _TMPL_LANE_AGGREGATION.format(top_n=top_n)

    # Generic analytics framing
    return _TMPL_AGGREGATION.format(question=question)


def _result(
    original: str,
    enriched: str,
    intent: str,
    applied: bool,
    reason: str,
) -> EnrichmentResult:
    return EnrichmentResult(
        original_prompt=original,
        enriched_prompt=enriched,
        intent=intent,
        enrichment_applied=applied,
        reason=reason,
    )
