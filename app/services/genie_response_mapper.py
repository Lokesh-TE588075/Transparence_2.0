"""Genie response mapper.

Converts a ``GenieMessage`` (plus optional ``GenieQueryResult``) into the
app's standard frontend ``ChatResponse`` dict contract.

Design goals:
  - Preserve every existing ChatResponse field without change.
  - Add Genie-specific optional fields as extensions (source, genie_*,
    suggested_questions, has_visualization, etc.).
  - Text-only responses (no SQL, no table) are fully supported — important
    for Genie's "quick summary" turn which returns only a text attachment.
  - Viz attachments are represented as references only. Genie does NOT
    expose a chart spec via the API. ``has_visualization=True`` signals
    to the frontend that it should render a chart from the query result
    data using its own charting library (Recharts, etc.).
  - Debug mode includes raw SQL and thoughts; production mode omits them
    from the visible response but always includes ``generated_sql`` for
    upstream audit logging.
  - Error/failed messages return safe user-facing text without stack traces.

Frontend contract preserved (ChatResponse fields):
  status           str  — success | error | greeting | off_topic | clarification
  message          str  — primary answer text
  is_table         bool — True when table_data rows exist
  table_data       dict | None  — {"headers": [...], "rows": [[...], ...]}
  row_count        int
  download_key     str | None  — unchanged (set by caller if needed)
  execution_time_ms int
  conversation_id  str | None  — app-level conversation ID
  clarification    str | None  — unchanged

Genie-specific extensions (new optional fields):
  source                    "genie"
  genie_conversation_id     str | None
  genie_message_id          str | None
  generated_sql             str | None  — always present for audit; callers may
                                          strip before sending to frontend
  query_description         str | None  — query.description from Genie (short
                                          plain-English label for the query,
                                          e.g. "Shipments from Germany by mode").
                                          Surfaced as a context line in the UI.
  genie_thought_description str | None  — THOUGHT_TYPE_DESCRIPTION text from
                                          the query attachment thoughts list.
                                          More verbose than query_description;
                                          only shown when GENIE_DEBUG=true.
  suggested_questions       list[str]   — follow-up chips
  has_visualization         bool
  visualization             dict | None — render strategy reference
  attachment_types          list[str]   — e.g. ["text", "query", "viz"]
  debug_info                dict | None — only when debug=True
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Status values understood by the frontend
_STATUS_SUCCESS  = "success"
_STATUS_ERROR    = "error"
_STATUS_GREETING = "greeting"

# Genie terminal statuses that map to error
_ERROR_STATUSES = frozenset({"FAILED", "CANCELLED"})

# Safe user-facing fallback message
_GENIE_ERROR_MESSAGE = (
    "I wasn't able to complete that request. Please try rephrasing your question."
)
_GENIE_EMPTY_MESSAGE = (
    "I processed your request but couldn't find a relevant answer. "
    "Try asking in a different way."
)


# =============================================================================
# PUBLIC MAPPER FUNCTION
# =============================================================================


def map_genie_message_to_chat_response(
    message: Any,  # GenieMessage — typed as Any to avoid circular imports in tests
    query_result: Optional[Any] = None,  # GenieQueryResult | None
    app_conversation_id: Optional[str] = None,
    genie_conversation_id: Optional[str] = None,
    execution_time_ms: Optional[int] = None,
    debug: bool = False,
) -> Dict[str, Any]:
    """Map a GenieMessage into the frontend ChatResponse dict contract.

    This is a pure function with no I/O — safe to call from any context
    and fully testable with plain dataclass instances.

    Args:
        message:              Parsed GenieMessage from GenieClient.
        query_result:         Optional GenieQueryResult containing rows and schema.
                              When present, table_data and row_count are populated.
        app_conversation_id:  App-level conversation UUID (returned to frontend).
        genie_conversation_id: Genie-side conversation UUID (for multi-turn tracking).
        execution_time_ms:    Total wall-clock time for the Genie round trip.
        debug:                When True, includes ``debug_info`` with raw SQL and
                              chain-of-thought from the query attachment.

    Returns:
        Dict matching ChatResponse fields plus Genie extension fields.
    """
    # ------------------------------------------------------------------
    # Validate input
    # ------------------------------------------------------------------
    if message is None:
        return _error_response(
            app_conversation_id=app_conversation_id,
            genie_conversation_id=genie_conversation_id,
            execution_time_ms=execution_time_ms or 0,
            user_message=_GENIE_ERROR_MESSAGE,
        )

    genie_status = getattr(message, "status", "")

    # ------------------------------------------------------------------
    # Rule 7: FAILED / CANCELLED → safe error response
    # ------------------------------------------------------------------
    if genie_status in _ERROR_STATUSES:
        logger.warning(
            "Genie message %s reached terminal error status: %s",
            getattr(message, "message_id", "?"),
            genie_status,
        )
        return _error_response(
            app_conversation_id=app_conversation_id,
            genie_conversation_id=genie_conversation_id,
            execution_time_ms=execution_time_ms or 0,
            user_message=_GENIE_ERROR_MESSAGE,
        )

    # ------------------------------------------------------------------
    # Extract all attachment types
    # ------------------------------------------------------------------
    text_attachments     = getattr(message, "text_attachments", []) or []
    query_attachments    = getattr(message, "query_attachments", []) or []
    viz_attachments      = getattr(message, "viz_attachments", []) or []
    suggested_questions  = getattr(message, "suggested_questions", []) or []

    attachment_types: List[str] = []
    if text_attachments:
        attachment_types.append("text")
    if query_attachments:
        attachment_types.append("query")
    if viz_attachments:
        attachment_types.append("viz")
    if suggested_questions:
        attachment_types.append("suggested_questions")

    # ------------------------------------------------------------------
    # Rule 1: Text attachment → primary message
    # ------------------------------------------------------------------
    primary_message = _extract_primary_message(
        text_attachments=text_attachments,
        query_attachments=query_attachments,
        fallback=_GENIE_EMPTY_MESSAGE,
    )

    # ------------------------------------------------------------------
    # Rule 2 & 3: Query attachment → SQL + table data + description/thoughts
    # ------------------------------------------------------------------
    generated_sql: Optional[str]       = None
    statement_id:  Optional[str]       = None
    query_description: Optional[str]   = None
    genie_thought_description: Optional[str] = None
    query_row_count = 0

    if query_attachments:
        first_query = query_attachments[0]
        generated_sql   = getattr(first_query, "sql", None) or None
        statement_id    = getattr(first_query, "statement_id", None) or None
        query_row_count = getattr(first_query, "row_count", 0) or 0

        # query.description — short plain-English label for the query
        _raw_desc = getattr(first_query, "description", None) or ""
        if _raw_desc.strip():
            query_description = _raw_desc.strip()

        # thoughts — extract THOUGHT_TYPE_DESCRIPTION
        thoughts = getattr(first_query, "thoughts", []) or []
        for thought in thoughts:
            _type  = getattr(thought, "type",    "") if hasattr(thought, "type")  else (thought.get("type",    "") if isinstance(thought, dict) else "")
            _value = getattr(thought, "value",   "") if hasattr(thought, "value") else (thought.get("value",  "") if isinstance(thought, dict) else "")
            if "DESCRIPTION" in str(_type).upper() and str(_value).strip():
                genie_thought_description = str(_value).strip()
                break

    # ------------------------------------------------------------------
    # Rule 3: Query result → table_data
    # ------------------------------------------------------------------
    is_table    = False
    table_data: Optional[Dict[str, Any]] = None
    final_row_count = query_row_count

    if query_result is not None:
        table_data, final_row_count = _build_table_data(query_result)
        is_table = final_row_count > 0

    # ------------------------------------------------------------------
    # Rule 5: Viz attachment → visualization reference
    # ------------------------------------------------------------------
    has_visualization = len(viz_attachments) > 0
    visualization: Optional[Dict[str, Any]] = None

    if has_visualization:
        first_viz = viz_attachments[0]
        visualization = {
            # Genie does NOT return a chart spec. This is a reference pointer only.
            "type": "genie_viz_reference",
            "query_attachment_id": getattr(first_viz, "query_attachment_id", ""),
            # Signal to the frontend: render a chart from the query result rows
            "render_strategy": "client_side_from_query_result",
            # True when we actually have row data to render
            "can_render_client_side": (
                table_data is not None and final_row_count > 0
            ),
        }

    # ------------------------------------------------------------------
    # Rule 8: Attachment metadata
    # ------------------------------------------------------------------
    genie_message_id = (
        getattr(message, "message_id", None)
        or getattr(message, "id", None)
    )

    # ------------------------------------------------------------------
    # Rule 10 (debug): debug_info
    # ------------------------------------------------------------------
    debug_info: Optional[Dict[str, Any]] = None
    if debug:
        thoughts = []
        if query_attachments:
            thoughts = getattr(query_attachments[0], "thoughts", []) or []

        debug_info = {
            "genie_status":     genie_status,
            "generated_sql":    generated_sql,
            "statement_id":     statement_id,
            "thoughts":         thoughts,
            "attachment_types": attachment_types,
            "raw_row_count":    final_row_count,
        }

    # ------------------------------------------------------------------
    # Build the final response dict
    # ------------------------------------------------------------------
    response: Dict[str, Any] = {
        # --- Existing ChatResponse fields (unchanged contract) ---
        "status":           _STATUS_SUCCESS,
        "message":          primary_message,
        "is_table":         is_table,
        "table_data":       table_data,
        "row_count":        final_row_count,
        "download_key":     None,          # caller sets this after export if needed
        "execution_time_ms": execution_time_ms or 0,
        "conversation_id":  app_conversation_id,
        "clarification":    None,
        # --- Genie extension fields ---
        "source":                  "genie",
        "genie_conversation_id":   genie_conversation_id or getattr(message, "conversation_id", None),
        "genie_message_id":        genie_message_id,
        "generated_sql":              generated_sql,   # always for audit; callers may redact
        "query_description":          query_description,
        "genie_thought_description":  genie_thought_description,
        "suggested_questions":        list(suggested_questions),
        "has_visualization":          has_visualization,
        "visualization":              visualization,
        "attachment_types":           attachment_types,
        "debug_info":                 debug_info,
    }

    return response


# =============================================================================
# INTERNAL HELPERS
# =============================================================================


def _extract_primary_message(
    text_attachments: List[Any],
    query_attachments: List[Any],
    fallback: str,
) -> str:
    """Build the primary text message shown to the user.

    Priority order:
    1. All text attachments, concatenated with a blank line separator.
    2. Description from the first query attachment.
    3. Fallback string.
    """
    if text_attachments:
        parts = [
            getattr(att, "content", "") or ""
            for att in text_attachments
        ]
        # Filter empty strings then join
        non_empty = [p.strip() for p in parts if p.strip()]
        if non_empty:
            return "\n\n".join(non_empty)

    if query_attachments:
        description = getattr(query_attachments[0], "description", "") or ""
        if description.strip():
            return description.strip()

    return fallback


def _build_table_data(
    query_result: Any,
) -> tuple:
    """Convert a GenieQueryResult into (table_data_dict, row_count).

    Supports two row formats:
    a. rows as List[List]   — native Genie / Statement Execution API output.
    b. rows as List[Dict]   — some intermediate transforms may produce this.

    Always returns headers as List[str] derived from ``columns``.
    Always returns rows as List[List] to match the existing TableData schema
    (consistent with how pipeline_adapter.py builds table_data).

    Args:
        query_result: GenieQueryResult dataclass.

    Returns:
        Tuple of (table_data dict | None, row_count int).
    """
    columns = getattr(query_result, "columns", []) or []
    rows    = getattr(query_result, "rows", [])    or []
    total   = getattr(query_result, "total_row_count", None)

    # Extract column names
    headers: List[str] = []
    for col in columns:
        if isinstance(col, dict):
            headers.append(col.get("name", ""))
        elif hasattr(col, "name"):
            headers.append(col.name)
        else:
            headers.append(str(col))

    if not rows:
        return None, 0

    # Normalise rows to List[List]
    list_rows: List[List] = []
    for row in rows:
        if isinstance(row, (list, tuple)):
            list_rows.append(list(row))
        elif isinstance(row, dict):
            # Convert dict row using header order
            if headers:
                list_rows.append([row.get(h) for h in headers])
            else:
                list_rows.append(list(row.values()))
        else:
            list_rows.append([row])

    row_count = len(list_rows)
    if total is not None and total > row_count:
        # Total may be larger than the returned page (Genie paginates at 5000)
        displayed_count = row_count
    else:
        displayed_count = row_count

    table_data = {
        "headers": headers,
        "rows":    list_rows,
    }
    return table_data, displayed_count


def _error_response(
    app_conversation_id: Optional[str],
    genie_conversation_id: Optional[str],
    execution_time_ms: int,
    user_message: str = _GENIE_ERROR_MESSAGE,
) -> Dict[str, Any]:
    """Build a safe error response. Never exposes stack traces."""
    return {
        # Existing fields
        "status":           _STATUS_ERROR,
        "message":          user_message,
        "is_table":         False,
        "table_data":       None,
        "row_count":        0,
        "download_key":     None,
        "execution_time_ms": execution_time_ms,
        "conversation_id":  app_conversation_id,
        "clarification":    None,
        # Genie extensions
        "source":                  "genie",
        "genie_conversation_id":   genie_conversation_id,
        "genie_message_id":        None,
        "generated_sql":           None,
        "suggested_questions":     [],
        "has_visualization":       False,
        "visualization":           None,
        "attachment_types":        [],
        "debug_info":              None,
    }
