"""Pre-Genie routing layer for TransparencE.

This module is the single source of truth for all *pre-Genie* decisions.
It decides whether a prompt should:

* return a local response immediately,
* reuse prior app-side context (corrections / downloads), or
* be sent to Genie, optionally with safe prompt enrichment.

The router always runs, even when prompt enrichment is disabled.  Safety
routing must never be bypassed.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, TypedDict

try:
    from app.config import settings as _settings
    _APP_PUBLIC_NAME = _settings.APP_PUBLIC_NAME
    _APP_BRAND_OWNER = _settings.APP_BRAND_OWNER
except Exception:
    _APP_PUBLIC_NAME = "TransparencE Shipment Intelligence"
    _APP_BRAND_OWNER = "GLOG team"


class PreGenieRouteDecision(TypedDict, total=False):
    """Structured decision returned by route_pre_genie()."""

    original_prompt: str
    normalized_prompt: str
    intent: str
    entity_type: Optional[str]
    entities: List[str]
    filters: Dict[str, str]
    is_follow_up: bool
    is_correction: bool
    should_call_genie: bool
    local_response: Optional[str]
    enriched_prompt: Optional[str]
    reason: str
    download_key: Optional[str]
    download_url: Optional[str]
    export_id: Optional[str]
    export_status: Optional[str]
    export_mode: Optional[str]
    export_row_count: Optional[int]


# =============================================================================
# Regexes — evaluated in priority order
# =============================================================================

_IDENTITY_RE = re.compile(
    r"\b("
    # "who are you" — must come before the partial "who … you" branch
    r"who\s+are\s+you"
    r"|who\s+(built|made|developed|created)\s+(you|this\s+(?:app|chatbot|tool|application))"
    r"|what\s+are\s+you"
    r"|what\s+is\s+this\s+(app|chatbot|tool|assistant|application)"
    r"|what\s+is\s+transparen[ct]e"
    r"|tell\s+me\s+about\s+yourself"
    r"|introduce\s+yourself"
    r"|what\s+can\s+you\s+do"
    r"|are\s+you\s+(chatgpt|databricks|genie|claude|an\s+ai\s+agent|an\s+assistant|an\s+ai)"
    r"|which\s+model\s+are\s+you"
    r"|what\s+model\s+are\s+you"
    r"|what\s+(powers|is\s+powering)\s+you"
    r"|who\s+developed\s+this\s+(?:app|chatbot|tool|application)"
    r"|who\s+created\s+this\s+(?:app|chatbot|tool|application)"
    r"|what\s+backend\s+are\s+you\s+using"
    r"|what\s+technology\s+are\s+you\s+using"
    r")\b",
    re.IGNORECASE,
)

_MODEL_OR_BACKEND_RE = re.compile(
    r"\b(model|backend|vendor|provider|powering|powered|technology|engine|llm)\b",
    re.IGNORECASE,
)

# Safety-net sanitizer: forbidden vendor/backend terms that must never appear
# in a user-facing chat response from a local (non-Genie) handler.
# Internal logs and code references are unaffected — this is UI-only.
_FORBIDDEN_IDENTITY_RE = re.compile(
    r"\b(genie|databricks|claude|openai|llm|lbn_with_scorecard)\b"
    # "model" alone is too broad (e.g. "transport mode") — exclude here;
    # _MODEL_OR_BACKEND_RE handles it at response-generation time.
    ,
    re.IGNORECASE,
)

_GREETING_RE = re.compile(
    r"^(hi+|hello+|hola|hey(?:\s+there)?|howdy|yo|sup"
    r"|good\s+(?:morning|afternoon|evening|day)"
    r"|greetings?|cheers?|ciao|namaste|salut|bonjour"
    r"|thanks?(?:\s+you)?|thank\s+you|thx|ty"
    r"|ok+a*y*|okay|sure|great|nice|cool|wow|got\s*it|noted)"
    r"\s*[!.?]*$",
    re.IGNORECASE,
)

_DOWNLOAD_RE = re.compile(
    r"\b(download|export)\b"
    r"|\b(give|get|send)\s+(me\s+)?(the\s+)?(full\s+)?csv\b"
    r"|\bdownload\s+csv\b"
    r"|\bexport\s+(this|all|result|data)\b",
    re.IGNORECASE,
)

_CORRECTION_RE = re.compile(
    r"\b("
    r"these\s+are\s+.*not\s+.*shipment\s+ids?"
    r"|those\s+are\s+.*part\s+numbers?"
    r"|i\s+meant\s+part\s+numbers?"
    r"|not\s+shipment\s+id[s]?,?\s+use\s+part\s+number"
    r"|these\s+are\s+material\s+numbers?"
    r"|those\s+are\s+product\s+numbers?"
    r"|use\s+part[_\s]?number\s+instead"
    r"|not\s+shipment\s+ids?"
    r"|no\s+shipment\s+ids?"
    r"|part\s+numbers?,?\s+not\s+shipment\s+ids?"
    r")\b",
    re.IGNORECASE,
)

_PART_NUMBER_KEYWORD_RE = re.compile(
    r"\b(part\s*(?:numbers?|nos?\.?|#)|part_number|material\s*(?:numbers?|nos?\.?|#)"
    r"|product\s*(?:numbers?|nos?\.?|#)|item\s*(?:numbers?|nos?\.?|#)|sku)\b",
    re.IGNORECASE,
)

_SHIPMENT_ID_KEYWORD_RE = re.compile(
    r"\b(shipment\s*(?:id|ids|number|numbers|no\.?|#)|shipment_number_id)\b",
    re.IGNORECASE,
)

_WAYBILL_KEYWORD_RE = re.compile(r"\bwaybill\b", re.IGNORECASE)
_HBL_KEYWORD_RE = re.compile(r"\bhbl\b", re.IGNORECASE)
_HAWB_KEYWORD_RE = re.compile(r"\bhawb\b", re.IGNORECASE)
_MBL_KEYWORD_RE = re.compile(r"\bmbl\b", re.IGNORECASE)
_MAWB_KEYWORD_RE = re.compile(r"\bmawb\b", re.IGNORECASE)
_PO_KEYWORD_RE = re.compile(r"\b(po|purchase\s+order)\b", re.IGNORECASE)
_SO_KEYWORD_RE = re.compile(r"\b(so|sales\s+order)\b", re.IGNORECASE)
_DELIVERY_DOC_KEYWORD_RE = re.compile(r"\bdelivery\s+doc(?:ument)?\b", re.IGNORECASE)

_NUMERIC_ID_RE = re.compile(r"^\s*\d{7,}\s*$")
_ALPHA_REF_RE = re.compile(r"\b[A-Z]{2,4}\d{6,}\b")
_ENTITY_VALUE_RE = re.compile(
    r"\b(?:[A-Z0-9]{2,}(?:[-_/][A-Z0-9]{1,})+|[A-Z]{2,4}\d{6,}|\d{7,})\b",
    re.IGNORECASE,
)

# P1 fix: standalone summari[sz]e removed — topic-based summaries are analytical
# requests (caught at step 8 by _AGGREGATION_RE), not conversational follow-ups.
# Context-dependent variants (summari[sz]e this / the above) are kept here.
_FOLLOW_UP_RE = re.compile(
    r"^(what\s+about|how\s+about|only\s+|just\s+|show\s+the\s+same|same\s+|"
    r"show\s+by\s+|summari[sz]e\s+(?:this|the\s+above)\b|quick\s+summary|this\s+time|"
    r"for\s+the\s+same|those\s+only|these\s+only|and\s+what\s+about)\b",
    re.IGNORECASE,
)

_DETAIL_LISTING_RE = re.compile(
    r"\b(list\s+(all|shipment|the)|give\s+me\s+all|show\s+me\s+all|get\s+all|"
    r"show\s+(raw|all)\s+(records?|data|rows?)|full\s+(list|data|records?)|"
    r"all\s+shipments?\s+(from|to|via|with|where)|shipment[-\s]level\s+details?)\b",
    re.IGNORECASE,
)

_BROAD_LISTING_RE = re.compile(
    r"\b(which\s+are|show\s+(?:me\s+)?(?:the\s+)?shipments?|shipments?\s+from|"
    r"shipments?\s+to|shipments?\s+via|shipments?\s+using|in[-\s]?transit\s+shipments?|"
    r"delayed\s+shipments?|late\s+shipments?|on[-\s]?time\s+shipments?|"
    r"pending\s+shipments?|delivered\s+shipments?|cancelled\s+shipments?|overdue\s+shipments?)\b",
    re.IGNORECASE,
)

# =============================================================================
# AGGREGATION regex — evaluated BEFORE BROAD_LISTING.
# Must catch ranking, distribution, breakdown, lane/route/destination analytics,
# and any aggregated metric question.  Expanded to cover enterprise contracts:
#   - lane-related phrases without digit (top lanes, busiest lanes, etc.)
#   - volume/count-by phrasing (by volume, count by, shipment count)
#   - top-N variants without explicit number (top destinations, top routes)
#   - transport mode and status distribution patterns
# =============================================================================
_AGGREGATION_RE = re.compile(
    r"\b("
    # Classic analytical keywords
    r"distribution|breakdown"
    # top N with digit, top ten
    r"|top\s+\d+|top\s+ten"
    # top X without digit — lanes, destinations, routes, modes, carriers
    r"|top\s+lanes?|top\s+destinations?|top\s+routes?|top\s+modes?|top\s+carriers?"
    r"|top\s+(?:transport|shipping|shipment)\s+lanes?"
    # busiest/slowest/highest/maximum volume
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
    # by-clause grouping — extended with volume, lane, route
    r"|by\s+(?:mode|status|country|destination|origin|bu|business\s+unit|lane|lanes|route|carrier|month|year|week|volume)"
    # count/sum patterns
    r"|count(?:\s+of)?|count\s+by|number\s+of|shipment\s+count|amount\s+by"
    # time-based aggregation
    r"|monthly|quarterly|yearly|weekly"
    # sorting/ranking
    r"|ranking|rank|sorted\s+by\s+(?:revenue|count|volume)"
    # lane-specific patterns
    r"|lanes?\s+(?:by|breakdown|analysis|ranking|distribution|with)"
    r"|lanes?\s+with\s+(?:maximum|highest|most|top)"
    r"|lane\s+(?:volume|count|analysis|breakdown|ranking)"
    # volume-by patterns
    r"|volume\s+by|volume\s+breakdown|volume\s+analysis"
    # mode/status distribution patterns
    r"|transport\s+mode\s+breakdown|mode\s+distribution|status\s+distribution"
    r"|delayed.*by\s+route"
    # E6: Analytical intent vocabulary — prompts explicitly requesting analysis,
    # insights, overview, or examination must route here (not BROAD_LISTING)
    # so Genie receives the stronger analytics-framing template.
    r"|analysis|analyze|analyse"
    r"|insights?|overview|report\s+on|examine"
    r"|assessment|evaluation"
    r"|how\s+delayed"
    # P1 fix: summary vocab routes AGGREGATION (step 8) before BROAD_LISTING.
    r"|summari[sz]e\b|summary\s+of\b|give\s+(?:me\s+)?a?\s*summary\b"
    r")\b",
    re.IGNORECASE,
)

_SUMMARY_RE = re.compile(
    r"\b(quick\s+summary|summarize|summarise|give\s+(me\s+)?a?\s*summary|"
    r"provide\s+a?\s*summary|summary\s+of\s+this|wrap\s+up)\b",
    re.IGNORECASE,
)

_COUNTRY_CODE_RE = re.compile(r"\b([A-Z]{2})\b")

# Logistics and business domain vocabulary.  A prompt containing at least one
# of these words carries real domain intent and is allowed through to Genie
# even if it is short or informal.  Used exclusively by _is_low_information()
# (step 12 NON_BUSINESS_OR_SMALL_TALK guard).
_DOMAIN_VOCAB_RE = re.compile(
    r"\b("
    r"shipment|shipments|delivery|deliveries|delivered"
    r"|delay|delayed|delays"
    r"|route|routes|lane|lanes"
    r"|revenue|sales|amount"
    r"|status|transit|transport|transportation|mode|modes"
    r"|origin|source|destination|destinations"
    r"|waybill|tracking|eta|etd|ata"
    r"|hbl|hawb|mbl|mawb"
    r"|carrier|forwarder|ffw"
    r"|air|ocean|road|rail"
    r"|part|material|product|item|sku"
    r"|order|purchase"
    r"|weight|chargeable|pgi|gr"
    r"|complete|completed|pending|cancelled|overdue|late"
    r"|bu|detail|details"
    r"|summary|summarize|summaries|overview|breakdown|analysis|report|trend"
    r")\b",
    re.IGNORECASE,
)

# Lane detection in aggregation context (used by _build_aggregation_prompt)
_LANE_RE = re.compile(r"\blanes?\b", re.IGNORECASE)
# Extracts top-N number from user prompt
_TOP_N_RE = re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE)
# Detects already-canonical prompts (Group by / Sort by / Do not return shipment-level)
_CANONICAL_SIGNAL_RE = re.compile(
    r"\b(group\s+by|sort\s+by\b.*\bdescending|do\s+not\s+return.*(?:shipment[- ]level|individual\s+shipment))\b",
    re.IGNORECASE,
)


# =============================================================================
# Public API
# =============================================================================


def route_pre_genie(
    user_message: str,
    *,
    is_follow_up: bool = False,
    previous_entities: Optional[List[str]] = None,
    previous_entity_type: Optional[str] = None,
    previous_filters: Optional[Dict[str, str]] = None,
    previous_intent: Optional[str] = None,
    last_download_key: Optional[str] = None,
    last_export_id: Optional[str] = None,
    last_export_status: Optional[str] = None,
    last_export_mode: Optional[str] = None,
    last_export_row_count: Optional[int] = None,
    latest_table_result: Optional[Dict] = None,
    export_job_manager: Any = None,
    enable_prompt_enrichment: bool = True,
    app_public_name: str = _APP_PUBLIC_NAME,
    app_brand_owner: str = _APP_BRAND_OWNER,
) -> PreGenieRouteDecision:
    """Route a user prompt before any Genie call occurs.

    Applies a safety-net sanitizer to all local (non-Genie) responses so that
    forbidden vendor/backend terms can never surface in the chat UI even if an
    edge case bypasses the primary identity check.

    Args:
        latest_table_result: Per-message export metadata dict from the most recent
            table response in this conversation.  Keyed: download_key, export_id,
            export_status, export_mode, export_row_count, query_description.
            When present, DOWNLOAD_REQUEST resolves against this rather than the
            flat last_download_key (fixes stale-export bug).
        export_job_manager: Optional ExportJobManager instance.  When provided,
            DOWNLOAD_REQUEST performs a live status check for async exports whose
            latest_table_result is stale (export_status queued/running but the
            background thread has since completed).
    """
    decision = _route_impl(
        user_message,
        is_follow_up=is_follow_up,
        previous_entities=previous_entities,
        previous_entity_type=previous_entity_type,
        previous_filters=previous_filters,
        previous_intent=previous_intent,
        last_download_key=last_download_key,
        last_export_id=last_export_id,
        last_export_status=last_export_status,
        last_export_mode=last_export_mode,
        last_export_row_count=last_export_row_count,
        latest_table_result=latest_table_result,
        export_job_manager=export_job_manager,
        enable_prompt_enrichment=enable_prompt_enrichment,
        app_public_name=app_public_name,
        app_brand_owner=app_brand_owner,
    )
    # Safety net: sanitize any local response containing forbidden vendor terms.
    # Does not affect Genie-bound prompts, logs, or internal code references.
    if not decision.get("should_call_genie") and decision.get("local_response"):
        decision["local_response"] = _sanitize_local_response(
            decision["local_response"], app_public_name, app_brand_owner
        )
    return decision


# =============================================================================
# Internal routing implementation
# =============================================================================


def _route_impl(
    user_message: str,
    *,
    is_follow_up: bool = False,
    previous_entities: Optional[List[str]] = None,
    previous_entity_type: Optional[str] = None,
    previous_filters: Optional[Dict[str, str]] = None,
    previous_intent: Optional[str] = None,
    last_download_key: Optional[str] = None,
    last_export_id: Optional[str] = None,
    last_export_status: Optional[str] = None,
    last_export_mode: Optional[str] = None,
    last_export_row_count: Optional[int] = None,
    latest_table_result: Optional[Dict] = None,
    export_job_manager: Any = None,
    enable_prompt_enrichment: bool = True,
    app_public_name: str = _APP_PUBLIC_NAME,
    app_brand_owner: str = _APP_BRAND_OWNER,
) -> PreGenieRouteDecision:
    """Core routing logic — called exclusively by route_pre_genie()."""

    original = user_message or ""
    normalized = _normalize_prompt(original)
    previous_entities = previous_entities or []
    previous_filters = previous_filters or {}

    base: PreGenieRouteDecision = {
        "original_prompt": original,
        "normalized_prompt": normalized,
        "intent": "GENERAL",
        "entity_type": None,
        "entities": [],
        "filters": {},
        "is_follow_up": bool(is_follow_up),
        "is_correction": False,
        "should_call_genie": True,
        "local_response": None,
        "enriched_prompt": normalized or None,
        "reason": "default business query",
        "download_key": None,
        "download_url": None,
        "export_id": None,
        "export_status": None,
        "export_mode": None,
        "export_row_count": None,
    }

    if not normalized:
        return _with(base,
            intent="GREETING",
            should_call_genie=False,
            local_response=(
                "Hi, I can help you with shipments, delays, routes, revenue, "
                "delivery status, and logistics trends. What would you like to check?"
            ),
            enriched_prompt=None,
            reason="empty prompt treated as greeting/help",
        )

    # 1. IDENTITY_OR_ABOUT_APP
    if _IDENTITY_RE.search(normalized):
        response = _identity_response(normalized, app_public_name, app_brand_owner)
        return _with(base,
            intent="IDENTITY_OR_ABOUT_APP",
            should_call_genie=False,
            local_response=response,
            enriched_prompt=None,
            reason="identity/about-app prompt handled locally",
        )

    # 2. GREETING
    if _GREETING_RE.match(normalized):
        return _with(base,
            intent="GREETING",
            should_call_genie=False,
            local_response=(
                "Hi, I can help you with shipments, delays, routes, revenue, "
                "delivery status, and logistics trends. What would you like to check?"
            ),
            enriched_prompt=None,
            reason="greeting/small-talk handled locally",
        )

    # 3. DOWNLOAD_REQUEST
    if _DOWNLOAD_RE.search(normalized):
        return _handle_download_request(
            base=base,
            latest_table_result=latest_table_result,
            last_download_key=last_download_key,
            last_export_id=last_export_id,
            last_export_status=last_export_status,
            last_export_mode=last_export_mode,
            last_export_row_count=last_export_row_count,
            export_job_manager=export_job_manager,
        )

    # 4. CORRECTION
    if _CORRECTION_RE.search(normalized):
        corrected_entity_type = _corrected_entity_type(normalized) or previous_entity_type or "part_number"
        if previous_entities:
            merged_filters = dict(previous_filters)
            return _with(base,
                intent="CORRECTION",
                entity_type=corrected_entity_type,
                entities=previous_entities,
                filters=merged_filters,
                is_correction=True,
                should_call_genie=True,
                enriched_prompt=_build_entity_prompt(corrected_entity_type, previous_entities, merged_filters),
                reason="correction applied using prior entities and corrected entity type",
            )
        return _with(base,
            intent="CORRECTION",
            entity_type=corrected_entity_type,
            is_correction=True,
            should_call_genie=False,
            local_response=(
                "Understood. Please resend the values you want me to use so I can "
                f"search by {corrected_entity_type.replace('_', ' ')} correctly."
            ),
            enriched_prompt=None,
            reason="correction received without prior entities to reuse",
        )

    # 5. EXPLICIT_ENTITY_SEARCH
    explicit_entity_type = _detect_explicit_entity_type(normalized)
    extracted_entities = _extract_entities(normalized)
    extracted_filters = _extract_filters(normalized)
    if explicit_entity_type:
        if explicit_entity_type == "shipment_id" and not extracted_entities and _NUMERIC_ID_RE.match(normalized):
            extracted_entities = [normalized]
        return _with(base,
            intent="EXPLICIT_ENTITY_SEARCH",
            entity_type=explicit_entity_type,
            entities=extracted_entities,
            filters=extracted_filters,
            should_call_genie=True,
            enriched_prompt=_build_entity_prompt(
                explicit_entity_type,
                extracted_entities,
                extracted_filters,
                enable_prompt_enrichment=enable_prompt_enrichment,
            ),
            reason="explicit entity keyword overrides generic alphanumeric detection",
        )

    # 6. DIRECT_LOOKUP
    if _is_direct_lookup(normalized):
        return _with(base,
            intent="DIRECT_LOOKUP",
            entity_type="shipment_id",
            entities=_extract_entities(normalized) or ([normalized] if _NUMERIC_ID_RE.match(normalized) else []),
            filters=extracted_filters,
            should_call_genie=True,
            enriched_prompt=normalized,
            reason="exact reference lookup sent through unchanged",
        )

    # 7. TRUE_FOLLOW_UP
    if is_follow_up and _is_true_follow_up(normalized, previous_intent):
        return _with(base,
            intent="TRUE_FOLLOW_UP",
            filters=extracted_filters,
            should_call_genie=True,
            enriched_prompt=_build_follow_up_prompt(normalized),
            reason="language indicates a genuine conversational follow-up",
        )

    # 8. AGGREGATION — evaluated BEFORE BROAD_LISTING so that overlapping
    #    phrases like "which are the top lanes" are captured here first.
    if _AGGREGATION_RE.search(normalized):
        return _with(base,
            intent="AGGREGATION",
            filters=extracted_filters,
            should_call_genie=True,
            enriched_prompt=(
                _build_aggregation_prompt(normalized) if enable_prompt_enrichment else normalized
            ),
            reason="aggregation / chartable business query",
        )

    # 9. BROAD_LISTING
    if _BROAD_LISTING_RE.search(normalized):
        return _with(base,
            intent="BROAD_LISTING",
            filters=extracted_filters,
            should_call_genie=True,
            enriched_prompt=(
                _build_broad_prompt(normalized) if enable_prompt_enrichment else normalized
            ),
            reason="broad shipment listing query",
        )

    # 10. DETAIL_LISTING
    if _DETAIL_LISTING_RE.search(normalized):
        return _with(base,
            intent="DETAIL_LISTING",
            filters=extracted_filters,
            should_call_genie=True,
            enriched_prompt=(
                _build_detail_prompt(normalized) if enable_prompt_enrichment else normalized
            ),
            reason="detail listing query",
        )

    # 11. SUMMARY_REQUEST
    if _SUMMARY_RE.search(normalized):
        return _with(base,
            intent="SUMMARY_REQUEST",
            should_call_genie=True,
            enriched_prompt=(
                _build_summary_prompt() if enable_prompt_enrichment else normalized
            ),
            reason="summary request",
        )

    # 12. NON_BUSINESS_OR_SMALL_TALK
    if _is_low_information(normalized, extracted_entities):
        return _with(base,
            intent="NON_BUSINESS_OR_SMALL_TALK",
            should_call_genie=False,
            local_response=(
                "I can help with shipment, delay, route, revenue, delivery status, and "
                "logistics questions. Please provide a shipment number, part number, "
                "route, status, or analysis request."
            ),
            enriched_prompt=None,
            reason="low-information prompt blocked; no logistics domain signal detected",
        )

    # 13. GENERAL
    return _with(base,
        intent="GENERAL",
        filters=extracted_filters,
        should_call_genie=True,
        enriched_prompt=(
            _build_general_prompt(normalized) if enable_prompt_enrichment else normalized
        ),
        reason="general business/data question",
    )


# =============================================================================
# Download request handler (isolated for clarity)
# =============================================================================


def _handle_download_request(
    base: PreGenieRouteDecision,
    *,
    latest_table_result: Optional[Dict],
    last_download_key: Optional[str],
    last_export_id: Optional[str],
    last_export_status: Optional[str],
    last_export_mode: Optional[str],
    last_export_row_count: Optional[int],
    export_job_manager: Any = None,
) -> PreGenieRouteDecision:
    """Resolve a DOWNLOAD_REQUEST against the best available export context.

    Priority:
      1. latest_table_result (message-level, set when a table response was created)
         1a. If download_key is present → ready.
         1b. If export_status queued/running → do a live ExportJobManager lookup
             (Part 2 fix: session ltr may be stale after async export completes).
             If live job is ready → serve it. If failed → report failure.
             Otherwise → return still-preparing.
         1c. If export_status failed → report failure.
         1d. No export (silent failure) → not available message.
      2. last_download_key (conversation-level flat slot, legacy fallback)
      3. No context — ask the user to run a table query first.

    Contract 7 compliance:
      - Only serves a download if the latest table result actually has one.
      - If the latest table has no export (creation failed silently), returns a
        meaningful "not available" message rather than serving a stale older export.
    """
    # Priority 1: message-level context from the latest table result
    if latest_table_result is not None:
        ltr_key = latest_table_result.get("download_key")
        ltr_status = latest_table_result.get("export_status")
        ltr_row_count = latest_table_result.get("export_row_count")
        ltr_export_id = latest_table_result.get("export_id")
        ltr_mode = latest_table_result.get("export_mode")

        if ltr_key:
            return _with(base,
                intent="DOWNLOAD_REQUEST",
                should_call_genie=False,
                local_response="Your CSV is ready. Use the download button to export the latest result.",
                enriched_prompt=None,
                download_key=ltr_key,
                download_url=f"/api/download/{ltr_key}",
                export_id=ltr_export_id,
                export_status="ready",
                export_mode=ltr_mode,
                export_row_count=ltr_row_count,
                reason="download request served from latest table result context",
            )
        if ltr_status in ("queued", "running"):
            # Part 2: live fallback — check ExportJobManager for actual current status.
            # The session's latest_table_result may be stale (captured when export
            # was initially queued).  If the async thread has since completed,
            # ExportJobManager holds the authoritative ready state.
            if export_job_manager is not None and ltr_export_id:
                try:
                    _live_job = export_job_manager.get_job(ltr_export_id)
                    if _live_job is not None:
                        if _live_job.status == "ready" and _live_job.download_key:
                            return _with(base,
                                intent="DOWNLOAD_REQUEST",
                                should_call_genie=False,
                                local_response="Your CSV is ready. Use the download button to export the latest result.",
                                enriched_prompt=None,
                                download_key=_live_job.download_key,
                                download_url=f"/api/download/{_live_job.download_key}",
                                export_id=ltr_export_id,
                                export_status="ready",
                                export_mode=_live_job.mode,
                                export_row_count=_live_job.row_count,
                                reason="download request served from live ExportJobManager (session ltr was stale)",
                            )
                        if _live_job.status == "failed":
                            return _with(base,
                                intent="DOWNLOAD_REQUEST",
                                should_call_genie=False,
                                local_response="The CSV export failed. Please narrow the query and try again.",
                                enriched_prompt=None,
                                export_status="failed",
                                reason="download request: live ExportJobManager shows failed",
                            )
                except Exception:  # noqa: BLE001
                    pass  # non-fatal: fall through to still-preparing below
            return _with(base,
                intent="DOWNLOAD_REQUEST",
                should_call_genie=False,
                local_response="Your CSV export is still being prepared. Please try again shortly.",
                enriched_prompt=None,
                export_id=ltr_export_id,
                export_status=ltr_status,
                reason="download requested while latest export is still preparing",
            )
        if ltr_status == "failed":
            return _with(base,
                intent="DOWNLOAD_REQUEST",
                should_call_genie=False,
                local_response="The CSV export failed. Please narrow the query and try again.",
                enriched_prompt=None,
                export_status="failed",
                reason="download requested after latest export failure",
            )
        # Latest table had no export (silent creation failure — do NOT serve stale old key)
        return _with(base,
            intent="DOWNLOAD_REQUEST",
            should_call_genie=False,
            local_response=(
                "The latest result is not available for download yet. "
                "Please try running the query again to generate a fresh export."
            ),
            enriched_prompt=None,
            reason="download requested but latest table has no export available",
        )

    # Priority 2: legacy flat last_download_key (older sessions / backward compat)
    if last_download_key:
        return _with(base,
            intent="DOWNLOAD_REQUEST",
            should_call_genie=False,
            local_response="Your CSV is ready. Use the download button to export the latest result.",
            enriched_prompt=None,
            download_key=last_download_key,
            download_url=f"/api/download/{last_download_key}",
            export_id=last_export_id,
            export_status="ready",
            export_mode=last_export_mode,
            export_row_count=last_export_row_count,
            reason="download request served from session download key (legacy fallback)",
        )
    if last_export_status in {"queued", "running"}:
        return _with(base,
            intent="DOWNLOAD_REQUEST",
            should_call_genie=False,
            local_response="Your CSV export is still being prepared. Please try again shortly.",
            enriched_prompt=None,
            export_id=last_export_id,
            export_status=last_export_status,
            export_mode=last_export_mode,
            export_row_count=last_export_row_count,
            reason="download requested while prior export is still preparing",
        )
    if last_export_status == "failed":
        return _with(base,
            intent="DOWNLOAD_REQUEST",
            should_call_genie=False,
            local_response="The previous CSV export failed. Please narrow the query and try again.",
            enriched_prompt=None,
            export_id=last_export_id,
            export_status="failed",
            export_mode=last_export_mode,
            reason="download requested after prior export failure",
        )

    # Priority 3: no context at all
    return _with(base,
        intent="DOWNLOAD_REQUEST",
        should_call_genie=False,
        local_response=(
            "I don't have a downloadable result yet. Please run a shipment or "
            "table query first, then I can prepare a CSV export."
        ),
        enriched_prompt=None,
        reason="download requested with no prior downloadable result",
    )


# =============================================================================
# Helpers
# =============================================================================


def _normalize_prompt(text: str) -> str:
    return " ".join((text or "").strip().split())



def _with(base: PreGenieRouteDecision, **updates) -> PreGenieRouteDecision:
    out = dict(base)
    out.update(updates)
    return out



def _identity_response(text: str, app_public_name: str, app_brand_owner: str) -> str:
    """Generate the controlled identity response.

    Only applies to user-facing chat text. Internal logs and code
    identifiers are unaffected.
    """
    if _FORBIDDEN_IDENTITY_RE.search(text):
        return (
            f"This is the {app_public_name} application developed by the "
            f"{app_brand_owner} to help users analyze shipment, delay, route, "
            f"revenue, and logistics data."
        )
    return (
        f"I'm {app_public_name}, developed by the {app_brand_owner} to help "
        f"you analyze shipment data, delays, routes, revenue, and logistics trends. "
        f"Ask me about shipments, delays, transport modes, business units, or "
        f"anything in your shipment dataset."
    )


def _sanitize_local_response(
    text: str,
    app_public_name: str,
    app_brand_owner: str,
) -> str:
    """Replace any forbidden vendor/backend terms in a local response.

    Only applies to user-facing chat text. Internal logs and code
    identifiers are unaffected.
    """
    if _FORBIDDEN_IDENTITY_RE.search(text):
        return (
            f"This is the {app_public_name} application developed by the "
            f"{app_brand_owner} to help users analyze shipment, delay, route, "
            f"revenue, and logistics data."
        )
    return text



def _detect_explicit_entity_type(text: str) -> Optional[str]:
    if _PART_NUMBER_KEYWORD_RE.search(text):
        return "part_number"
    if _SHIPMENT_ID_KEYWORD_RE.search(text):
        return "shipment_id"
    if _WAYBILL_KEYWORD_RE.search(text):
        return "waybill"
    if _HBL_KEYWORD_RE.search(text):
        return "hbl"
    if _HAWB_KEYWORD_RE.search(text):
        return "hawb"
    if _MBL_KEYWORD_RE.search(text):
        return "mbl"
    if _MAWB_KEYWORD_RE.search(text):
        return "mawb"
    if _PO_KEYWORD_RE.search(text):
        return "purchase_order"
    if _SO_KEYWORD_RE.search(text):
        return "sales_order"
    if _DELIVERY_DOC_KEYWORD_RE.search(text):
        return "delivery_document"
    return None



def _corrected_entity_type(text: str) -> Optional[str]:
    if re.search(r"\b(part|material|product|item|sku)\b", text, re.IGNORECASE):
        return "part_number"
    if re.search(r"\bshipment\s+id\b|\bshipment\s+number\b|\bshipment_number_id\b", text, re.IGNORECASE):
        return "shipment_id"
    return None



def _extract_entities(text: str) -> List[str]:
    values = []
    seen = set()
    for token in _ENTITY_VALUE_RE.findall(text.upper()):
        cleaned = token.strip(" ,.;:")
        if cleaned in {"US", "CN", "DE", "IN", "MX", "BR", "TH", "JP", "KR", "PL", "HK", "VN", "CZ", "TW"}:
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            values.append(cleaned)
    return values



def _extract_filters(text: str) -> Dict[str, str]:
    filters: Dict[str, str] = {}
    lower = text.lower()

    m = re.search(r"\b(?:going\s+to|to|destination\s+is|destination\s*=)\s+([A-Z]{2}|[A-Za-z]+(?:\s+[A-Za-z]+)?)\b", text, re.IGNORECASE)
    if m:
        filters["destination"] = m.group(1).upper()

    m = re.search(r"\b(?:from|origin\s+is|source\s+is|source\s*=)\s+([A-Z]{2}|[A-Za-z]+(?:\s+[A-Za-z]+)?)\b", text, re.IGNORECASE)
    if m:
        filters["source"] = m.group(1).upper()

    if re.search(r"\bvia\s+air\b|\bair\s+transport\b|\bair\b", lower):
        filters["transport_mode"] = "AIR"
    elif re.search(r"\bvia\s+(ocean|sea)\b|\bocean\s+transport\b|\bocean\b|\bsea\b", lower):
        filters["transport_mode"] = "OCEAN"
    elif re.search(r"\broad\b|\btruck\b", lower):
        filters["transport_mode"] = "ROAD"
    elif re.search(r"\brail\b", lower):
        filters["transport_mode"] = "RAIL"

    if re.search(r"\bin\s+transit\b", lower):
        filters["status"] = "IN TRANSIT"
    elif re.search(r"\bdelayed\b|\blate\b|\boverdue\b", lower):
        filters["status"] = "DELAYED"
    elif re.search(r"\bpending\b", lower):
        filters["status"] = "PENDING"
    elif re.search(r"\bdelivered\b", lower):
        filters["status"] = "DELIVERED"
    elif re.search(r"\bcancelled\b", lower):
        filters["status"] = "CANCELLED"

    return filters



def _is_direct_lookup(text: str) -> bool:
    if _NUMERIC_ID_RE.match(text):
        return True
    if re.search(r"\bshipment\s+(?:id|number|no\.?|#)?\s*[A-Z0-9-]{7,}\b", text, re.IGNORECASE):
        return True
    if _ALPHA_REF_RE.search(text) and not _PART_NUMBER_KEYWORD_RE.search(text):
        return True
    return False



def _is_true_follow_up(text: str, previous_intent: Optional[str]) -> bool:
    lower = text.lower()
    if _FOLLOW_UP_RE.search(text):
        return True
    if re.search(r"\b(only|same|those|these|them|it|that|this)\b", text, re.IGNORECASE):
        return True
    if previous_intent and re.search(r"^(which|what)\s+are\b", lower) and re.search(
        r"\b(in\s+transit|delayed|late|pending|delivered|cancelled|overdue|air|ocean|sea|rail|road|truck)\b",
        lower,
    ):
        return True
    # P1 fix: removed overly-broad _SUMMARY_RE gate that classified
    # topic-based summaries as TRUE_FOLLOW_UP. Context-dependent summaries
    # are handled by _FOLLOW_UP_RE and the pronoun-word check above.
    return False



def _build_entity_prompt(
    entity_type: str,
    entities: List[str],
    filters: Dict[str, str],
    *,
    enable_prompt_enrichment: bool = True,
) -> str:
    values = ", ".join(entities) if entities else "the provided values"
    filter_text = _format_filters(filters)

    if entity_type == "part_number":
        return (
            f"Find shipments where part_number is one of [{values}]"
            f"{filter_text}. Return shipment-level details with shipment number, "
            f"part number, source, destination, mode, status, ETA, ATA, and delivery dates."
        )

    if entity_type == "shipment_id":
        return (
            f"Find shipments where shipment_number_id is one of [{values}]"
            f"{filter_text}. Return exact shipment-level details."
        )

    column_map = {
        "waybill": "waybill",
        "hbl": "hbl",
        "hawb": "hawb",
        "mbl": "mbl",
        "mawb": "mawb",
        "purchase_order": "purchase_order",
        "sales_order": "sales_order",
        "delivery_document": "delivery_document",
    }
    column = column_map.get(entity_type, entity_type)
    if not enable_prompt_enrichment:
        return f"{entity_type}: {values}"
    return (
        f"Find shipments where {column} is one of [{values}]"
        f"{filter_text}. Return shipment-level details with key shipment attributes."
    )



def _format_filters(filters: Dict[str, str]) -> str:
    if not filters:
        return ""
    parts = []
    if filters.get("destination"):
        parts.append(f" and destination is {filters['destination']}")
    if filters.get("source"):
        parts.append(f" and source is {filters['source']}")
    if filters.get("transport_mode"):
        parts.append(f" and transport mode is {filters['transport_mode']}")
    if filters.get("status"):
        parts.append(f" and status is {filters['status']}")
    return "".join(parts)



def _build_follow_up_prompt(text: str) -> str:
    if _SUMMARY_RE.search(text):
        return _build_summary_prompt()
    return text



def _build_lane_aggregation_prompt(question: str, top_n: str = "10") -> str:
    """Canonical lane aggregation prompt — explicit GROUP BY for Genie SQL generation."""
    return (
        f"Show the top {top_n} shipment lanes by shipment count. "
        f"Group by source_ and destination. "
        f"Return source_, destination, shipment_count. "
        f"Sort by shipment_count descending. "
        f"Do not return individual shipment-level rows."
    )



def _build_broad_prompt(question: str) -> str:
    # P1: avoid double-prefix when question already starts with a summary verb.
    _q_lower = question.lower().strip()
    if re.match(r"^summari[sz]e?\b|^summary\b", _q_lower):
        return (
            f"{question}: total count, top 5 destinations, transport mode breakdown, "
            f"and status distribution."
        )
    return (
        f"Summarise {question}: total count, top 5 destinations, transport mode breakdown, "
        f"and status distribution."
    )



def _build_aggregation_prompt(question: str) -> str:
    """Build the enriched aggregation prompt.

    For canonical prompts (already contain explicit GROUP BY / Sort by descending /
    Do not return shipment-level rows), pass through unchanged to avoid double-wrapping.

    For lane-related questions, use the deterministic lane aggregation template that
    explicitly specifies GROUP BY source_, destination and shipment_count.

    For all other aggregation questions, use the generic analytics framing template.
    """
    # If already canonical, pass through unchanged
    if _CANONICAL_SIGNAL_RE.search(question):
        return question

    # Detect top-N number in the prompt (default 10 if not specified)
    n_match = _TOP_N_RE.search(question)
    top_n = n_match.group(1) if n_match else "10"

    # Lane-specific canonical template
    if _LANE_RE.search(question):
        return _build_lane_aggregation_prompt(question, top_n)

    # Generic analytics framing template (P1 fix: hardened from soft "unless necessary")
    return (
        f"Answer as a business analytics summary for: {question}. "
        f"Return grouped or aggregated results suitable for charting. "
        f"Include key observations. "
        f"Do not return individual shipment-level rows."
    )



def _build_detail_prompt(question: str) -> str:
    return (
        f"Return shipment-level details for: {question}. Include a concise factual summary "
        f"(total count, key attributes) and keep the result table suitable for preview and CSV download."
    )



def _build_summary_prompt() -> str:
    return (
        "Provide a concise business summary of the current result or conversation context. "
        "Include key numbers and observations."
    )



def _build_general_prompt(question: str) -> str:
    return (
        f"Answer clearly and concisely using the shipment dataset. Include business context where useful: {question}"
    )


def _is_low_information(text: str, extracted_entities: List[str]) -> bool:
    """Return True if the prompt carries no logistics/business domain signal.
    Allows through when ANY of: len>40, _DOMAIN_VOCAB_RE matches, extracted_entities present.
    """
    if len(text) > 40:
        return False
    if _DOMAIN_VOCAB_RE.search(text):
        return False
    if extracted_entities:
        return False
    return True
