"""Result shape validator — enterprise accuracy contract (Phase E5/E6/P1).

Validates whether a Genie response shape matches the user intent.

For AGGREGATION intents, if Genie returns raw shipment-level rows instead of
grouped metric rows, this module detects the mismatch so the pipeline can
retry once with a stronger canonical prompt.

E6 extension: is_analytical_shape_mismatch() covers BROAD_LISTING / GENERAL
intents whose original prompt contains analytical vocabulary (analysis, insights,
overview, etc.) but Genie still returned raw shipment rows.  This acts as a
safety net for the rare case where a prompt was not caught by _AGGREGATION_RE.

P1 fix (2026-07-16):
  - "amount" removed from _AGGREGATION_METRIC_WORDS.  It was too broad: the
    token matched raw per-shipment columns like sales_functional_currency_amount
    and sales_budget_rate_amount, causing has_aggregation_metrics() to return
    True for fully raw schemas and suppressing the one-shot retry guardrail.
  - Raw per-shipment measure columns added to _RAW_SHIPMENT_SIGNALS so they
    are always treated as raw-row evidence regardless of metric-word overlap.
  - summarise / summarize / summary added to _ANALYTICAL_VOCAB_RE so that
    summary prompts routed as BROAD_LISTING also trigger mismatch detection.
  - SUMMARY_REQUEST intent added to is_analytical_shape_mismatch() coverage.

Design principles:
  - Pure-Python, no I/O, no LLM calls.  Fully deterministic.
  - Conservative: only detects clear-cut mismatches (raw_signals present AND
    no aggregation_metrics present).  False negatives (missed mismatch) are
    far safer than false positives (incorrect retry on a valid result).
  - Retry is one-shot only.  No loops, no broad multi-query orchestration.
"""

from __future__ import annotations

import re
from typing import List, Optional

# =============================================================================
# Column-name signal sets
# =============================================================================

# Columns that indicate raw shipment-level rows.
# P1 fix: added per-shipment measure columns that were causing false positives
# in has_aggregation_metrics() due to the "amount" token.
_RAW_SHIPMENT_SIGNALS: frozenset = frozenset({
    "shipment_number_id",
    "part_number",
    "actual_pgi_date",
    "eta",
    "ata",
    "final_gr_date",
    "waybill",
    "delivery_document_id",
    "hbl",
    "hawb",
    "mbl",
    "mawb",
    "customer_purchase_order_id",
    "sales_order_number",
    "carrier_name",
    "shipper_name",
    "consignee_name",
    "actual_departure_at",
    "po_line_number",
    # P1 fix: raw per-shipment measure columns (not aggregation outputs)
    "sales_functional_currency_amount",
    "sales_budget_rate_amount",
    "shipment_quantity",
    "chargeable_weight",
    "actual_weight",
})

# Single-token metric keywords that indicate aggregated/grouped data.
# Used by has_aggregation_metrics() for token-based compound-column detection.
#
# P1 fix: "amount" REMOVED — it is too broad.  Raw per-shipment columns like
# sales_functional_currency_amount and sales_budget_rate_amount contain the
# token "amount", causing has_aggregation_metrics() to return True for fully
# raw schemas and silencing the one-shot retry.  Legitimate aggregation columns
# containing "total" (total_revenue, total_sales_amount) still match via
# the "total" token which remains in this set.
_AGGREGATION_METRIC_WORDS: frozenset = frozenset({
    "count",
    "total",
    "sum",
    "avg",
    "average",
    "revenue",
    "volume",
    # "amount" intentionally removed — P1 fix (too broad; matches
    # per-shipment columns like sales_functional_currency_amount).
    # Tokens like rate/delay/pct also excluded for same reason.
    "metric",
    "num",
})

# Known exact compound column names that are always aggregation metrics.
_AGGREGATION_EXACT_COLS: frozenset = frozenset({
    "shipment_count",
    "n_shipments",
    "total_shipments",
    "num_shipments",
})

# Kept for backward-compatibility / external imports; not used internally.
_AGGREGATION_METRIC_RE = re.compile(
    r"\b(count|total|sum|avg|average|revenue|shipment_count|volume|"
    r"n_shipments|total_shipments|num_shipments|metric|num_|_count|_total|_sum)\b",
    re.IGNORECASE,
)

# E6: Analytical vocabulary that signals the user expects aggregated output.
# Mirrors the E6 additions to _AGGREGATION_RE in pre_genie_router.py and
# genie_prompt_enricher.py.
# P1 fix: added summarise / summarize / summary so that summary-intent prompts
# routed as BROAD_LISTING or GENERAL also trigger mismatch detection.
_ANALYTICAL_VOCAB_RE = re.compile(
    r"\b(analysis|analyze|analyse|insights?|overview|report\s+on|examine|assessment|evaluation|how\s+delayed"
    r"|summari[sz]e|summary)\b",
    re.IGNORECASE,
)

# E6: Delay-analysis sub-pattern — used by build_retry_prompt to build a
# delay-specific canonical retry prompt when the original query was about delays.
_DELAY_ANALYSIS_RE = re.compile(
    r"\b(delay|delayed|delays)\b.*\b(analysis|analyze|analyse|overview|how\s+delayed)\b"
    r"|\b(analysis|analyze|analyse|overview)\b.*\b(delay|delayed|delays)\b"
    r"|\bhow\s+delayed\b",
    re.IGNORECASE | re.DOTALL,
)

# P1 fix: summary sub-pattern — used by build_retry_prompt to build a
# summary-specific canonical retry prompt when the original query was about
# summarising a subset of shipments.
_SUMMARY_INTENT_RE = re.compile(
    r"\bsummari[sz]e\b|\bsummary\s+of\b|\bgive\s+(?:me\s+)?a?\s*summary\b",
    re.IGNORECASE,
)

# =============================================================================
# Detection helpers
# =============================================================================


def _normalize(h: str) -> str:
    return h.strip().lower()


def has_raw_shipment_signals(headers: List[str]) -> bool:
    """True if any header matches a known raw shipment-level column name."""
    normalized = {_normalize(h) for h in headers}
    return bool(normalized & _RAW_SHIPMENT_SIGNALS)


def has_aggregation_metrics(headers: List[str]) -> bool:
    """True if any header looks like an aggregation metric column.

    Detects metrics by two methods:
    1. Exact match against known compound column names (shipment_count, etc.).
    2. Token matching: split column name on ``_`` and check whether any token
       is a known metric keyword (count, total, avg, revenue, etc.).

    Token matching handles compound column names like ``total_revenue``,
    ``avg_weight``, ``revenue_by_bu``, and ``record_count`` that the original
    regex-with-word-boundaries failed to detect.

    P1 fix: "amount" removed from _AGGREGATION_METRIC_WORDS to prevent false
    positives from raw columns like sales_functional_currency_amount.  Those
    columns now appear in _RAW_SHIPMENT_SIGNALS and are classified correctly.
    Columns like total_sales_amount still match via the "total" token.
    """
    for h in headers:
        h_norm = _normalize(h)
        # Exact compound match (e.g. "shipment_count", "n_shipments")
        if h_norm in _AGGREGATION_EXACT_COLS:
            return True
        # Token-based match — handles "total_revenue", "avg_weight", etc.
        tokens = [t for t in h_norm.split("_") if t]
        if any(t in _AGGREGATION_METRIC_WORDS for t in tokens):
            return True
    return False


def has_analytical_vocabulary(prompt: str) -> bool:
    """True if the prompt contains analytical-intent vocabulary (E6 + P1).

    Used by is_analytical_shape_mismatch() as a safety-net trigger for
    BROAD_LISTING / GENERAL / SUMMARY_REQUEST intents that were not caught
    by _AGGREGATION_RE.  P1 fix adds summarise/summarize/summary.
    """
    return bool(_ANALYTICAL_VOCAB_RE.search(prompt or ""))


def is_aggregation_shape_mismatch(intent: str, headers: List[str]) -> bool:
    """Return True when an AGGREGATION intent produced raw shipment rows.

    Mismatch condition:
      - Intent is AGGREGATION
      - Result contains raw shipment-level column(s)
      - Result has NO aggregation metric columns

    Only triggers when BOTH conditions hold to avoid false positives on
    mixed-schema results.
    """
    if intent != "AGGREGATION":
        return False
    if not headers:
        return False
    return has_raw_shipment_signals(headers) and not has_aggregation_metrics(headers)


def is_analytical_shape_mismatch(
    intent: str,
    headers: List[str],
    original_prompt: str = "",
) -> bool:
    """Return True when a user analytical request produced raw shipment rows (E6/P1).

    Extends is_aggregation_shape_mismatch to cover safety-net cases:
    - Intent is AGGREGATION → delegates to is_aggregation_shape_mismatch
    - Intent is BROAD_LISTING, GENERAL, or SUMMARY_REQUEST AND the original
      prompt contains analytical vocabulary (analysis, insights, overview,
      summarise, …) → checks raw signals vs aggregation metrics as usual

    P1 fix: SUMMARY_REQUEST added to the covered intent set.  Previously,
    summary-intent prompts were excluded from shape validation even when they
    returned raw rows, because SUMMARY_REQUEST was not in the intent guard.

    This catches edge-cases where a prompt was not classified as AGGREGATION by
    _AGGREGATION_RE (e.g. novel phrasing) but the user clearly wanted analytics.
    """
    if not headers:
        return False
    if intent == "AGGREGATION":
        return is_aggregation_shape_mismatch(intent, headers)
    if intent in ("BROAD_LISTING", "GENERAL", "SUMMARY_REQUEST") and has_analytical_vocabulary(original_prompt):
        return has_raw_shipment_signals(headers) and not has_aggregation_metrics(headers)
    return False


# =============================================================================
# Canonical retry prompt builders
# =============================================================================

_LANE_RE = re.compile(r"\blanes?\b", re.IGNORECASE)
_TOP_N_RE = re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE)
_STATUS_DIST_RE = re.compile(
    r"\bstatus\s+distribution\b|\bshipment\s+status\b|\bby\s+status\b",
    re.IGNORECASE,
)
_REVENUE_RE = re.compile(
    r"\brevenue\b|\bby\s+bu\b|\bby\s+business\s+unit\b|\bbu\s+breakdown\b",
    re.IGNORECASE,
)
_MODE_RE = re.compile(
    r"\bmode\s+breakdown\b|\bby\s+mode\b|\btransport\s+mode\b|\bmode\s+distribution\b",
    re.IGNORECASE,
)
_DESTINATION_RE = re.compile(
    r"\btop\s+destinations?\b|\bby\s+destination\b|\bdestination\s+breakdown\b",
    re.IGNORECASE,
)


def build_retry_prompt(original_prompt: str, intent: str) -> str:
    """Build a canonical one-shot retry prompt for a shape-mismatched result.

    Selects the most specific template based on detected sub-intent.
    Falls back to a generic aggregation framing for unrecognised sub-intents.

    E6: Added delay-analysis template to handle analytical delay queries that
    returned raw rows on the first attempt.
    P1: Added summary-intent template for summarise/summarize queries.
    """
    n_match = _TOP_N_RE.search(original_prompt)
    top_n = n_match.group(1) if n_match else "10"

    if _LANE_RE.search(original_prompt):
        return (
            f"Show the top {top_n} shipment lanes by shipment count. "
            f"Group by source_ and destination. "
            f"Return source_, destination, shipment_count. "
            f"Sort by shipment_count descending. "
            f"Do not return individual shipment-level rows."
        )
    if _STATUS_DIST_RE.search(original_prompt):
        return (
            "Show the shipment status distribution. "
            "Group by execution_status. "
            "Return execution_status, shipment_count. "
            "Sort by shipment_count descending. "
            "Do not return individual shipment-level rows."
        )
    if _REVENUE_RE.search(original_prompt):
        return (
            "Show total revenue by business unit. "
            "Group by business_unit_id. "
            "Return business_unit_id, total_revenue. "
            "Sort by total_revenue descending. "
            "Do not return individual shipment-level rows."
        )
    if _MODE_RE.search(original_prompt):
        return (
            "Show shipment breakdown by transport mode. "
            "Group by transportation_mode_desc. "
            "Return transportation_mode_desc, shipment_count. "
            "Sort by shipment_count descending. "
            "Do not return individual shipment-level rows."
        )
    if _DESTINATION_RE.search(original_prompt):
        return (
            f"Show the top {top_n} destinations by shipment count. "
            f"Group by destination. "
            f"Return destination, shipment_count. "
            f"Sort by shipment_count descending. "
            f"Do not return individual shipment-level rows."
        )
    # E6: Delay analysis canonical retry template
    if _DELAY_ANALYSIS_RE.search(original_prompt):
        return (
            f"Provide a delay analysis for: {original_prompt}. "
            "Show: total shipment count, count of delayed shipments, "
            "delay percentage, and average delay in days where available. "
            "Group by execution_status or relevant dimension. "
            "Do not return individual shipment-level rows."
        )
    # P1: Summary intent canonical retry template
    if _SUMMARY_INTENT_RE.search(original_prompt):
        return (
            f"Provide a summary for: {original_prompt}. "
            "Show: total shipment count, top 5 destinations by count, "
            "transport mode breakdown, execution status distribution, "
            "and business unit breakdown. "
            "Return grouped/aggregated rows. "
            "Do not return individual shipment-level rows."
        )
    # Generic aggregation canonical fallback
    return (
        f"Answer as a business analytics aggregation for: {original_prompt}. "
        f"Return a grouped/aggregated table with dimension columns and a metric column "
        f"(count, sum, or average). "
        f"Sort by the primary metric column descending. "
        f"Do not return individual shipment-level rows."
    )
