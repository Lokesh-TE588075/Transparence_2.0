"""End-to-end diagnostic trace recorder for TransparencE pipeline.

Provides structured, request-scoped tracing so that every pipeline turn can be
reconstructed as a complete execution path:

  User prompt → session lookup → routing → enrichment → Genie API call
  → Genie SQL/attachment → query result → shape validation → retry
  → fallback → summarizer → chart → final response

SECURITY CONTROLS
  - No raw row values are ever stored.
  - User emails, session IDs, and conversation IDs are SHA-256 hashed.
  - Authentication tokens are never recorded.
  - Full SQL is only stored when TRANSPARENCE_DIAGNOSTIC_LOG_SQL=true (off by default).
  - Prompt excerpts are truncated to TRANSPARENCE_DIAGNOSTIC_PROMPT_EXCERPT_LENGTH chars.

PERFORMANCE CONTRACT
  - When TRANSPARENCE_DIAGNOSTIC_TRACING_ENABLED=false, all operations are no-ops.
  - Delta writes happen on daemon background threads (fire-and-forget).
  - A write failure logs a WARNING but NEVER raises or alters the user response.
  - Zero additional latency on the hot path when tracing is disabled.

PROCESS IDENTITY
  application_startup_id is a UUID4 generated once at module import time.
  Because Databricks Apps restarts the Python process after container suspension,
  a change in application_startup_id between requests proves a container restart
  occurred during the idle interval.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

# =============================================================================
# PROCESS-LEVEL IDENTITY — stable within a process, changes on container restart
# =============================================================================

#: UUID4 generated once at module import. Proves process identity across turns.
application_startup_id: str = str(uuid.uuid4())

_PROCESS_ID: int = os.getpid()


# =============================================================================
# CONFIGURATION ACCESSORS
# =============================================================================

def _tracing_enabled() -> bool:
    try:
        from app.config import settings
        return bool(settings.TRANSPARENCE_DIAGNOSTIC_TRACING_ENABLED)
    except Exception:
        return False


def _log_sql_enabled() -> bool:
    try:
        from app.config import settings
        return bool(settings.TRANSPARENCE_DIAGNOSTIC_LOG_SQL)
    except Exception:
        return False


def _prompt_excerpt_max() -> int:
    try:
        from app.config import settings
        return int(settings.TRANSPARENCE_DIAGNOSTIC_PROMPT_EXCERPT_LENGTH)
    except Exception:
        return 200


def _get_deployment_id() -> str:
    try:
        from app.config import settings
        return getattr(settings, "TRANSPARENCE_DEPLOYMENT_ID", "") or os.getenv("TRANSPARENCE_DEPLOYMENT_ID", "")
    except Exception:
        return os.getenv("TRANSPARENCE_DEPLOYMENT_ID", "")


# =============================================================================
# HASHING / REDACTION UTILITIES
# =============================================================================

def _hash(value: Optional[str], length: int = 12) -> Optional[str]:
    """SHA-256 hash of a string, truncated to `length` hex chars.

    Returns None when value is None or empty.
    Used to record session/conversation identities without storing raw values.
    """
    if not value:
        return None
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
    return digest[:length]


def _excerpt(text: Optional[str], max_chars: Optional[int] = None) -> Optional[str]:
    """Return a safe, truncated excerpt of prompt text."""
    if not text:
        return None
    limit = max_chars if max_chars is not None else _prompt_excerpt_max()
    return text[:limit] + ("…" if len(text) > limit else "")


def _phash(text: Optional[str]) -> Optional[str]:
    """16-char hash of prompt text for exact-match identification."""
    return _hash(text, 16)


# =============================================================================
# PROMPT STRUCTURE FLAGS
# =============================================================================

_RE_GROUP_BY      = re.compile(r"\bGROUP\s+BY\b",                      re.IGNORECASE)
_RE_AGG_INSTR     = re.compile(r"\b(COUNT|SUM|AVG|total|shipment_count|total_revenue|total_sales_amount)\b", re.IGNORECASE)
_RE_DATE_BUCKET   = re.compile(r"\b(MONTH|MONTHLY|QUARTERLY|YEARLY|DATE_TRUNC|DATE_FORMAT|TRUNC|year|quarter|by\s+month|by\s+year)\b", re.IGNORECASE)
_RE_NO_RAW_ROWS   = re.compile(r"do not return individual.*rows",       re.IGNORECASE)
_RE_PRIOR_CONTEXT = re.compile(r"\b(current result|conversation context|previous|prior|above|this result|the above)\b", re.IGNORECASE)


def analyze_prompt_structure(text: Optional[str]) -> Dict[str, bool]:
    """Return structural boolean flags for an enriched prompt.

    These flags are used in traces to understand what instructions the
    enriched prompt carried to Genie.  No prompt text is stored here.
    """
    if not text:
        return {
            "has_group_by_instruction":     False,
            "has_aggregate_instruction":    False,
            "has_date_bucket_instruction":  False,
            "has_no_raw_rows_instruction":  False,
            "references_prior_context":     False,
        }
    return {
        "has_group_by_instruction":     bool(_RE_GROUP_BY.search(text)),
        "has_aggregate_instruction":    bool(_RE_AGG_INSTR.search(text)),
        "has_date_bucket_instruction":  bool(_RE_DATE_BUCKET.search(text)),
        "has_no_raw_rows_instruction":  bool(_RE_NO_RAW_ROWS.search(text)),
        "references_prior_context":     bool(_RE_PRIOR_CONTEXT.search(text)),
    }


# =============================================================================
# EXPECTED CONTRACT CLASSIFIER  (diagnostic only — does NOT alter responses)
# =============================================================================

_RE_TIME_SERIES = re.compile(r"\b(monthly|quarterly|yearly|weekly|daily|by\s+month|by\s+quarter|by\s+year|over\s+time|trend|time.series)\b", re.IGNORECASE)
_RE_AGGREGATION = re.compile(r"\b(top\s+\w+|total|revenue|by\s+\w+|count\s+of|how\s+many|breakdown|distribution|analysis|analyse|analyze|group|rank|summary\s+of)\b", re.IGNORECASE)
_RE_LISTING     = re.compile(r"^(show\s+me|list|get|find|fetch|display)\b", re.IGNORECASE)
_RE_SUMMARY     = re.compile(r"\b(summar[iy]z?e?|give\s+a\s+summary|summarise|overview|insight)\b", re.IGNORECASE)


def classify_expected_contract(
    user_message: str,
    router_intent: Optional[str],
) -> Dict[str, Any]:
    """Classify the expected output contract for diagnostic comparison.

    This classifier is PURELY for tracing.  It does NOT alter the pipeline
    response in any way.
    """
    try:
        if _RE_TIME_SERIES.search(user_message):
            return {"expected_contract": "TIME_SERIES",   "allow_raw_identifiers": False, "requires_time_bucket": True,  "requires_aggregate_metric": True}
        if _RE_SUMMARY.search(user_message):
            return {"expected_contract": "SUMMARY",       "allow_raw_identifiers": False, "requires_time_bucket": False, "requires_aggregate_metric": True}
        if router_intent == "AGGREGATION" or _RE_AGGREGATION.search(user_message):
            return {"expected_contract": "AGGREGATION",   "allow_raw_identifiers": False, "requires_time_bucket": False, "requires_aggregate_metric": True}
        if router_intent == "BROAD_LISTING":
            return {"expected_contract": "LISTING",       "allow_raw_identifiers": True,  "requires_time_bucket": False, "requires_aggregate_metric": False}
    except Exception:
        pass
    return {"expected_contract": "UNKNOWN", "allow_raw_identifiers": True, "requires_time_bucket": False, "requires_aggregate_metric": False}


# =============================================================================
# RESULT GRAIN ANALYSIS
# =============================================================================

_RAW_ID_COLS    = frozenset({"shipment_number_id", "part_number", "actual_pgi_date", "eta", "ata", "final_gr_date", "currency_code"})
_AGG_COL_RE     = re.compile(r"\b(count|total|sum|avg|revenue|amount|shipment_count|total_sales_amount)\b", re.IGNORECASE)
_TIME_COL_RE    = re.compile(r"\b(month|year|quarter|date|week|day)\b", re.IGNORECASE)
_GROUP_DIM_COLS = frozenset({"source_", "destination", "transportation_mode_desc", "business_unit_id", "execution_status"})


def analyze_result_grain(headers: Optional[List[str]]) -> Dict[str, Any]:
    """Classify the grain of a result set from its header names alone."""
    if not headers:
        return {"result_grain": "EMPTY", "has_raw_identifiers": False,
                "has_aggregate_metric": False, "has_time_bucket_dimension": False,
                "has_grouping_dimension": False, "header_count": 0, "headers": []}
    try:
        h_lower = [h.lower() for h in headers]
        h_set   = set(h_lower)
        has_raw = bool(h_set & {c.lower() for c in _RAW_ID_COLS})
        has_agg = bool(any(_AGG_COL_RE.search(h) for h in h_lower))
        has_time = bool(any(_TIME_COL_RE.search(h) for h in h_lower))
        has_dim  = bool(h_set & {c.lower() for c in _GROUP_DIM_COLS})

        if has_raw:
            grain = "RAW_SHIPMENT"
        elif has_time and has_agg:
            grain = "TIME_SERIES_AGGREGATE"
        elif has_agg and has_dim:
            grain = "GROUPED_AGGREGATE"
        elif has_agg:
            grain = "AGGREGATE"
        elif has_dim:
            grain = "GROUPED_DIMENSION"
        else:
            grain = "UNKNOWN"

        return {
            "result_grain": grain,
            "has_raw_identifiers": has_raw,
            "has_aggregate_metric": has_agg,
            "has_time_bucket_dimension": has_time,
            "has_grouping_dimension": has_dim,
            "header_count": len(headers),
            "headers": list(headers),
        }
    except Exception:
        return {"result_grain": "ERROR", "has_raw_identifiers": False,
                "has_aggregate_metric": False, "has_time_bucket_dimension": False,
                "has_grouping_dimension": False, "header_count": 0, "headers": []}


def validate_result_contract(
    headers: Optional[List[str]],
    expected_contract: str,
    allow_raw_identifiers: bool,
    requires_aggregate_metric: bool,
    requires_time_bucket: bool,
) -> Dict[str, Any]:
    """Validate headers against the expected contract.  Diagnostic only."""
    try:
        grain = analyze_result_grain(headers)
        violations: List[str] = []
        if grain["has_raw_identifiers"] and not allow_raw_identifiers:
            violations.append("raw_identifiers_present")
        if requires_aggregate_metric and not grain["has_aggregate_metric"]:
            violations.append("missing_aggregate_metric")
        if requires_time_bucket and not grain["has_time_bucket_dimension"]:
            violations.append("missing_time_bucket")
        return {
            "contract_valid":      len(violations) == 0,
            "contract_violations": violations,
            "result_grain":        grain["result_grain"],
        }
    except Exception:
        return {"contract_valid": False, "contract_violations": ["analysis_error"], "result_grain": "ERROR"}


# =============================================================================
# DIAGNOSTIC TRACE  —  main data accumulator
# =============================================================================

class DiagnosticTrace:
    """Accumulates diagnostic information for a single pipeline request.

    All ``record_*`` methods are completely safe:
      - They are no-ops when tracing is disabled.
      - They swallow all exceptions internally.
      - They never raise to the caller.
      - They never modify or inspect the pipeline's business data.
    """

    def __init__(self, enabled: bool, parent_trace_id: Optional[str] = None):
        self._enabled = enabled
        if not enabled:
            self.trace_id: Optional[str] = None
            self._record: Dict[str, Any] = {}
            return

        self.trace_id = str(uuid.uuid4())
        self._ts_start = time.time()
        self._record = {
            "trace_id":               self.trace_id,
            "parent_trace_id":        parent_trace_id,
            "application_startup_id": application_startup_id,
            "process_id":             _PROCESS_ID,
            "deployment_id":          _get_deployment_id(),
            "request_timestamp_utc":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    # ── internal helpers ──────────────────────────────────────────────────────

    def _safe(self, fn: Any, *args: Any, **kwargs: Any) -> None:
        """Execute fn silently.  A no-op when tracing is disabled."""
        if not self._enabled:
            return
        try:
            fn(*args, **kwargs)
        except Exception:
            pass

    def _merge(self, d: Dict[str, Any]) -> None:
        self._record.update(d)

    # ── Stage 1: REQUEST_RECEIVED ─────────────────────────────────────────────

    def record_request(
        self, *, user_id_raw: Optional[str] = None, session_id_raw: Optional[str] = None,
        frontend_conv_id_raw: Optional[str] = None, server_conv_key_raw: Optional[str] = None,
        original_prompt: Optional[str] = None,
    ) -> None:
        def _do() -> None:
            self._merge({
                "stage_request":               True,
                "user_id_hash":                _hash(user_id_raw),
                "session_id_hash":             _hash(session_id_raw),
                "frontend_conv_id_hash":       _hash(frontend_conv_id_raw),
                "server_conv_key_hash":        _hash(server_conv_key_raw),
                "original_prompt_excerpt":     _excerpt(original_prompt),
                "original_prompt_hash":        _phash(original_prompt),
                "original_prompt_length":      len(original_prompt) if original_prompt else 0,
            })
        self._safe(_do)

    # ── Stage 2: SESSION_LOOKUP ───────────────────────────────────────────────

    def record_session_lookup(
        self, *, session_found: bool = False, genie_session_found: bool = False,
        genie_conv_id_raw: Optional[str] = None, previous_intent: Optional[str] = None,
        previous_entities_count: int = 0, previous_filters_count: int = 0,
        session_state: Optional[str] = None, session_age_seconds: Optional[float] = None,
        frontend_conversation_present: bool = False, backend_session_present: bool = False,
        session_state_mismatch: bool = False,
    ) -> None:
        def _do() -> None:
            self._merge({
                "stage_session_lookup":           True,
                "session_found":                  session_found,
                "genie_session_found":            genie_session_found,
                "previous_genie_conv_id_hash":    _hash(genie_conv_id_raw),
                "previous_intent":                previous_intent,
                "previous_entities_count":        previous_entities_count,
                "previous_filters_count":         previous_filters_count,
                "session_state":                  session_state,
                "session_age_seconds":            session_age_seconds,
                "frontend_conversation_present":  frontend_conversation_present,
                "backend_session_present":        backend_session_present,
                "session_state_mismatch":         session_state_mismatch,
            })
        self._safe(_do)

    # ── Stage 3: ROUTING ──────────────────────────────────────────────────────

    def record_routing(
        self, *, normalized_prompt: Optional[str] = None, canonical_prompt: Optional[str] = None,
        route_intent: Optional[str] = None, route_reason: Optional[str] = None,
        is_follow_up: bool = False, should_call_genie: bool = True,
        reuse_genie_context: bool = False, entities_count: int = 0, filters_count: int = 0,
        expected_contract_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        def _do() -> None:
            d: Dict[str, Any] = {
                "stage_routing":             True,
                "normalized_prompt_excerpt": _excerpt(normalized_prompt, 100),
                "canonical_prompt_hash":     _phash(canonical_prompt),
                "route_intent":              route_intent,
                "route_reason":              route_reason,
                "is_follow_up":              is_follow_up,
                "should_call_genie":         should_call_genie,
                "reuse_genie_context":       reuse_genie_context,
                "entities_count":            entities_count,
                "filters_count":             filters_count,
            }
            if expected_contract_info:
                d.update(expected_contract_info)
            self._merge(d)
        self._safe(_do)

    # ── Stage 4: PROMPT_ENRICHMENT ────────────────────────────────────────────

    def record_enrichment(
        self, *, prompt_builder_name: Optional[str] = None, enriched_prompt: Optional[str] = None,
        enrichment_applied: bool = False, enricher_intent: Optional[str] = None,
    ) -> None:
        def _do() -> None:
            structure = analyze_prompt_structure(enriched_prompt)
            self._merge({
                "stage_enrichment":           True,
                "prompt_builder_name":        prompt_builder_name,
                "enriched_prompt_excerpt":    _excerpt(enriched_prompt),
                "enriched_prompt_hash":       _phash(enriched_prompt),
                "enriched_prompt_length":     len(enriched_prompt) if enriched_prompt else 0,
                "enrichment_applied":         enrichment_applied,
                "enricher_intent":            enricher_intent,
                **{f"prompt_{k}": v for k, v in structure.items()},
            })
        self._safe(_do)

    # ── Stage 5: GENIE_REQUEST ────────────────────────────────────────────────

    def record_genie_request(
        self, *, call_type: Optional[str] = None, genie_conv_id_raw: Optional[str] = None,
        genie_message_id_raw: Optional[str] = None, request_timestamp_utc: Optional[str] = None,
        elapsed_seconds: Optional[float] = None, polling_attempts: int = 0,
        genie_status: Optional[str] = None, error: Optional[str] = None,
    ) -> None:
        def _do() -> None:
            self._merge({
                "stage_genie_request":      True,
                "genie_call_type":          call_type,
                "genie_conv_id_hash":       _hash(genie_conv_id_raw),
                "genie_message_id_hash":    _hash(genie_message_id_raw),
                "genie_request_timestamp":  request_timestamp_utc,
                "genie_elapsed_seconds":    elapsed_seconds,
                "genie_polling_attempts":   polling_attempts,
                "genie_final_status":       genie_status,
                "genie_error":              error,
            })
        self._safe(_do)

    # ── Stage 6: GENIE_RESPONSE ───────────────────────────────────────────────

    def record_genie_response(
        self, *, attachment_types: Optional[List[str]] = None,
        text_attachment_count: int = 0, query_attachment_count: int = 0,
        viz_attachment_count: int = 0,
        selected_query_attachment_id_raw: Optional[str] = None,
        selected_text_attachment_id_raw: Optional[str] = None,
        multiple_query_attachments: bool = False,
        attachment_selection_reason: Optional[str] = None,
        generated_sql_hash: Optional[str] = None,
        generated_sql_structure: Optional[Dict[str, Any]] = None,
        full_sql: Optional[str] = None,
    ) -> None:
        def _do() -> None:
            d: Dict[str, Any] = {
                "stage_genie_response":              True,
                "genie_attachment_types":            attachment_types or [],
                "genie_text_attachment_count":       text_attachment_count,
                "genie_query_attachment_count":      query_attachment_count,
                "genie_viz_attachment_count":        viz_attachment_count,
                "genie_selected_query_id_hash":      _hash(selected_query_attachment_id_raw),
                "genie_selected_text_id_hash":       _hash(selected_text_attachment_id_raw),
                "genie_multiple_query_attachments":  multiple_query_attachments,
                "genie_attachment_selection_reason": attachment_selection_reason,
                "genie_generated_sql_hash":          generated_sql_hash,
            }
            if generated_sql_structure:
                d.update({f"sql_{k}": v for k, v in generated_sql_structure.items()})
            if _log_sql_enabled() and full_sql:
                d["generated_sql_full"] = full_sql
            self._merge(d)
        self._safe(_do)

    # ── Stage 7: FIRST_RESULT ─────────────────────────────────────────────────

    def record_first_result(
        self, *, headers: Optional[List[str]] = None, row_count: int = 0,
        expected_contract: Optional[str] = None, allow_raw_identifiers: bool = True,
        requires_aggregate: bool = False, requires_time_bucket: bool = False,
    ) -> None:
        def _do() -> None:
            grain  = analyze_result_grain(headers or [])
            c_info = validate_result_contract(headers or [], expected_contract or "UNKNOWN",
                                              allow_raw_identifiers, requires_aggregate, requires_time_bucket)
            self._merge({
                "stage_first_result":      True,
                "first_result_row_count":  row_count,
                "first_expected_contract": expected_contract,
                **{f"first_result_{k}": v for k, v in grain.items()},
                **{f"first_contract_{k}": v for k, v in c_info.items()},
            })
        self._safe(_do)

    # ── Stage 8: RETRY ────────────────────────────────────────────────────────

    def record_retry(
        self, *, retry_required: bool, retry_reason: Optional[str] = None,
        retry_prompt_builder: Optional[str] = None, retry_prompt: Optional[str] = None,
        retry_fresh_conversation: bool = False,
        retry_genie_conv_id_raw: Optional[str] = None,
        retry_genie_msg_id_raw: Optional[str] = None,
        retry_generated_sql_structure: Optional[Dict[str, Any]] = None,
        retry_headers: Optional[List[str]] = None,
        retry_row_count: int = 0, retry_contract_valid: Optional[bool] = None,
        retry_elapsed_seconds: Optional[float] = None,
    ) -> None:
        def _do() -> None:
            d: Dict[str, Any] = {
                "stage_retry":             True,
                "retry_required":          retry_required,
                "retry_reason":            retry_reason,
                "retry_prompt_builder":    retry_prompt_builder,
                "retry_prompt_excerpt":    _excerpt(retry_prompt),
                "retry_prompt_hash":       _phash(retry_prompt),
                "retry_fresh_conversation": retry_fresh_conversation,
                "retry_genie_conv_id_hash": _hash(retry_genie_conv_id_raw),
                "retry_genie_msg_id_hash":  _hash(retry_genie_msg_id_raw),
                "retry_row_count":         retry_row_count,
                "retry_contract_valid":    retry_contract_valid,
                "retry_elapsed_seconds":   retry_elapsed_seconds,
            }
            if retry_generated_sql_structure:
                d.update({f"retry_sql_{k}": v for k, v in retry_generated_sql_structure.items()})
            if retry_headers is not None:
                grain = analyze_result_grain(retry_headers)
                d.update({f"retry_result_{k}": v for k, v in grain.items()})
            self._merge(d)
        self._safe(_do)

    # ── Stage 9: FALLBACK ─────────────────────────────────────────────────────

    def record_fallback(
        self, *, fallback_attempted: bool, fallback_source: Optional[str] = None,
        fallback_reason: Optional[str] = None, fallback_headers: Optional[List[str]] = None,
        fallback_contract_valid: Optional[bool] = None, fallback_accepted: bool = False,
    ) -> None:
        def _do() -> None:
            d: Dict[str, Any] = {
                "stage_fallback":          True,
                "fallback_attempted":      fallback_attempted,
                "fallback_source":         fallback_source,
                "fallback_reason":         fallback_reason,
                "fallback_accepted":       fallback_accepted,
                "fallback_contract_valid": fallback_contract_valid,
            }
            if fallback_headers is not None:
                grain = analyze_result_grain(fallback_headers)
                d.update({f"fallback_result_{k}": v for k, v in grain.items()})
            self._merge(d)
        self._safe(_do)

    # ── Stage 10: SUMMARIZER ──────────────────────────────────────────────────

    def record_summarizer(
        self, *, executed: bool, input_row_count: int = 0,
        input_headers: Optional[List[str]] = None,
        computed_metrics_keys: Optional[List[str]] = None,
        summary_text_generated: bool = False, summary_text_length: int = 0,
        operates_on_raw_rows: bool = False,
        summary_represents_requested_measure: bool = True,
    ) -> None:
        def _do() -> None:
            grain = analyze_result_grain(input_headers or []) if input_headers else {}
            self._merge({
                "stage_summarizer":                         True,
                "summarizer_executed":                      executed,
                "summarizer_input_row_count":               input_row_count,
                "summarizer_input_grain":                   grain.get("result_grain"),
                "summarizer_computed_metrics_keys":         computed_metrics_keys or [],
                "summarizer_summary_text_generated":        summary_text_generated,
                "summarizer_summary_text_length":           summary_text_length,
                "summarizer_operates_on_raw_rows":          operates_on_raw_rows,
                "summarizer_represents_requested_measure":  summary_represents_requested_measure,
            })
        self._safe(_do)

    # ── Stage 11: CHART ───────────────────────────────────────────────────────

    def record_chart(
        self, *, genie_viz_present: bool = False, computed_chart_data_present: bool = False,
        chart_x_key: Optional[str] = None, chart_y_key: Optional[str] = None,
        chart_data_row_count: int = 0, chart_suppressed_reason: Optional[str] = None,
        chart_fields_match_request: bool = True, chart_type_inferred: Optional[str] = None,
    ) -> None:
        def _do() -> None:
            self._merge({
                "stage_chart":                  True,
                "chart_genie_viz_present":      genie_viz_present,
                "chart_computed_data_present":  computed_chart_data_present,
                "chart_x_key":                  chart_x_key,
                "chart_y_key":                  chart_y_key,
                "chart_data_row_count":         chart_data_row_count,
                "chart_suppressed_reason":      chart_suppressed_reason,
                "chart_fields_match_request":   chart_fields_match_request,
                "chart_type_inferred":          chart_type_inferred,
            })
        self._safe(_do)

    # ── Stage 12: FINAL_RESPONSE ──────────────────────────────────────────────

    def record_final_response(
        self, *, final_source: Optional[str] = None, final_headers: Optional[List[str]] = None,
        final_row_count: int = 0, final_contract_type: Optional[str] = None,
        final_contract_valid: bool = False, chart_included: bool = False,
        summary_included: bool = False, raw_table_included: bool = False,
        response_elapsed_ms: int = 0, controlled_error_returned: bool = False,
    ) -> None:
        def _do() -> None:
            grain = analyze_result_grain(final_headers or [])
            self._merge({
                "stage_final_response":       True,
                "final_source":               final_source,
                "final_row_count":            final_row_count,
                "final_contract_type":        final_contract_type,
                "final_contract_valid":       final_contract_valid,
                "final_chart_included":       chart_included,
                "final_summary_included":     summary_included,
                "final_raw_table_included":   raw_table_included,
                "final_response_elapsed_ms":  response_elapsed_ms,
                "final_controlled_error":     controlled_error_returned,
                **{f"final_result_{k}": v for k, v in grain.items()},
            })
        self._safe(_do)

    # ── Finalization ──────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Return a complete snapshot of the trace record."""
        if not self._enabled:
            return {}
        try:
            self._record["total_elapsed_seconds"] = round(time.time() - self._ts_start, 3)
            return dict(self._record)
        except Exception:
            return {}

    def submit(self) -> None:
        """Asynchronously write this trace to the configured store.

        Fire-and-forget.  Failures are logged but never raised.
        """
        if not self._enabled:
            return
        try:
            from app.services.diagnostic_trace_store import write_trace_async
            write_trace_async(self.to_dict())
        except Exception:
            pass


# =============================================================================
# FACTORY
# =============================================================================

def new_trace(parent_trace_id: Optional[str] = None) -> DiagnosticTrace:
    """Create a new DiagnosticTrace, enabled only when tracing is configured."""
    return DiagnosticTrace(enabled=_tracing_enabled(), parent_trace_id=parent_trace_id)
