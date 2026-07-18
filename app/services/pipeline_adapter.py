"""Pipeline adapter: bridges real app services to ChatPipeline's DI protocols.

This module wraps existing sql_service, sql_validator, and llm_service
into the protocol interfaces expected by ChatPipeline, without duplicating
any existing logic.

Critical adaptations:
- SQLService.execute_query() returns QueryResult with rows as List[List].
  The adapter converts to List[Dict] (keyed by headers) for process_query_result().
- LLMService.classify_intent() returns IntentClassification with Intent enum (5 values).
  The adapter normalizes to the 11-intent string labels used by the new pipeline.

Also provides:
- map_pipeline_result_to_response: converts ChatPipelineResult to ChatResponse dict
- should_use_new_pipeline: routing decision helper (testable without FastAPI)
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from app.services.chat_pipeline import (
    ChatPipeline,
    ChatPipelineInput,
    ChatPipelineResult,
    SQLExecutionResult,
    SQLValidationResult,
)

logger = logging.getLogger(__name__)


# =============================================================================
# INTENT NORMALIZATION MAP
# =============================================================================

# Maps live LLM Intent enum .value strings (lowercase) to new pipeline intent labels
_LIVE_INTENT_TO_PIPELINE_INTENT = {
    "shipment_query": "SHIPMENT_QUERY",
    "greeting": "GREETING",
    "clarification_needed": "CLARIFICATION_NEEDED",
    "off_topic": "OFF_TOPIC",
    "follow_up": "FOLLOW_UP_FILTER",
    # Extended mappings (if LLM is updated or patterns detected elsewhere):
    "exit": "EXIT",
    "abusive": "ABUSIVE_OR_INAPPROPRIATE",
    "download": "DOWNLOAD_LAST_RESULT",
    "summarize": "SUMMARIZE_LAST_RESULT",
    "broaden": "BROADEN_PREVIOUS_DATE_RANGE",
    "direct_lookup": "DIRECT_LOOKUP",
}


# =============================================================================
# SERVICE ADAPTERS
# =============================================================================


class SQLServiceAdapter:
    """Wraps existing SQLService into ChatPipeline's SQLExecutor protocol.

    Key conversion: SQLService returns QueryResult with rows as List[List].
    ChatPipeline expects rows as List[Dict] (keyed by column headers).
    """

    def __init__(self, sql_service):
        self._sql_service = sql_service

    def execute(self, sql: str) -> SQLExecutionResult:
        """Execute SQL via the real SQLService."""
        try:
            result = self._sql_service.execute_query(sql)

            # Handle error
            if result.error:
                return SQLExecutionResult(error=result.error)

            # Convert List[List] rows → List[Dict] using headers
            headers = result.headers or []
            dict_rows = []
            if result.rows and headers:
                for row in result.rows:
                    dict_rows.append(dict(zip(headers, row)))

            return SQLExecutionResult(
                rows=dict_rows,
                columns=headers,
                total_row_count=result.total_row_count,
                execution_time_ms=result.execution_time_ms,
            )

        except Exception as e:
            return SQLExecutionResult(error=str(e)[:500])


class SQLValidatorAdapter:
    """Wraps existing SQLValidator into ChatPipeline's SQLValidator protocol."""

    def __init__(self, validator):
        self._validator = validator

    def validate(self, sql: str) -> SQLValidationResult:
        """Validate SQL via the real SQLValidator."""
        try:
            result = self._validator.validate(sql)
            if hasattr(result, "is_valid"):
                return SQLValidationResult(
                    is_valid=result.is_valid,
                    error=getattr(result, "error", None),
                    sanitized_sql=getattr(result, "sanitized_sql", sql) if result.is_valid else None,
                )
            # If validator returns bool
            if isinstance(result, bool):
                return SQLValidationResult(is_valid=result)
            # Default: pass through
            return SQLValidationResult(is_valid=True, sanitized_sql=sql)
        except Exception as e:
            return SQLValidationResult(is_valid=False, error=str(e)[:200])


class LLMServiceAdapter:
    """Wraps existing LLMService into ChatPipeline's LLMService protocol.

    Key conversion: Live LLM has 5 intents. New pipeline uses 11 intent labels.
    Normalization map handles the translation.
    """

    def __init__(self, llm_service):
        self._llm = llm_service

    def classify_intent(self, user_input: str) -> str:
        """Classify intent via the real LLMService, normalized to pipeline labels."""
        try:
            result = self._llm.classify_intent(user_input)
            # result is IntentClassification with .intent (Intent enum)
            if hasattr(result, "intent"):
                raw_value = result.intent.value if hasattr(result.intent, "value") else str(result.intent)
            else:
                raw_value = str(result)

            # Normalize to pipeline intent label
            normalized = _LIVE_INTENT_TO_PIPELINE_INTENT.get(
                raw_value.lower(), "SHIPMENT_QUERY"
            )
            return normalized

        except Exception as e:
            logger.warning("LLM intent classification failed: %s", str(e)[:100])
            return "SHIPMENT_QUERY"

    def generate_sql(self, prompt: str) -> str:
        """Generate SQL via the real LLMService (for LLM fallback path)."""
        try:
            from app.business_rules.prompt_builder import build_system_prompt
            from app.config import settings
            system_prompt = build_system_prompt(settings.SHIPMENT_TABLE_NAME)
            result = self._llm.generate_sql(
                user_message=prompt,
                system_prompt=system_prompt,
            )
            if hasattr(result, "sql"):
                return result.sql
            return str(result)
        except Exception as e:
            logger.warning("LLM SQL generation failed in adapter: %s", str(e)[:200])
            return ""


# =============================================================================
# EXPORT KEY GENERATOR
# =============================================================================


def generate_export_key() -> str:
    """Generate a unique export key for result downloads."""
    return f"export-{uuid.uuid4().hex[:12]}"


# =============================================================================
# RESULT MAPPING (ChatPipelineResult → frontend response dict)
# =============================================================================


def map_pipeline_result_to_response(
    pipeline_result: ChatPipelineResult,
    conversation_id: str,
    execution_time_ms: int,
) -> Dict[str, Any]:
    """Map ChatPipelineResult to the frontend ChatResponse contract.

    This is a pure function, easily testable without FastAPI.

    Returns a dict matching ChatResponse fields:
        status, message, is_table, table_data, row_count,
        download_key, execution_time_ms, conversation_id, clarification
    """
    # Map pipeline status to frontend status
    status = _map_status(pipeline_result)

    response = {
        "status": status,
        "message": pipeline_result.message,
        "is_table": pipeline_result.is_table,
        "table_data": None,
        "row_count": pipeline_result.row_count,
        "download_key": pipeline_result.export_key,
        "execution_time_ms": execution_time_ms,
        "conversation_id": conversation_id,
        "clarification": None,
    }

    # Map table data — convert List[Dict] rows to List[List] for TableData schema
    if pipeline_result.is_table and pipeline_result.table_data:
        headers = pipeline_result.table_data.get("headers", [])
        raw_rows = pipeline_result.table_data.get("rows", [])

        # Convert rows: if they're dicts, extract values in header order
        list_rows = []
        for row in raw_rows:
            if isinstance(row, dict):
                list_rows.append([row.get(h) for h in headers])
            elif isinstance(row, (list, tuple)):
                list_rows.append(list(row))
            else:
                list_rows.append([row])

        response["table_data"] = {
            "headers": headers,
            "rows": list_rows,
        }

    # Map clarification
    if pipeline_result.requires_clarification:
        response["status"] = "clarification"
        response["clarification"] = pipeline_result.clarification_question or pipeline_result.message

    return response


def _map_status(result: ChatPipelineResult) -> str:
    """Map ChatPipelineResult status to frontend-expected status string."""
    mapping = {
        "success": "success",
        "error": "error",
        "no_sql_required": "greeting",
        "requires_llm_fallback": "error",
    }
    status = mapping.get(result.status, "success")

    # Refine for non-business intents
    if result.status == "no_sql_required":
        if result.intent == "OFF_TOPIC":
            return "off_topic"
        if result.intent in ("GREETING", "EXIT"):
            return "greeting"

    return status


# =============================================================================
# ROUTING DECISION HELPER
# =============================================================================


def should_use_new_pipeline(use_flag: bool) -> bool:
    """Determine whether to route through the new pipeline.

    Pure function for testability.
    """
    return use_flag


# =============================================================================
# STATE MANAGER FACTORY INTEGRATION (Phase 8)
# =============================================================================


def create_state_manager(sql_service=None):
    """Create conversation state manager using factory.

    The factory creates its own DeltaSQLExecutor (direct SDK, bypasses
    chat SQL validator) when Delta is enabled. The sql_service param is
    accepted for API compatibility but not used for Delta operations.

    Returns:
        ConversationStateManager (Delta-backed or in-memory).
    """
    from app.services.conversation_state_factory import get_conversation_state_manager

    # Let the factory create its own DeltaSQLExecutor (direct SDK).
    # Do NOT pass SQLServiceAdapter — it blocks DDL/DML via chat validator.
    return get_conversation_state_manager(sql_executor=None)
