"""Genie pipeline orchestrator.

Connects the Genie backend components into a single, testable end-to-end turn:

    GenieSessionStore          — look up or create Genie conversation mapping
    → GenieClient.start_conversation  OR  .send_message
    → GenieClient.wait_for_message_completion  (polling)
    → GenieClient.fetch_query_result           (optional rows)
    → map_genie_message_to_chat_response        (mapper)
    → ChatResponse-compatible dict

This class is wired into chat.py behind the ``USE_GENIE_BACKEND`` feature
flag in Phase G4/G5.  It does NOT touch chat.py in this phase.

Design mirrors ChatPipeline in chat_pipeline.py:
  - ``run()``          : public entry point, catches all exceptions
  - ``_run_inner()``   : core logic, raises on error
  - ``_build_error_response()`` : safe error dict, always includes fallback signal

E5 additions:
  - Suggestion canonicalization: known chip labels are mapped to canonical
    enriched prompts before routing so that clickable suggestions always
    produce the correct aggregated result.
  - Result-shape validation + one-shot retry: if an AGGREGATION intent
    returns raw shipment rows, the pipeline retries once with a stronger
    GROUP-BY-explicit canonical prompt.
  - Message-level export context: every table response registers a
    TableExportRecord in the session store so that a subsequent
    "download this data" request resolves to the *latest* table's export,
    not a stale export from an earlier turn.
"""

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.services.genie_client import (
    GenieClientError,
    GenieExecutionError,
    GenieTimeoutError,
)
from app.services.genie_response_mapper import map_genie_message_to_chat_response
from app.services.genie_session_store import GenieSessionStore, TableExportRecord
from app.services.pre_genie_router import route_pre_genie, PreGenieRouteDecision
from app.services.genie_table_summarizer import summarize_table
from app.services.result_shape_validator import (
    is_aggregation_shape_mismatch,
    is_analytical_shape_mismatch,
    build_retry_prompt,
)

logger = logging.getLogger(__name__)


# =============================================================================
# USER-FACING MESSAGES  (no stack traces, no internal identifiers)
# =============================================================================

_MSG_ERROR   = (
    "I wasn't able to complete that request. "
    "Please try again or rephrase your question."
)
_MSG_TIMEOUT = (
    "The request took too long to complete. "
    "Please try again — the data warehouse may have been starting up."
)


# =============================================================================
# SUGGESTION CANONICALIZATION  (Contract 5)
#
# Maps known chip display labels to deterministic canonical prompts.
# This ensures clickable suggestion chips always route to the correct intent
# and produce the correct aggregated / filtered result.
#
# Keys are normalised (lowercased, stripped) chip display text.
# Values are the enriched canonical prompts sent to Genie.
# =============================================================================

_SUGGESTION_CANONICAL_MAP: Dict[str, str] = {
    # Lane aggregation
    "top lanes by volume": (
        "Show the top 10 shipment lanes by shipment count. "
        "Group by source_ and destination. "
        "Return source_, destination, shipment_count. "
        "Sort by shipment_count descending. "
        "Do not return individual shipment-level rows."
    ),
    "top lanes": (
        "Show the top 10 shipment lanes by shipment count. "
        "Group by source_ and destination. "
        "Return source_, destination, shipment_count. "
        "Sort by shipment_count descending. "
        "Do not return individual shipment-level rows."
    ),
    # Status distribution
    "shipment status distribution": (
        "Show the shipment status distribution. "
        "Group by execution_status. "
        "Return execution_status, shipment_count. "
        "Sort by shipment_count descending. "
        "Do not return individual shipment-level rows."
    ),
    # Broad listing — enriched but still forwards to Genie correctly
    "which shipments are in transit?": (
        "Show me shipments that are currently in transit. "
        "Filter where execution_status indicates in-transit. "
        "Return a concise sample list with shipment number, source, destination, mode, and ETA."
    ),
    "which shipments are in transit": (
        "Show me shipments that are currently in transit. "
        "Filter where execution_status indicates in-transit. "
        "Return a concise sample list with shipment number, source, destination, mode, and ETA."
    ),
    "show me delayed shipments": (
        "Show me delayed shipments. "
        "Filter where the shipment is delayed (ETA passed, not yet delivered). "
        "Return shipment number, source, destination, mode, ETA, delay reason if available."
    ),
}


def _canonicalize_user_message(user_message: str) -> str:
    """Map a known suggestion chip label to its canonical enriched prompt.

    Returns the original message unchanged if no mapping is found.
    The comparison is case-insensitive and whitespace-normalised.
    """
    normalised = " ".join(user_message.strip().lower().split())
    # Exact match first
    canonical = _SUGGESTION_CANONICAL_MAP.get(normalised)
    if canonical:
        return canonical
    # Strip trailing punctuation (e.g. "Which shipments are in transit?" → match)
    stripped = normalised.rstrip("?.!")
    canonical = _SUGGESTION_CANONICAL_MAP.get(stripped)
    if canonical:
        return canonical
    return user_message


# =============================================================================
# OWNER-KEY VALIDATION (Phase 4C1)
# =============================================================================

_OWNER_KEY_LENGTH = 64
_OWNER_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_MSG_INVALID_OWNER_KEY = (
    "Internal error: request identity contract violation."
)
_MSG_DURABLE_LOOKUP_UNAVAILABLE = (
    "I wasn't able to complete that request. "
    "Please try again in a moment."
)


class _OwnerKeyContractError(Exception):
    """Internal-only exception for owner-key structural violations.

    This is never exposed through the public response.  It signals that
    the trusted owner key failed structural validation, which is an
    internal contract violation — not a user-facing error.

    When this error is raised, the pipeline MUST NOT fall back to the
    custom pipeline because the trusted identity contract is broken.
    """


class _DurableLookupUnavailableError(Exception):
    """Internal-only exception for durable lookup failures.

    Raised when the durable Genie session lookup encounters a repository
    unavailability or returns an unconfirmed degraded read.  Processing
    MUST NOT proceed without confirmed durable state when durable mode
    is enabled.  Fallback to custom pipeline is prohibited.
    """


def _validate_owner_key(owner_key: object) -> None:
    """Validate the structural contract of a trusted owner key."""
    if not isinstance(owner_key, str):
        raise _OwnerKeyContractError(_MSG_INVALID_OWNER_KEY)
    if owner_key != owner_key.strip():
        raise _OwnerKeyContractError(_MSG_INVALID_OWNER_KEY)
    if not owner_key:
        raise _OwnerKeyContractError(_MSG_INVALID_OWNER_KEY)
    if "@" in owner_key:
        raise _OwnerKeyContractError(_MSG_INVALID_OWNER_KEY)
    if len(owner_key) != _OWNER_KEY_LENGTH:
        raise _OwnerKeyContractError(_MSG_INVALID_OWNER_KEY)
    if not _OWNER_KEY_RE.match(owner_key):
        raise _OwnerKeyContractError(_MSG_INVALID_OWNER_KEY)


# =============================================================================
# PIPELINE
# =============================================================================


class GeniePipeline:
    """Orchestrates one complete Genie backend turn.

    Designed for dependency injection — all dependencies are passed in the
    constructor.  The pipeline has no direct dependency on FastAPI, the
    Databricks SDK, or any live HTTP connection, making it fully unit-testable
    with a FakeGenieClient.

    Usage::

        pipeline = GeniePipeline(
            genie_client=GenieClient(),
            session_store=GenieSessionStore(),
            space_id="01f17a93e6aa1b97a9da7ef329e15e46",
        )
        response = pipeline.run(
            user_message="shipments from US via air",
            app_conversation_id="conv-uuid-123",
        )
    """

    def __init__(
        self,
        genie_client: Any,            # duck-typed: must implement Genie client interface
        session_store: GenieSessionStore,
        space_id: str,
        timeout_seconds: int = 120,
        poll_interval_seconds: float = 2.0,
        debug: bool = False,
        fetch_query_results: bool = True,
        enable_prompt_enrichment: bool = True,
        table_display_row_limit: int = 100,
        max_download_rows: int = 5000,
        audit_service: Any = None,
        async_export_enabled: bool = False,
        export_mode: str = "returned_rows_only",
        max_export_rows: int = 100000,
        export_query_timeout_seconds: int = 300,
        export_strip_limit: bool = True,
        export_job_manager: Any = None,
        sql_service: Any = None,
        enable_table_summary: bool = True,
        summary_threshold: int = 50,
        summary_top_n: int = 5,
        enable_computed_chart: bool = True,
        enable_shape_validation: bool = True,
    ):
        """Initialise the pipeline.

        Args:
            genie_client:           GenieClient (or compatible fake).
            session_store:          GenieSessionStore that maps app conversation
                                    IDs to Genie conversation IDs.
            space_id:               Genie Space UUID to converse in.
            timeout_seconds:        Maximum time to wait for Genie to complete
                                    a message (polling timeout).
            poll_interval_seconds:  Sleep between status polls.
            debug:                  When True, includes ``debug_info`` in the
                                    response (SQL, thoughts, attachment types).
            fetch_query_results:    When True, fetches row data from the
                                    Statement Execution API for query attachments.
                                    Set False in tests that do not need rows.
            enable_shape_validation: When True (default), validates AGGREGATION
                                    results and retries once if raw shipment rows
                                    are returned instead of aggregated metrics.
        """
        self._client          = genie_client
        self._store           = session_store
        self._space_id        = space_id
        self._timeout         = timeout_seconds
        self._poll_interval   = poll_interval_seconds
        self._debug           = debug
        self._fetch_query_results     = fetch_query_results
        self._enable_prompt_enrichment = enable_prompt_enrichment
        self._table_display_row_limit  = table_display_row_limit
        self._max_download_rows        = max_download_rows
        self._audit_service            = audit_service
        self._async_export_enabled     = async_export_enabled
        self._export_mode              = export_mode
        self._max_export_rows          = max_export_rows
        self._export_query_timeout     = export_query_timeout_seconds
        self._export_strip_limit       = export_strip_limit
        self._export_job_manager       = export_job_manager
        self._sql_service              = sql_service
        self._enable_table_summary     = enable_table_summary
        self._summary_threshold        = summary_threshold
        self._summary_top_n            = summary_top_n
        self._enable_computed_chart    = enable_computed_chart
        self._enable_shape_validation  = enable_shape_validation

    # -------------------------------------------------------------------------
    # PUBLIC ENTRY POINT
    # -------------------------------------------------------------------------

    def run(
        self,
        user_message: str,
        app_conversation_id: str,
        execution_time_ms: Optional[int] = None,
        *,
        owner_key: Optional[str] = None,
        frontend_conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute one turn of the Genie conversation.

        Always returns a dict — never raises.  Errors are caught and
        converted to a safe response with ``status="error"`` and
        ``fallback_recommended=True`` so that chat.py can route to the
        custom pipeline if configured.

        Args:
            user_message:           Natural language query from the user.
            app_conversation_id:    App-level conversation UUID.  Used to look
                                    up and persist the Genie conversation mapping
                                    in the session store.
            execution_time_ms:      If the caller already has a measured start
                                    time (e.g. from the HTTP request), pass the
                                    elapsed ms here.  When None, the pipeline
                                    measures its own wall-clock time.
            owner_key:              Optional trusted owner identity hash
                                    (Phase 4C1).  When supplied, must be a
                                    64-character lowercase hexadecimal string.
                                    Validated structurally; used in Phase 4C2A
                                    for durable session lookup.
            frontend_conversation_id:  Optional raw frontend conversation ID
                                    (Phase 4C2A).  This is the browser-supplied
                                    conversation identifier used together with
                                    owner_key to build the durable ownership
                                    key.  Must not be parsed from
                                    app_conversation_id.

        Returns:
            ChatResponse-compatible dict.  Successful turns include
            ``fallback_recommended=False``; error turns include
            ``fallback_recommended=True``.
        """
        start_time = time.monotonic()

        try:
            # Phase 4C1: Structural validation of trusted owner key.
            # The key is request-local only; it is NOT stored on the pipeline
            # instance, NOT logged, NOT passed to Genie, NOT included in
            # session state, and NOT exposed in any response.
            if owner_key is not None:
                _validate_owner_key(owner_key)

            # Phase 4C2A: Read-only durable Genie session lookup.
            # When the durable runtime bundle is enabled and both owner_key
            # and frontend_conversation_id are present, perform exactly one
            # read-only lookup to recover an existing Genie conversation
            # mapping from the durable repository.
            self._durable_session_lookup(
                owner_key=owner_key,
                frontend_conversation_id=frontend_conversation_id,
                app_conversation_id=app_conversation_id,
            )

            return self._run_inner(
                user_message, app_conversation_id, start_time, execution_time_ms
            )

        except GenieTimeoutError as exc:
            _exc_str = str(exc)[:300]
            logger.warning(
                "GeniePipeline: timeout for app_conv=%s: %s",
                app_conversation_id, _exc_str,
            )
            _dbg_msg = f"Genie debug [GenieTimeoutError]: {_exc_str}" if self._debug else _MSG_TIMEOUT
            return self._build_error_response(
                app_conversation_id, start_time, execution_time_ms,
                user_message=_dbg_msg,
            )

        except GenieExecutionError as exc:
            _exc_str = str(exc)[:300]
            logger.warning(
                "GeniePipeline: execution error for app_conv=%s: %s",
                app_conversation_id, _exc_str,
            )
            _dbg_msg = f"Genie debug [GenieExecutionError]: {_exc_str}" if self._debug else _MSG_ERROR
            return self._build_error_response(
                app_conversation_id, start_time, execution_time_ms,
                user_message=_dbg_msg,
            )

        except GenieClientError as exc:
            _exc_str = str(exc)[:300]
            logger.error(
                "GeniePipeline: client error for app_conv=%s: %s",
                app_conversation_id, _exc_str,
            )
            _dbg_msg = f"Genie debug [GenieClientError]: {_exc_str}" if self._debug else _MSG_ERROR
            return self._build_error_response(
                app_conversation_id, start_time, execution_time_ms,
                user_message=_dbg_msg,
            )

        except _OwnerKeyContractError:
            # Owner-key contract violation: MUST NOT fall back to custom
            # pipeline.  The trusted identity contract is broken; processing
            # without the owner key is prohibited.  No details are logged.
            return self._build_owner_key_error_response(
                app_conversation_id, start_time, execution_time_ms,
            )

        except _DurableLookupUnavailableError:
            # Durable lookup unavailable: MUST NOT fall back to custom
            # pipeline and MUST NOT start a new Genie conversation.
            return self._build_durable_lookup_error_response(
                app_conversation_id, start_time, execution_time_ms,
            )

        except Exception as exc:  # noqa: BLE001
            _exc_str = str(exc)[:300]
            logger.error(
                "GeniePipeline: unexpected error for app_conv=%s: %s",
                app_conversation_id, _exc_str,
            )
            _dbg_msg = f"Genie debug [{type(exc).__name__}]: {_exc_str}" if self._debug else _MSG_ERROR
            return self._build_error_response(
                app_conversation_id, start_time, execution_time_ms,
                user_message=_dbg_msg,
            )

    # -------------------------------------------------------------------------
    # CORE LOGIC  (raises on error — caller handles exceptions)
    # -------------------------------------------------------------------------

    def _run_inner(
        self,
        user_message: str,
        app_conversation_id: str,
        start_time: float,
        execution_time_ms: Optional[int],
        *,
        _is_shape_retry: bool = False,
    ) -> Dict[str, Any]:
        """Execute the full pipeline turn.  Raises on any failure.

        Args:
            _is_shape_retry: Internal flag — set True when this call is a
                one-shot shape-validation retry.  Prevents recursive retrying.
        """

        # -----------------------------------------------------------------
        # Step 1: Load current app-side context + existing Genie session
        # -----------------------------------------------------------------
        context = self._store.get_context_snapshot(app_conversation_id)
        genie_conv_id: Optional[str] = context.get("genie_conversation_id")

        # -----------------------------------------------------------------
        # Step 2: Canonicalize + Route
        #
        # Canonicalize known suggestion chip labels to their deterministic
        # canonical prompts *before* routing so that suggestion clicks always
        # produce the correct intent and enrichment.  The original_user_message
        # is preserved for context storage and display.
        # -----------------------------------------------------------------
        original_user_message = user_message
        canonical_message = _canonicalize_user_message(user_message)
        if canonical_message != user_message:
            logger.debug(
                "GeniePipeline: canonicalized suggestion %r → %r for app_conv=%s",
                user_message, canonical_message[:80], app_conversation_id,
            )
            user_message = canonical_message

        route_decision: PreGenieRouteDecision = route_pre_genie(
            user_message,
            is_follow_up=bool(genie_conv_id) and bool(context.get("last_intent")),
            previous_entities=context.get("last_entities") or [],
            previous_entity_type=context.get("last_entity_type"),
            previous_filters=context.get("last_filters") or {},
            previous_intent=context.get("last_intent"),
            last_download_key=context.get("last_download_key"),
            last_export_id=context.get("last_export_id"),
            last_export_status=context.get("last_export_status"),
            last_export_mode=context.get("last_export_mode"),
            last_export_row_count=context.get("last_export_row_count"),
            latest_table_result=context.get("latest_table_result"),
            export_job_manager=self._export_job_manager,
            enable_prompt_enrichment=self._enable_prompt_enrichment,
        )
        genie_message = route_decision.get("enriched_prompt") or user_message

        # P1 observability: always log intent at INFO so post-idle degradation
        # is visible in production logs without GENIE_DEBUG=true.
        _ep = route_decision.get("enriched_prompt") or ""
        logger.info(
            "GeniePipeline: intent=%s is_follow_up=%s enriched_len=%d app_conv=%s",
            route_decision.get("intent"),
            route_decision.get("is_follow_up"),
            len(_ep),
            app_conversation_id,
        )
        if self._debug:
            logger.debug(
                "GeniePipeline: router intent=%s should_call_genie=%s is_follow_up=%s reason=%r app_conv=%s",
                route_decision.get("intent"),
                route_decision.get("should_call_genie"),
                route_decision.get("is_follow_up"),
                route_decision.get("reason"),
                app_conversation_id,
            )

        if not route_decision.get("should_call_genie", True):
            local_response = self._build_local_response(
                route_decision=route_decision,
                app_conversation_id=app_conversation_id,
                genie_conversation_id=genie_conv_id,
                start_time=start_time,
                execution_time_ms=execution_time_ms,
            )
            self._store.update_context(
                app_conversation_id,
                last_intent=route_decision.get("intent"),
                last_user_prompt=original_user_message,
                last_enriched_prompt=route_decision.get("enriched_prompt"),
            )
            return local_response

        # -----------------------------------------------------------------
        # Step 3: Start new conversation or send follow-up
        # Reuse Genie context only for explicit conversational follow-ups.
        # All other routed prompts start a fresh Genie conversation so stale
        # Genie-side context does not distort a new standalone query.
        # -----------------------------------------------------------------
        reuse_genie_context = (
            bool(genie_conv_id)
            and route_decision.get("intent") == "TRUE_FOLLOW_UP"
        )

        if not reuse_genie_context and genie_conv_id is not None:
            self._store.reset_genie_mapping(app_conversation_id)
            genie_conv_id = None

        if genie_conv_id is None:
            # --- New conversation (first message, correction, or standalone query) ---
            resp = self._client.start_conversation(self._space_id, genie_message)

            genie_conv_id = resp.get("conversation_id", "")
            message_id    = resp.get("message_id", "")

            if not genie_conv_id or not message_id:
                raise GenieClientError(
                    f"start_conversation returned incomplete response — "
                    f"keys present: {sorted(resp.keys())}"
                )

            # Persist the mapping immediately so genuine follow-ups work
            self._store.set_genie_conversation_id(app_conversation_id, genie_conv_id)
            logger.debug(
                "GeniePipeline: started genie_conv=%s for app_conv=%s intent=%s",
                genie_conv_id, app_conversation_id, route_decision.get("intent"),
            )

        else:
            # --- Follow-up in existing Genie conversation ---
            resp = self._client.send_message(
                self._space_id, genie_conv_id, genie_message
            )
            message_id = resp.get("message_id", "")

            if not message_id:
                raise GenieClientError(
                    f"send_message returned no message_id — "
                    f"keys present: {sorted(resp.keys())}"
                )

            logger.debug(
                "GeniePipeline: follow-up msg=%s in genie_conv=%s for app_conv=%s",
                message_id, genie_conv_id, app_conversation_id,
            )

        # -----------------------------------------------------------------
        # Step 4: Store most-recent message_id for audit / correlation
        # -----------------------------------------------------------------
        self._store.set_last_message_id(app_conversation_id, message_id)

        # -----------------------------------------------------------------
        # Step 5: Poll Genie until the message reaches a terminal status
        # -----------------------------------------------------------------
        message = self._client.wait_for_message_completion(
            self._space_id,
            genie_conv_id,
            message_id,
            timeout_seconds=self._timeout,
            poll_interval_seconds=self._poll_interval,
        )

        # -----------------------------------------------------------------
        # Step 6: Optionally fetch query result rows
        # Skipped when: fetch_query_results=False, or no query attachment.
        # Failure is non-fatal — a text-only response is still returned.
        # -----------------------------------------------------------------
        query_result = None

        if self._fetch_query_results:
            stmt_ids: List[str] = [
                q.statement_id
                for q in (getattr(message, "query_attachments", None) or [])
                if getattr(q, "statement_id", None)
            ]

            if stmt_ids:
                try:
                    query_result = self._client.fetch_query_result(
                        self._space_id,
                        genie_conv_id,
                        message_id,
                        statement_id=stmt_ids[0],   # first (primary) result
                        fetch_rows=True,
                        row_limit=self._determine_fetch_row_limit(),
                    )
                    logger.info(
                        "GeniePipeline: genie_response rows=%s headers=%s source=genie app_conv=%s",
                        getattr(query_result, "row_count", "?"),
                        getattr(query_result, "headers", [])[:5],
                        app_conversation_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    # Non-fatal: text response is still valuable without rows
                    logger.warning(
                        "GeniePipeline: query result fetch failed (non-fatal) "
                        "for app_conv=%s stmt=%s: %s",
                        app_conversation_id,
                        stmt_ids[0],
                        str(exc)[:150],
                    )

        # -----------------------------------------------------------------
        # Step 7: Calculate total elapsed time
        # -----------------------------------------------------------------
        elapsed_ms = (
            execution_time_ms
            if execution_time_ms is not None
            else int((time.monotonic() - start_time) * 1000)
        )

        # -----------------------------------------------------------------
        # Step 8: Map to ChatResponse-compatible dict
        # -----------------------------------------------------------------
        response = map_genie_message_to_chat_response(
            message=message,
            query_result=query_result,
            app_conversation_id=app_conversation_id,
            genie_conversation_id=genie_conv_id,
            execution_time_ms=elapsed_ms,
            debug=self._debug,
        )

        # -----------------------------------------------------------------
        # Step 8b: Result-shape validation — one-shot retry for AGGREGATION
        #          and analytical BROAD_LISTING / GENERAL (E6)
        #
        # If Genie returned raw shipment-level rows for an AGGREGATION intent,
        # OR for a BROAD_LISTING/GENERAL intent whose prompt contains analytical
        # vocabulary (analysis, insights, overview, …), retry once with a
        # stronger canonical prompt.
        # Controlled by enable_shape_validation and _is_shape_retry flag
        # (prevents recursion — maximum one retry per turn).
        # -----------------------------------------------------------------
        _current_intent = route_decision.get("intent", "")
        if (
            self._enable_shape_validation
            and not _is_shape_retry
            and _current_intent in ("AGGREGATION", "BROAD_LISTING", "GENERAL", "SUMMARY_REQUEST")
            and response.get("is_table")
            and response.get("table_data")
        ):
            _sv_headers = response.get("table_data", {}).get("headers", [])
            # P1 observability: log shape validation check at INFO
            logger.info(
                "GeniePipeline: shape_validation_check intent=%s headers=%s is_retry=%s app_conv=%s",
                _current_intent, _sv_headers[:5], _is_shape_retry, app_conversation_id,
            )
            _sv_result = is_analytical_shape_mismatch(
                _current_intent, _sv_headers, original_user_message
            )
            logger.info(
                "GeniePipeline: shape_validation=%s intent=%s app_conv=%s",
                "MISMATCH" if _sv_result else "PASS",
                _current_intent, app_conversation_id,
            )
            if _sv_result:
                _retry_prompt = build_retry_prompt(
                    route_decision.get("original_prompt") or original_user_message,
                    _current_intent,
                )
                logger.info(
                    "GeniePipeline: shape_mismatch_retry intent=%s headers=%s app_conv=%s",
                    _current_intent, _sv_headers[:5], app_conversation_id,
                )
                # Reset Genie mapping so the retry starts a fresh conversation
                self._store.reset_genie_mapping(app_conversation_id)
                # One-shot retry — _is_shape_retry=True prevents further recursion
                return self._run_inner(
                    _retry_prompt, app_conversation_id, start_time, execution_time_ms,
                    _is_shape_retry=True,
                )

        # -----------------------------------------------------------------
        # Step 8c: Retry exhaustion safety (FIX 5)
        #
        # When _is_shape_retry=True this turn is a one-shot retry triggered
        # by a shape-validation mismatch in the previous turn.  If the retry
        # ALSO returns raw shipment-level rows (shape mismatch confirmed), the
        # result must NOT be silently accepted as a valid analytical response.
        # Mark fallback_recommended=True and shape_retry_exhausted=True so
        # the caller (chat.py) can route to the custom pipeline.
        # Only fires when enable_shape_validation=True and a table is present.
        # Does not discard the response; the fallback handler decides
        # what to return to the user.
        # -----------------------------------------------------------------
        if (
            _is_shape_retry
            and self._enable_shape_validation
            and _current_intent in ("AGGREGATION", "BROAD_LISTING", "GENERAL", "SUMMARY_REQUEST")
            and response.get("is_table")
            and response.get("table_data")
        ):
            _retry_sv_headers = response.get("table_data", {}).get("headers", [])
            _retry_sv_result = is_analytical_shape_mismatch(
                _current_intent, _retry_sv_headers, original_user_message
            )
            if _retry_sv_result:
                logger.warning(
                    "GeniePipeline: shape_retry_exhausted intent=%s headers=%s "
                    "app_conv=%s \u2014 retry also returned raw rows; fallback_recommended",
                    _current_intent, _retry_sv_headers[:5], app_conversation_id,
                )
                response["fallback_recommended"] = True
                response["shape_retry_exhausted"] = True

        # -----------------------------------------------------------------
        # Step 9 (E4): truthful row counts + export preparation
        # -----------------------------------------------------------------
        response.setdefault("download_key", None)
        response.setdefault("export_id", None)
        response.setdefault("export_status", None)
        response.setdefault("export_mode", None)
        response.setdefault("export_row_count", None)
        response.setdefault("display_row_limit", self._table_display_row_limit)
        response.setdefault("preview_row_count", 0)
        response.setdefault("returned_row_count", 0)
        response.setdefault("total_row_count", None)

        if response.get("is_table") and response.get("table_data"):
            td = response["table_data"]
            headers = td.get("headers", [])
            all_rows = td.get("rows", [])

            returned_row_count = (
                getattr(query_result, "row_count", None)
                if query_result is not None
                else None
            )
            if returned_row_count is None:
                returned_row_count = response.get("row_count", len(all_rows))
            total_row_count = (
                getattr(query_result, "total_row_count", None)
                if query_result is not None
                else None
            )
            if total_row_count is None:
                total_row_count = returned_row_count

            preview_rows = all_rows[: self._table_display_row_limit]
            response["table_data"] = {**td, "rows": preview_rows}
            response["display_row_count"] = len(preview_rows)
            response["preview_row_count"] = len(preview_rows)
            response["returned_row_count"] = returned_row_count
            response["total_row_count"] = total_row_count
            response["row_count"] = returned_row_count

            if self._audit_service is not None:
                if self._async_export_enabled and self._export_job_manager is not None:
                    export_meta = self._start_async_export(
                        app_conversation_id=app_conversation_id,
                        headers=headers,
                        rows=all_rows,
                        response=response,
                    )
                    response.update(export_meta)
                else:
                    export_row_count, download_key = self._create_sync_export(headers, all_rows)
                    if download_key:
                        response["download_key"] = download_key
                        response["export_status"] = "ready"
                        response["export_mode"] = "returned_rows_only"
                        response["export_row_count"] = export_row_count

        # -----------------------------------------------------------------
        # Step 10 (Q3): Deterministic table summarizer
        # Activates when:
        #   - enable_table_summary=True
        #   - row_count > summary_threshold (avoids summarizing small results)
        #   - table_data is present
        # Produces: computed_metrics, summary_text, computed_chart_data.
        # summary_text is prepended to the Genie message when the Genie
        # response text is short/weak (< 120 chars or starts with a template).
        # -----------------------------------------------------------------
        response["computed_chart_data"] = None
        response["computed_metrics"]    = None

        td_for_summary = response.get("table_data")
        rc_for_summary = response.get("row_count", 0)

        if (
            self._enable_table_summary
            and td_for_summary
            and rc_for_summary >= self._summary_threshold
        ):
            try:
                _headers = td_for_summary.get("headers", [])
                _rows    = td_for_summary.get("rows", [])
                summary_result = summarize_table(
                    headers=_headers,
                    rows=_rows,
                    row_count=rc_for_summary,
                    top_n=self._summary_top_n,
                    enable_chart=self._enable_computed_chart,
                )

                if summary_result.computed_metrics:
                    response["computed_metrics"] = summary_result.computed_metrics

                # Prepend summary text when Genie message is weak
                existing_msg = response.get("message", "")
                _msg_weak = (
                    not existing_msg
                    or len(existing_msg) < 120
                    or existing_msg.startswith("I processed")  # _GENIE_EMPTY_MESSAGE
                )
                if summary_result.summary_text and _msg_weak:
                    response["message"] = (
                        summary_result.summary_text
                        + ("\n\n" + existing_msg if existing_msg else "")
                    ).strip()

                if summary_result.computed_chart_data:
                    response["computed_chart_data"] = {
                        "data":  summary_result.computed_chart_data,
                        "x_key": summary_result.chart_x_key,
                        "y_key": summary_result.chart_y_key,
                    }

                if self._debug:
                    logger.debug(
                        "GeniePipeline: table summarizer activated rows=%d "
                        "metrics=%s for app_conv=%s",
                        rc_for_summary,
                        list(summary_result.computed_metrics.keys()),
                        app_conversation_id,
                    )

            except Exception as _summ_exc:  # noqa: BLE001
                logger.warning(
                    "GeniePipeline: table summarizer failed (non-fatal) "
                    "for app_conv=%s: %s",
                    app_conversation_id, str(_summ_exc)[:100],
                )

        # -----------------------------------------------------------------
        # Step 11: Persist business context from router + mapped response
        #
        # Contract 7: register a TableExportRecord for every table response
        # (even when download_key is None — this prevents stale exports from
        # being served when the latest table's export failed silently).
        # -----------------------------------------------------------------
        table_data = response.get("table_data") or {}
        headers = table_data.get("headers", []) if isinstance(table_data, dict) else []
        download_key = response.get("download_key")
        row_count = response.get("row_count")
        total_row_count = response.get("total_row_count")
        returned_row_count = response.get("returned_row_count")

        # Build per-message export record for Contract 7
        table_export_record: Optional[TableExportRecord] = None
        if response.get("is_table"):
            table_export_record = TableExportRecord(
                assistant_message_id=response.get("genie_message_id") or message_id,
                download_key=download_key,
                export_id=response.get("export_id"),
                export_status=response.get("export_status"),
                export_mode=response.get("export_mode"),
                export_row_count=response.get("export_row_count"),
                query_description=response.get("query_description"),
                created_at=datetime.now(timezone.utc),
            )

        self._store.update_context(
            app_conversation_id,
            last_entities=route_decision.get("entities") or [],
            last_entity_type=route_decision.get("entity_type"),
            last_filters=route_decision.get("filters") or {},
            last_intent=route_decision.get("intent"),
            last_user_prompt=original_user_message,
            last_enriched_prompt=genie_message,
            last_download_key=download_key,
            last_export_id=response.get("export_id"),
            last_export_status=response.get("export_status"),
            last_export_mode=response.get("export_mode"),
            last_export_row_count=response.get("export_row_count"),
            last_table_headers=headers,
            last_row_count=row_count,
            last_total_row_count=total_row_count,
            last_returned_row_count=returned_row_count,
            latest_table_result=table_export_record,
        )

        if self._debug:
            debug_info = response.get("debug_info") or {}
            if not isinstance(debug_info, dict):
                debug_info = {"raw": debug_info}
            debug_info["router_decision"] = {
                "intent": route_decision.get("intent"),
                "entity_type": route_decision.get("entity_type"),
                "entities": route_decision.get("entities") or [],
                "filters": route_decision.get("filters") or {},
                "is_follow_up": route_decision.get("is_follow_up"),
                "is_correction": route_decision.get("is_correction"),
                "should_call_genie": route_decision.get("should_call_genie"),
                "reason": route_decision.get("reason"),
                "enriched_prompt": genie_message,
            }
            response["debug_info"] = debug_info

        # Successful turn — no fallback needed.
        # Exception: shape_retry_exhausted (FIX 5) already set fallback_recommended=True;
        # preserve that signal so the caller can route to the custom pipeline.
        if not response.get("shape_retry_exhausted"):
            response["fallback_recommended"] = False
        return response

    # -------------------------------------------------------------------------
    # ERROR RESPONSE BUILDER
    # -------------------------------------------------------------------------

    def _build_local_response(
        self,
        route_decision: PreGenieRouteDecision,
        app_conversation_id: str,
        genie_conversation_id: Optional[str],
        start_time: float,
        execution_time_ms: Optional[int],
    ) -> Dict[str, Any]:
        elapsed_ms = (
            execution_time_ms
            if execution_time_ms is not None
            else int((time.monotonic() - start_time) * 1000)
        )

        response: Dict[str, Any] = {
            "status": "success",
            "message": route_decision.get("local_response") or "",
            "is_table": False,
            "table_data": None,
            "row_count": 0,
            "display_row_count": 0,
            "preview_row_count": 0,
            "returned_row_count": 0,
            "total_row_count": None,
            "export_row_count": route_decision.get("export_row_count"),
            "display_row_limit": self._table_display_row_limit,
            "download_key": route_decision.get("download_key"),
            "export_id": route_decision.get("export_id"),
            "export_status": route_decision.get("export_status"),
            "export_mode": route_decision.get("export_mode"),
            "execution_time_ms": elapsed_ms,
            "conversation_id": app_conversation_id,
            "clarification": None,
            "source": "local",
            "genie_conversation_id": genie_conversation_id,
            "genie_message_id": None,
            "generated_sql": None,
            "suggested_questions": [],
            "has_visualization": False,
            "visualization": None,
            "attachment_types": [],
            "debug_info": None,
            "query_description": None,
            "genie_thought_description": None,
            "computed_chart_data": None,
            "computed_metrics": None,
            "fallback_recommended": False,
        }

        if route_decision.get("intent") == "GREETING":
            # Display labels for suggestion chips.
            # "Top lanes by volume" is intentionally kept as the display label —
            # _canonicalize_user_message() maps it to the canonical prompt at runtime.
            response["suggested_questions"] = [
                "Which shipments are in transit?",
                "Show me delayed shipments",
                "Shipment status distribution",
                "Top lanes by volume",
            ]

        if self._debug:
            response["debug_info"] = {
                "router_decision": {
                    "intent": route_decision.get("intent"),
                    "entity_type": route_decision.get("entity_type"),
                    "entities": route_decision.get("entities") or [],
                    "filters": route_decision.get("filters") or {},
                    "is_follow_up": route_decision.get("is_follow_up"),
                    "is_correction": route_decision.get("is_correction"),
                    "should_call_genie": route_decision.get("should_call_genie"),
                    "reason": route_decision.get("reason"),
                    "enriched_prompt": route_decision.get("enriched_prompt"),
                }
            }

        return response

    def _determine_fetch_row_limit(self) -> int:
        if self._async_export_enabled and self._export_mode == "async_full_query":
            return max(self._table_display_row_limit, 100)
        return max(self._max_download_rows, self._table_display_row_limit)

    def _create_sync_export(self, headers: List[str], rows: List[List]) -> tuple[Optional[int], Optional[str]]:
        download_rows = rows[: self._max_download_rows]
        try:
            download_key = self._audit_service.create_export(
                headers=headers,
                rows=download_rows,
                filename_prefix="genie_export",
            )
            if not download_key:
                return None, None
            return len(download_rows), download_key
        except Exception as exc:  # noqa: BLE001
            logger.warning("GeniePipeline: sync CSV export failed: %s", str(exc)[:100])
            return None, None

    def _resolve_export_mode(self, response: Dict[str, Any]) -> str:
        requested_mode = (self._export_mode or "returned_rows_only").strip().lower()
        if requested_mode != "async_full_query":
            return "returned_rows_only"
        generated_sql = response.get("generated_sql")
        if not generated_sql or self._sql_service is None:
            return "returned_rows_only"
        sanitized = self._sanitize_export_sql(generated_sql)
        return "async_full_query" if sanitized else "returned_rows_only"

    def _start_async_export(
        self,
        *,
        app_conversation_id: str,
        headers: List[str],
        rows: List[List],
        response: Dict[str, Any],
    ) -> Dict[str, Any]:
        mode = self._resolve_export_mode(response)
        job = self._export_job_manager.create_job(
            app_conversation_id=app_conversation_id,
            source="genie",
            mode=mode,
        )
        self._store.update_context(
            app_conversation_id,
            last_export_id=job.export_id,
            last_export_status=job.status,
            last_export_mode=mode,
        )

        worker = threading.Thread(
            target=self._run_export_job,
            kwargs={
                "export_id": job.export_id,
                "app_conversation_id": app_conversation_id,
                "mode": mode,
                "headers": list(headers),
                "rows": list(rows),
                "response": dict(response),
            },
            daemon=True,
        )
        worker.start()
        return {
            "export_id": job.export_id,
            "export_status": "queued",
            "export_mode": mode,
            "download_key": None,
            "export_row_count": None,
        }

    def _run_export_job(
        self,
        *,
        export_id: str,
        app_conversation_id: str,
        mode: str,
        headers: List[str],
        rows: List[List],
        response: Dict[str, Any],
    ) -> None:
        self._export_job_manager.mark_running(export_id)
        self._store.update_context(
            app_conversation_id,
            last_export_id=export_id,
            last_export_status="running",
            last_export_mode=mode,
        )
        try:
            if mode == "async_full_query":
                export_headers, export_rows = self._execute_full_query_export(response)
            else:
                export_headers = headers
                export_rows = rows[: self._max_export_rows]

            download_key = self._audit_service.create_export(
                headers=export_headers,
                rows=export_rows,
                filename_prefix="genie_export",
            )
            if not download_key:
                raise RuntimeError("CSV export file could not be created")
            file_path = self._audit_service.get_export_path(download_key)
            self._export_job_manager.mark_ready(
                export_id,
                file_path=file_path or "",
                download_key=download_key,
                row_count=len(export_rows),
            )
            self._store.update_context(
                app_conversation_id,
                last_download_key=download_key,
                last_export_id=export_id,
                last_export_status="ready",
                last_export_mode=mode,
                last_export_row_count=len(export_rows),
            )
            # Part 1 (Option D): update latest_table_result if it still refers
            # to this export.  This prevents stale-LTR causing "still preparing"
            # on typed-download when the ExportJobManager is already ready.
            # Guard: only update when export_id matches to avoid an older export
            # completing late from overwriting a newer table's LTR.
            try:
                _session = self._store.get_session(app_conversation_id)
                if _session is not None and _session.latest_table_result is not None:
                    _ltr = _session.latest_table_result
                    if _ltr.export_id == export_id:
                        _updated_ltr = TableExportRecord(
                            assistant_message_id=_ltr.assistant_message_id,
                            download_key=download_key,
                            export_id=export_id,
                            export_status="ready",
                            export_mode=mode,
                            export_row_count=len(export_rows),
                            query_description=_ltr.query_description,
                            created_at=_ltr.created_at,
                        )
                        self._store.update_context(
                            app_conversation_id,
                            latest_table_result=_updated_ltr,
                        )
                        logger.debug(
                            "GeniePipeline: latest_table_result updated to ready "
                            "for export_id=%s app_conv=%s rows=%d",
                            export_id, app_conversation_id, len(export_rows),
                        )
            except Exception as _ltr_exc:  # noqa: BLE001
                logger.debug(
                    "GeniePipeline: latest_table_result update skipped (non-fatal): %s",
                    str(_ltr_exc)[:100],
                )
        except Exception as exc:  # noqa: BLE001
            err = str(exc)[:200]
            self._export_job_manager.mark_failed(export_id, err)
            self._store.update_context(
                app_conversation_id,
                last_export_id=export_id,
                last_export_status="failed",
                last_export_mode=mode,
            )
            logger.warning(
                "GeniePipeline: async export failed export_id=%s app_conv=%s: %s",
                export_id,
                app_conversation_id,
                err,
            )

    def _execute_full_query_export(self, response: Dict[str, Any]) -> tuple[List[str], List[List]]:
        generated_sql = response.get("generated_sql") or ""
        safe_sql = self._sanitize_export_sql(generated_sql)
        if not safe_sql:
            raise ValueError("Generated SQL is unavailable or unsafe for export")

        wrapped_sql = (
            "SELECT * FROM (\n"
            f"{safe_sql}\n"
            ") q\n"
            f"LIMIT {self._max_export_rows}"
        )
        result = self._sql_service.execute_query(
            wrapped_sql,
            row_limit=self._max_export_rows,
            timeout_seconds=self._export_query_timeout,
        )
        if result.error:
            raise RuntimeError(result.error)
        return result.headers, result.rows[: self._max_export_rows]

    def _sanitize_export_sql(self, sql: str) -> Optional[str]:
        cleaned = (sql or "").strip()
        if not cleaned:
            return None
        if cleaned.endswith(";"):
            cleaned = cleaned[:-1].rstrip()
        if ";" in cleaned:
            return None
        try:
            self._sql_service._enforce_read_only(cleaned)
        except Exception:
            return None
        if re.search(
            r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|CREATE|ALTER|TRUNCATE)\b",
            cleaned,
            flags=re.IGNORECASE,
        ):
            return None
        if self._export_strip_limit:
            cleaned = re.sub(r"(?is)\s+LIMIT\s+\d+\s*$", "", cleaned).strip()
        return cleaned or None

    def _build_owner_key_error_response(
        self,
        app_conversation_id: str,
        start_time: float,
        execution_time_ms: Optional[int],
    ) -> Dict[str, Any]:
        """Build an error response for owner-key contract violations.

        Unlike _build_error_response, this sets fallback_recommended=False
        because the trusted identity contract is broken and the request
        must not be processed by any pipeline without the owner key.
        """
        elapsed_ms = (
            execution_time_ms
            if execution_time_ms is not None
            else int((time.monotonic() - start_time) * 1000)
        )
        return {
            "status":            "error",
            "message":           _MSG_INVALID_OWNER_KEY,
            "is_table":          False,
            "table_data":        None,
            "row_count":         0,
            "preview_row_count": 0,
            "returned_row_count": 0,
            "total_row_count": None,
            "export_row_count": None,
            "display_row_limit": self._table_display_row_limit,
            "download_key":      None,
            "export_id":         None,
            "export_status":     None,
            "export_mode":       None,
            "execution_time_ms": elapsed_ms,
            "conversation_id":   app_conversation_id,
            "clarification":     None,
            "source":                  "genie",
            "genie_conversation_id":   None,
            "genie_message_id":        None,
            "generated_sql":           None,
            "suggested_questions":     [],
            "has_visualization":       False,
            "visualization":           None,
            "attachment_types":        [],
            "debug_info":              None,
            "fallback_recommended":    False,
        }

    # -------------------------------------------------------------------------
    # PHASE 4C2A: DURABLE SESSION LOOKUP (read-only)
    # -------------------------------------------------------------------------

    def _durable_session_lookup(
        self,
        *,
        owner_key: Optional[str],
        frontend_conversation_id: Optional[str],
        app_conversation_id: str,
    ) -> None:
        """Perform a read-only durable Genie session lookup.

        When the durable runtime bundle is disabled, this is a no-op.
        When enabled, it requires both owner_key and frontend_conversation_id.
        On a successful hit, restores the Genie conversation mapping into the
        in-memory GenieSessionStore so that _run_inner() uses send_message
        instead of start_conversation.

        No durable record is created, bound, updated or deleted.
        """
        # --- Gate 1: Is durable mode enabled? ---
        bundle = getattr(self, "_durable_session_runtime_bundle", None)
        if bundle is None or not getattr(bundle, "enabled", False):
            return  # Disabled path: no access to adapter or repository.

        # --- Gate 2: Prerequisites ---
        if owner_key is None:
            raise _OwnerKeyContractError(_MSG_INVALID_OWNER_KEY)
        if not frontend_conversation_id:
            raise _DurableLookupUnavailableError(
                "frontend_conversation_id is required for durable lookup."
            )

        # --- Gate 3: Access adapter ---
        adapter = getattr(bundle, "adapter", None)
        if adapter is None:
            raise _DurableLookupUnavailableError(
                "Durable adapter is unavailable."
            )

        # --- Gate 4: Build durable key ---
        from app.services.durable_genie_session_adapter import (
            DurableGenieSessionKey,
            DurableGenieSessionUnavailableError,
        )
        from app.services.conversation_repository import ConversationStatus

        try:
            durable_key = DurableGenieSessionKey(
                owner_user_id_hash=owner_key,
                frontend_conversation_id=frontend_conversation_id,
            )
        except (ValueError, TypeError):
            raise _DurableLookupUnavailableError(
                "Invalid durable key components."
            )

        # --- Gate 5: Perform exactly one read-only lookup ---
        try:
            result = adapter.load(durable_key)
        except DurableGenieSessionUnavailableError:
            raise _DurableLookupUnavailableError(
                "Durable session repository is unavailable."
            )
        except Exception:
            raise _DurableLookupUnavailableError(
                "Durable session lookup failed."
            )

        # --- Outcome A: No record found → new-conversation flow ---
        if result is None:
            return

        # --- Outcome A.1: Reject unconfirmed degraded reads ---
        if getattr(result, "degraded", False):
            raise _DurableLookupUnavailableError(
                "Durable session lookup returned an unconfirmed degraded read."
            )

        # --- Outcome A.2: Only recover ACTIVE records ---
        record = result.record
        if record.status != ConversationStatus.ACTIVE:
            return  # Non-active: treat as "not found", new-conversation flow.

        # --- Outcome B: Active record with Genie conversation ID ---
        genie_conversation_id = record.genie_conversation_id
        if not genie_conversation_id:
            return  # Bound but no Genie conversation yet: new-conversation flow.

        # --- Restore in-memory session mapping ---
        self._store.set_genie_conversation_id(
            app_conversation_id, genie_conversation_id
        )
        if record.last_genie_message_id:
            self._store.set_last_message_id(
                app_conversation_id, record.last_genie_message_id
            )

    def _build_durable_lookup_error_response(
        self,
        app_conversation_id: str,
        start_time: float,
        execution_time_ms: Optional[int],
    ) -> Dict[str, Any]:
        """Build error response for durable lookup failures.

        Sets fallback_recommended=False: processing MUST NOT proceed
        through any pipeline when durable state is unavailable.
        """
        elapsed_ms = (
            execution_time_ms
            if execution_time_ms is not None
            else int((time.monotonic() - start_time) * 1000)
        )
        return {
            "status":            "error",
            "message":           _MSG_DURABLE_LOOKUP_UNAVAILABLE,
            "is_table":          False,
            "table_data":        None,
            "row_count":         0,
            "preview_row_count": 0,
            "returned_row_count": 0,
            "total_row_count": None,
            "export_row_count": None,
            "display_row_limit": self._table_display_row_limit,
            "download_key":      None,
            "export_id":         None,
            "export_status":     None,
            "export_mode":       None,
            "execution_time_ms": elapsed_ms,
            "conversation_id":   app_conversation_id,
            "clarification":     None,
            "source":                  "genie",
            "genie_conversation_id":   None,
            "genie_message_id":        None,
            "generated_sql":           None,
            "suggested_questions":     [],
            "has_visualization":       False,
            "visualization":           None,
            "attachment_types":        [],
            "debug_info":              None,
            "fallback_recommended":    False,
        }

    def _build_error_response(
        self,
        app_conversation_id: str,
        start_time: float,
        execution_time_ms: Optional[int],
        user_message: str = _MSG_ERROR,
    ) -> Dict[str, Any]:
        """Build a safe error response.  Never exposes stack traces.

        Always sets ``fallback_recommended=True`` so that the chat route
        (Phase G4/G5) can transparently fall back to the custom pipeline.

        Includes the Genie conversation ID if one was already stored for
        this app conversation (useful for audit logging).
        """
        elapsed_ms = (
            execution_time_ms
            if execution_time_ms is not None
            else int((time.monotonic() - start_time) * 1000)
        )

        # Best-effort: retrieve the Genie conv ID if it was persisted before
        # the error occurred.  Silently falls back to None.
        genie_conv_id: Optional[str] = None
        try:
            genie_conv_id = self._store.get_genie_conversation_id(
                app_conversation_id
            )
        except Exception:  # noqa: BLE001
            pass

        return {
            # --- Existing ChatResponse contract (unchanged) ---
            "status":            "error",
            "message":           user_message,
            "is_table":          False,
            "table_data":        None,
            "row_count":         0,
            "preview_row_count": 0,
            "returned_row_count": 0,
            "total_row_count": None,
            "export_row_count": None,
            "display_row_limit": self._table_display_row_limit,
            "download_key":      None,
            "export_id":         None,
            "export_status":     None,
            "export_mode":       None,
            "execution_time_ms": elapsed_ms,
            "conversation_id":   app_conversation_id,
            "clarification":     None,
            # --- Genie extension fields ---
            "source":                  "genie",
            "genie_conversation_id":   genie_conv_id,
            "genie_message_id":        None,
            "generated_sql":           None,
            "suggested_questions":     [],
            "has_visualization":       False,
            "visualization":           None,
            "attachment_types":        [],
            "debug_info":              None,
            # --- Pipeline-level error signal ---
            "fallback_recommended":    True,
        }
