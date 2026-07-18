"""New accuracy pipeline orchestrator for the TransparencE chatbot.

Connects Phase 2-6 modules into a single end-to-end flow:
  raw input → normalize → understand → sql_template → execute → process → format → summarize → state update

Designed for dependency injection so tests run without DB/LLM.
Does NOT replace live chat.py — will be wired via feature flag in Phase 7B.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

from app.models.canonical_query import CanonicalQuery, QueryFilters, QueryIntent
from app.services.conversation_state import (
    ConversationStateManager,
    ResultMetadata as StateResultMetadata,
)
from app.services.input_normalizer import normalize
from app.services.query_understanding import understand
from app.services.response_formatter import format_response
from app.services.result_processor import ProcessedResult, ResultMetadata, process_query_result
from app.services.result_summarizer import summarize_processed_result
from app.services.sql_template_engine import (
    DEFAULT_TABLE,
    SQLTemplateResult,
    generate_sql_from_canonical,
)

logger = logging.getLogger(__name__)


# =============================================================================
# FEATURE FLAG (not wired into live chat yet)
# =============================================================================

USE_NEW_ACCURACY_PIPELINE = False


# =============================================================================
# PROTOCOLS FOR DEPENDENCY INJECTION
# =============================================================================


class SQLExecutor(Protocol):
    """Protocol for SQL execution dependency."""

    def execute(self, sql: str) -> "SQLExecutionResult":
        ...


class SQLValidator(Protocol):
    """Protocol for SQL validation dependency."""

    def validate(self, sql: str) -> "SQLValidationResult":
        ...


class LLMService(Protocol):
    """Protocol for LLM service dependency."""

    def classify_intent(self, user_input: str) -> str:
        ...

    def generate_sql(self, prompt: str) -> str:
        ...


# =============================================================================
# SUPPORTING DATACLASSES
# =============================================================================


@dataclass
class SQLExecutionResult:
    """Result from SQL execution."""

    rows: List[Dict[str, Any]] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)
    total_row_count: Optional[int] = None
    execution_time_ms: Optional[int] = None
    error: Optional[str] = None


@dataclass
class SQLValidationResult:
    """Result from SQL validation."""

    is_valid: bool = True
    error: Optional[str] = None
    sanitized_sql: Optional[str] = None


# =============================================================================
# PIPELINE INPUT / OUTPUT
# =============================================================================


@dataclass
class ChatPipelineInput:
    """Input to the chat pipeline."""

    user_input: str
    conversation_id: str
    debug: bool = False


@dataclass
class ChatPipelineResult:
    """Full structured result from the chat pipeline."""

    status: str = "success"  # success | error | requires_llm_fallback | no_sql_required
    message: str = ""
    intent: str = ""
    canonical_query: Optional[CanonicalQuery] = None
    sql_used: Optional[str] = None
    sql_generation_source: str = ""  # deterministic_template | llm_fallback | none
    is_table: bool = False
    table_data: Optional[Dict[str, Any]] = None
    row_count: int = 0
    displayed_row_count: int = 0
    summary_basis: str = "none"
    export_key: Optional[str] = None
    requires_clarification: bool = False
    clarification_question: Optional[str] = None
    interpretation_notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    assistant_suggestion: Optional[str] = None
    execution_time_ms: Optional[int] = None
    summary: Optional[str] = None


# =============================================================================
# PIPELINE ORCHESTRATOR
# =============================================================================


class ChatPipeline:
    """End-to-end accuracy pipeline orchestrator.

    Connects Phase 2-6 modules with dependency injection.
    """

    def __init__(
        self,
        sql_executor: Optional[Any] = None,
        sql_validator: Optional[Any] = None,
        llm_service: Optional[Any] = None,
        state_manager: Optional[ConversationStateManager] = None,
        table_name: str = DEFAULT_TABLE,
        export_key_generator: Optional[Callable[[], str]] = None,
    ):
        self.sql_executor = sql_executor
        self.sql_validator = sql_validator
        self.llm_service = llm_service
        self.state_manager = state_manager or ConversationStateManager()
        self.table_name = table_name
        self.export_key_generator = export_key_generator

    def run(self, pipeline_input: ChatPipelineInput) -> ChatPipelineResult:
        """Execute the full pipeline."""
        start_time = time.time()

        try:
            return self._run_internal(pipeline_input, start_time)
        except Exception as e:
            logger.error("Pipeline error: %s", str(e)[:500])
            return ChatPipelineResult(
                status="error",
                message=f"An error occurred processing your request: {str(e)[:200]}",
                intent="ERROR",
            )

    def _run_internal(
        self, pipeline_input: ChatPipelineInput, start_time: float
    ) -> ChatPipelineResult:
        """Internal pipeline execution."""
        conv_id = pipeline_input.conversation_id
        raw_input = pipeline_input.user_input.strip()

        # Step 1: Normalize input
        normalized = normalize(raw_input)

        # Step 2: Detect intent via LLM or meta-intent hints
        intent = self._resolve_intent(normalized, conv_id)

        # Step 3: Handle non-business intents early
        if intent in _NON_BUSINESS_INTENTS:
            return self._handle_non_business_intent(intent, conv_id)

        # Step 4: Get conversation context
        context = self.state_manager.get_conversation_state(conv_id)

        # Step 5: Query understanding
        canonical_query = understand(normalized, intent, context)

        # Step 6: Handle clarification
        if canonical_query.requires_clarification:
            return ChatPipelineResult(
                status="success",
                message=canonical_query.clarification_question or "Could you clarify your request?",
                intent=intent,
                canonical_query=canonical_query,
                requires_clarification=True,
                clarification_question=canonical_query.clarification_question,
                sql_generation_source="none",
            )

        # Step 7: Handle context-dependent intents that don't need SQL
        no_sql_result = self._handle_no_sql_intent(
            intent, canonical_query, conv_id, pipeline_input
        )
        if no_sql_result is not None:
            return no_sql_result

        # Step 8: Generate SQL from canonical query
        template_result = generate_sql_from_canonical(
            canonical_query, table_name=self.table_name
        )

        # Step 9: Handle LLM fallback
        if template_result.requires_llm_fallback:
            return self._handle_llm_fallback(
                canonical_query, intent, pipeline_input, conv_id, start_time
            )

        # Step 10: Validate SQL
        sql = template_result.sql
        if self.sql_validator:
            validation = self.sql_validator.validate(sql)
            if not validation.is_valid:
                return ChatPipelineResult(
                    status="error",
                    message="Generated SQL failed validation.",
                    intent=intent,
                    canonical_query=canonical_query,
                    warnings=[validation.error or "SQL validation failed"],
                    sql_generation_source="deterministic_template",
                )
            if validation.sanitized_sql:
                sql = validation.sanitized_sql

        # Step 11: Execute SQL
        exec_result = self._execute_sql(sql)
        if exec_result.error:
            return ChatPipelineResult(
                status="error",
                message=f"Query execution failed: {exec_result.error[:200]}",
                intent=intent,
                canonical_query=canonical_query,
                sql_used=sql,
                sql_generation_source="deterministic_template",
            )

        # Step 12: Generate export key
        export_key = None
        if self.export_key_generator and exec_result.rows:
            export_key = self.export_key_generator()

        # Step 13: Process result
        processed = process_query_result(
            rows=exec_result.rows,
            columns=exec_result.columns,
            canonical_query=canonical_query,
            sql_used=sql,
            template_result=template_result,
            total_row_count=exec_result.total_row_count,
            export_key=export_key,
            execution_time_ms=exec_result.execution_time_ms,
        )

        # Step 14: Format response
        response = format_response(processed, canonical_query)

        # Step 15: Summarize
        summary = summarize_processed_result(processed, canonical_query)

        # Step 16: Update conversation state
        elapsed_ms = int((time.time() - start_time) * 1000)
        # Convert Phase 5 ResultMetadata to Phase 3 StateResultMetadata
        state_meta = StateResultMetadata(
            total_row_count=processed.metadata.total_matching_rows,
            displayed_rows=processed.metadata.displayed_row_count,
            columns=processed.metadata.columns_returned,
            has_aggregates=(processed.metadata.result_type == "aggregate"),
            is_empty=processed.is_empty,
            execution_time_ms=processed.metadata.execution_time_ms,
        )
        self.state_manager.update_after_query(
            conv_id,
            user_input=raw_input,
            normalized_input=normalized.cleaned,
            canonical_query=canonical_query,
            validated_sql=sql,
            result_metadata=state_meta,
            displayed_rows=processed.display_rows[:5],
            total_row_count=processed.metadata.total_matching_rows,
            download_key=export_key,
            assistant_suggestion=processed.metadata.assistant_suggestion,
            result_summary=summary,
        )

        return ChatPipelineResult(
            status="success",
            message=response["message"],
            intent=intent,
            canonical_query=canonical_query,
            sql_used=sql,
            sql_generation_source="deterministic_template",
            is_table=response["is_table"],
            table_data=response.get("table_data"),
            row_count=response["row_count"],
            displayed_row_count=response["displayed_row_count"],
            summary_basis=response["summary_basis"],
            export_key=response.get("export_key"),
            interpretation_notes=response.get("interpretation_notes", []),
            warnings=response.get("warnings", []),
            assistant_suggestion=response.get("assistant_suggestion"),
            execution_time_ms=elapsed_ms,
            summary=summary,
        )

    # =========================================================================
    # INTENT RESOLUTION
    # =========================================================================

    def _resolve_intent(self, normalized, conv_id: str = "") -> str:
        """Resolve intent from normalized input.

        Priority order:
        1. Meta-intent hints (summarize, download, broaden)
        2. Direct lookup (shipment IDs)
        3. Deterministic follow-up detection (Phase 7E)
        4. LLM-based classification (when available)
        5. Default entity-based heuristic
        """
        entities = normalized.entities

        # Meta-intent shortcuts — but only honor them if the input does NOT
        # also contain query entities (e.g., "summary of shipments from Thailand"
        # has entity source_country=TH, so it's a SHIPMENT_QUERY, not SUMMARIZE)
        if entities.meta_intent_hint:
            has_query_entities = (
                entities.source_country
                or entities.destination_country
                or entities.transport_mode
                or entities.status_category
                or entities.business_unit
                or entities.shipment_ids
            )
            if has_query_entities:
                # Input has both a meta-hint AND real entities — treat as query
                pass  # fall through to entity-based classification below
            else:
                return _META_INTENT_MAP.get(
                    entities.meta_intent_hint, "SHIPMENT_QUERY"
                )

        # Check for shipment IDs -> direct lookup
        if entities.shipment_ids:
            return "DIRECT_LOOKUP"

        # Phase 7E: Deterministic follow-up detection (runs BEFORE LLM)
        # Only trigger if conversation context exists (previous query made)
        has_context = bool(self.state_manager.get_conversation_state(conv_id))
        if has_context and self._is_deterministic_follow_up(normalized):
            return QueryIntent.FOLLOW_UP_FILTER

        # LLM-based intent classification (if available)
        if self.llm_service and hasattr(self.llm_service, "classify_intent"):
            try:
                return self.llm_service.classify_intent(normalized.cleaned)
            except Exception:
                logger.warning("LLM intent classification failed, defaulting")

        # Default: if entities extracted, it's a shipment query
        if (
            entities.source_country
            or entities.destination_country
            or entities.transport_mode
            or entities.status_category
            or entities.date_phrases
            or entities.business_unit
        ):
            return "SHIPMENT_QUERY"

        return "SHIPMENT_QUERY"  # Generous default

    def _is_deterministic_follow_up(self, normalized) -> bool:
        """Phase 7E: Detect follow-up intent deterministically.

        A message is a follow-up when ALL of these are true:
        1. Conversation context exists (there was a previous query)
        2. The message does NOT introduce a new source country (no 'from X')
        3. The message modifies the previous query (adds status, mode, or destination)

        This catches patterns like:
        - 'which are in transit?' (adds status filter)
        - 'what about ocean?' (changes mode)
        - 'which of them are to Czech?' (adds destination)
        - 'only air' (changes mode)
        """
        entities = normalized.entities
        cleaned = normalized.cleaned.lower().strip()

        # Requirement 1: Conversation context must exist
        # We check if state_manager has a previous query for ANY conversation
        # (actual conv_id check happens in run() — here we just verify the
        # input looks like a follow-up vs a new query)

        # Requirement 2: No source country (no "from X")
        # If the user says "from US", it's a new query, not a follow-up
        if entities.source_country:
            return False

        # Requirement 3a: Check explicit follow-up phrase patterns
        for phrase in _FOLLOW_UP_PHRASES:
            if phrase in cleaned:
                return True

        # Requirement 3b: Short contextual query with modifying entities
        # (destination-only, status-only, or mode-only without source)
        word_count = len(cleaned.split())
        has_modifier = (
            entities.status_category
            or entities.transport_mode
            or entities.destination_country
        )

        if has_modifier and word_count <= 8:
            return True

        # Requirement 3c: Destination-only pattern ("to Czech?", "to Germany")
        if entities.destination_country and not entities.transport_mode and not entities.status_category:
            if word_count <= 6:
                return True

        return False

    # =========================================================================
    # NON-BUSINESS INTENT HANDLING
    # =========================================================================

    def _handle_non_business_intent(
        self, intent: str, conv_id: str
    ) -> ChatPipelineResult:
        """Handle greetings, exits, abusive, off-topic without touching state."""
        messages = {
            "GREETING": "Hello! I can help you search shipments, check status, and analyze logistics data. What would you like to know?",
            "EXIT": "Goodbye! Feel free to come back anytime.",
            "ABUSIVE_OR_INAPPROPRIATE": "I\'m here to help with shipment queries. Please keep the conversation professional.",
            "OFF_TOPIC": "I specialize in shipment tracking and logistics queries. Could you ask something related to shipments?",
        }
        return ChatPipelineResult(
            status="no_sql_required",
            message=messages.get(intent, "How can I help with shipments?"),
            intent=intent,
            sql_generation_source="none",
        )

    # =========================================================================
    # NO-SQL INTENTS (use stored state)
    # =========================================================================

    def _handle_no_sql_intent(
        self, intent: str, cq: CanonicalQuery, conv_id: str, pinput: ChatPipelineInput
    ) -> Optional[ChatPipelineResult]:
        """Handle intents that don\'t require new SQL execution."""
        ctx = self.state_manager.get_or_create_context(conv_id)

        if intent == "SUMMARIZE_LAST_RESULT":
            if ctx.last_result_metadata:
                # Use stored summary if available
                if ctx.last_result_summary:
                    summary = ctx.last_result_summary
                else:
                    # Build a minimal ProcessedResult from Phase 3 state metadata
                    p5_meta = ResultMetadata(
                        total_matching_rows=ctx.last_result_metadata.total_row_count,
                        displayed_row_count=ctx.last_result_metadata.displayed_rows,
                        columns_returned=ctx.last_result_metadata.columns,
                        result_type="aggregate" if ctx.last_result_metadata.has_aggregates else "detail",
                        summary_basis="preview_only" if ctx.last_result_metadata.displayed_rows < ctx.last_result_metadata.total_row_count else "full_result",
                        is_empty=ctx.last_result_metadata.is_empty,
                    )
                    stored_processed = ProcessedResult(
                        metadata=p5_meta,
                        display_rows=ctx.last_displayed_rows or [],
                        table_headers=ctx.last_result_metadata.columns,
                        is_empty=ctx.last_result_metadata.is_empty,
                    )
                    summary = summarize_processed_result(stored_processed, cq)

                return ChatPipelineResult(
                    status="success",
                    message=summary,
                    intent=intent,
                    canonical_query=cq,
                    sql_generation_source="none",
                    row_count=ctx.last_result_metadata.total_row_count,
                    displayed_row_count=ctx.last_result_metadata.displayed_rows,
                    summary_basis="preview_only" if ctx.last_result_metadata.displayed_rows < ctx.last_result_metadata.total_row_count else "full_result",
                    summary=summary,
                )
            # No previous result
            return ChatPipelineResult(
                status="success",
                message="There\'s no previous result to summarize. Please run a query first.",
                intent=intent,
                requires_clarification=True,
                clarification_question="Please run a query first before asking for a summary.",
                sql_generation_source="none",
            )

        if intent == "DOWNLOAD_LAST_RESULT":
            download_key = ctx.last_download_key
            if download_key:
                return ChatPipelineResult(
                    status="success",
                    message=f"Your export is ready. Download key: {download_key}",
                    intent=intent,
                    canonical_query=cq,
                    export_key=download_key,
                    sql_generation_source="none",
                )
            return ChatPipelineResult(
                status="success",
                message="No previous result available for download. Please run a query first.",
                intent=intent,
                requires_clarification=True,
                sql_generation_source="none",
            )

        # Other intents (FOLLOW_UP_FILTER, BROADEN, SHIPMENT_QUERY, DIRECT_LOOKUP)
        # all need SQL execution — return None to continue pipeline
        return None

    # =========================================================================
    # LLM FALLBACK
    # =========================================================================

    def _handle_llm_fallback(
        self,
        cq: CanonicalQuery,
        intent: str,
        pinput: ChatPipelineInput,
        conv_id: str,
        start_time: float,
    ) -> ChatPipelineResult:
        """Handle queries that need LLM-generated SQL."""
        if self.llm_service and hasattr(self.llm_service, "generate_sql"):
            try:
                sql = self.llm_service.generate_sql(pinput.user_input)
                if sql:
                    exec_result = self._execute_sql(sql)
                    if not exec_result.error:
                        processed = process_query_result(
                            rows=exec_result.rows,
                            columns=exec_result.columns,
                            canonical_query=cq,
                            sql_used=sql,
                            total_row_count=exec_result.total_row_count,
                            execution_time_ms=exec_result.execution_time_ms,
                        )
                        response = format_response(processed, cq)
                        summary = summarize_processed_result(processed, cq)
                        elapsed_ms = int((time.time() - start_time) * 1000)
                        return ChatPipelineResult(
                            status="success",
                            message=response["message"],
                            intent=intent,
                            canonical_query=cq,
                            sql_used=sql,
                            sql_generation_source="llm_fallback",
                            is_table=response["is_table"],
                            table_data=response.get("table_data"),
                            row_count=response["row_count"],
                            displayed_row_count=response["displayed_row_count"],
                            summary_basis=response["summary_basis"],
                            interpretation_notes=response.get("interpretation_notes", []),
                            warnings=response.get("warnings", []),
                            execution_time_ms=elapsed_ms,
                            summary=summary,
                        )
            except Exception as e:
                logger.warning("LLM SQL generation failed: %s", str(e)[:200])

        # No LLM available or LLM failed
        return ChatPipelineResult(
            status="requires_llm_fallback",
            message="This query requires advanced SQL generation which is not available in deterministic mode.",
            intent=intent,
            canonical_query=cq,
            sql_generation_source="none",
        )

    # =========================================================================
    # SQL EXECUTION
    # =========================================================================

    def _execute_sql(self, sql: str) -> SQLExecutionResult:
        """Execute SQL using the injected executor."""
        if not self.sql_executor:
            return SQLExecutionResult(
                error="No SQL executor configured."
            )
        try:
            return self.sql_executor.execute(sql)
        except Exception as e:
            return SQLExecutionResult(error=str(e)[:500])


# =============================================================================
# CONSTANTS
# =============================================================================

_NON_BUSINESS_INTENTS = frozenset({
    "GREETING", "EXIT", "ABUSIVE_OR_INAPPROPRIATE", "OFF_TOPIC",
})

_META_INTENT_MAP = {
    "summarize": "SUMMARIZE_LAST_RESULT",
    "download": "DOWNLOAD_LAST_RESULT",
    "broaden": "BROADEN_PREVIOUS_DATE_RANGE",
}

# Phase 7E: Deterministic follow-up phrase patterns
# Trigger FOLLOW_UP_FILTER when conversation context exists.
_FOLLOW_UP_PHRASES = frozenset({
    "which are in transit",
    "which are completed",
    "which are delayed",
    "which ones are in transit",
    "which ones are completed",
    "which ones are delayed",
    "show only delayed",
    "show only in transit",
    "show only completed",
    "delayed ones",
    "in transit ones",
    "completed ones",
    "what about ocean",
    "what about air",
    "what about sea",
    "what about rail",
    "same for air",
    "same for ocean",
    "same for sea",
    "only ocean",
    "only air",
    "only sea",
    "via ocean",
    "via air",
    "via sea",
})
