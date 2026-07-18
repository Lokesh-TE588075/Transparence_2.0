"""Structured business context tracking for the TransparencE chatbot.

Separate from the raw chat transcript (ConversationManager). This module
maintains per-session business state that supports:
- Follow-up resolution (merging new filters with previous context)
- Summarize/download/broaden intent resolution
- Off-topic/abusive message protection (never overwrites business state)
- Session TTL expiry
- Session reset

Designed for in-memory operation now, compatible with future Delta persistence.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from app.models.canonical_query import (
    CanonicalQuery,
    ConversationState,
    DateRange,
    QueryFilters,
    QueryIntent,
)

logger = logging.getLogger(__name__)

# Intents that should NOT update business context
_NON_BUSINESS_INTENTS = frozenset({
    QueryIntent.GREETING,
    QueryIntent.EXIT,
    QueryIntent.ABUSIVE_OR_INAPPROPRIATE,
    QueryIntent.OFF_TOPIC,
})

# Default session TTL: 24 hours
DEFAULT_SESSION_TTL_SECONDS = 24 * 60 * 60


# =============================================================================
# BUSINESS CONTEXT MODEL
# =============================================================================


@dataclass
class ResultMetadata:
    """Metadata about the last successful query result."""

    total_row_count: int = 0
    displayed_rows: int = 0
    columns: List[str] = field(default_factory=list)
    has_aggregates: bool = False
    is_empty: bool = True
    execution_time_ms: Optional[int] = None


@dataclass
class BusinessContext:
    """Structured business state for a conversation session.

    This is the authoritative source for follow-up resolution.
    Off-topic, abusive, greeting, and exit messages never modify this.
    """

    # Input tracking
    last_user_input: Optional[str] = None
    last_normalized_input: Optional[str] = None

    # Query tracking
    last_canonical_query: Optional[CanonicalQuery] = None
    last_validated_sql: Optional[str] = None

    # Result tracking
    last_result_metadata: Optional[ResultMetadata] = None
    last_displayed_rows: Optional[List[Dict[str, Any]]] = None
    last_total_row_count: Optional[int] = None
    last_download_key: Optional[str] = None

    # Filter tracking (denormalized for fast access)
    last_filters: Optional[QueryFilters] = None

    # Response tracking
    last_assistant_suggestion: Optional[str] = None
    last_result_summary: Optional[str] = None

    # Timing
    last_updated_at: float = 0.0  # time.time() epoch
    query_count: int = 0


# =============================================================================
# CONVERSATION STATE MANAGER
# =============================================================================


class ConversationStateManager:
    """Manages structured business context per conversation session.

    Separate from ConversationManager (raw transcript in Delta).
    This tracks business state for follow-up resolution.

    Thread-safe for single-session use (one conversation at a time per worker).
    For multi-worker deployment, each worker holds its own in-memory state.
    Future: serialize to Delta for cross-worker persistence.
    """

    def __init__(self, session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS):
        self._sessions: Dict[str, BusinessContext] = {}
        self._session_ttl = session_ttl_seconds

    # -------------------------------------------------------------------------
    # SESSION LIFECYCLE
    # -------------------------------------------------------------------------

    def get_context(self, conversation_id: str) -> Optional[BusinessContext]:
        """Get business context for a conversation, or None if expired/missing."""
        ctx = self._sessions.get(conversation_id)
        if ctx is None:
            return None
        if self._is_expired(ctx):
            self._sessions.pop(conversation_id, None)
            logger.info("Session expired for conversation %s", conversation_id)
            return None
        return ctx

    def get_or_create_context(self, conversation_id: str) -> BusinessContext:
        """Get existing context or create a new empty one."""
        ctx = self.get_context(conversation_id)
        if ctx is None:
            ctx = BusinessContext(last_updated_at=time.time())
            self._sessions[conversation_id] = ctx
        return ctx

    def reset_context(self, conversation_id: str) -> None:
        """Clear all structured business context for a session."""
        self._sessions.pop(conversation_id, None)
        logger.info("Business context reset for conversation %s", conversation_id)

    def _is_expired(self, ctx: BusinessContext) -> bool:
        """Check if a session has exceeded its TTL."""
        if ctx.last_updated_at == 0.0:
            return False
        return (time.time() - ctx.last_updated_at) > self._session_ttl

    # -------------------------------------------------------------------------
    # CONTEXT UPDATE (guards against non-business intents)
    # -------------------------------------------------------------------------

    def update_after_query(
        self,
        conversation_id: str,
        *,
        user_input: str,
        normalized_input: str,
        canonical_query: CanonicalQuery,
        validated_sql: Optional[str] = None,
        result_metadata: Optional[ResultMetadata] = None,
        displayed_rows: Optional[List[Dict[str, Any]]] = None,
        total_row_count: Optional[int] = None,
        download_key: Optional[str] = None,
        assistant_suggestion: Optional[str] = None,
        result_summary: Optional[str] = None,
    ) -> None:
        """Update business context after a successful query.

        CRITICAL: Only call this for business-relevant intents.
        Off-topic, abusive, greeting, and exit messages must NOT call this.
        """
        intent = canonical_query.intent

        # Guard: non-business intents never update structured context
        if intent in _NON_BUSINESS_INTENTS:
            logger.debug(
                "Skipping business context update for non-business intent: %s",
                intent,
            )
            return

        ctx = self.get_or_create_context(conversation_id)

        # Update input tracking
        ctx.last_user_input = user_input
        ctx.last_normalized_input = normalized_input

        # Update query tracking
        ctx.last_canonical_query = canonical_query
        if validated_sql is not None:
            ctx.last_validated_sql = validated_sql

        # Update result tracking
        if result_metadata is not None:
            ctx.last_result_metadata = result_metadata
            ctx.last_total_row_count = result_metadata.total_row_count
        if total_row_count is not None:
            ctx.last_total_row_count = total_row_count
        if displayed_rows is not None:
            ctx.last_displayed_rows = displayed_rows
        if download_key is not None:
            ctx.last_download_key = download_key

        # Update filter tracking (denormalize from canonical query)
        if canonical_query.filters:
            ctx.last_filters = canonical_query.filters

        # Update response tracking
        if assistant_suggestion is not None:
            ctx.last_assistant_suggestion = assistant_suggestion
        if result_summary is not None:
            ctx.last_result_summary = result_summary

        # Timing
        ctx.last_updated_at = time.time()
        ctx.query_count += 1

    # -------------------------------------------------------------------------
    # CONTEXT RETRIEVAL (for follow-up resolution)
    # -------------------------------------------------------------------------

    def get_conversation_state(self, conversation_id: str) -> Optional[ConversationState]:
        """Convert BusinessContext to ConversationState for query_understanding.

        Returns None if no context exists (triggers clarification in Phase 2 modules).
        """
        ctx = self.get_context(conversation_id)
        if ctx is None:
            return None

        # Only return state if there's meaningful business context
        if ctx.last_canonical_query is None and ctx.last_total_row_count is None:
            return None

        # Extract date range from last filters if available
        last_date_range = None
        if ctx.last_filters and ctx.last_filters.date_range:
            last_date_range = ctx.last_filters.date_range

        return ConversationState(
            last_canonical_query=ctx.last_canonical_query,
            last_result_row_count=ctx.last_total_row_count,
            last_result_headers=(
                ctx.last_result_metadata.columns
                if ctx.last_result_metadata
                else None
            ),
            last_download_key=ctx.last_download_key,
            last_assistant_suggestion=ctx.last_assistant_suggestion,
            last_date_range=last_date_range,
            last_filters=ctx.last_filters,
        )

    def get_last_filters(self, conversation_id: str) -> Optional[QueryFilters]:
        """Get last filters for follow-up merging."""
        ctx = self.get_context(conversation_id)
        if ctx is None:
            return None
        return ctx.last_filters

    def get_last_displayed_rows(self, conversation_id: str) -> Optional[List[Dict[str, Any]]]:
        """Get last displayed rows (for 'details of first shipment' resolution)."""
        ctx = self.get_context(conversation_id)
        if ctx is None:
            return None
        return ctx.last_displayed_rows

    def get_last_download_key(self, conversation_id: str) -> Optional[str]:
        """Get last download key (for DOWNLOAD_LAST_RESULT)."""
        ctx = self.get_context(conversation_id)
        if ctx is None:
            return None
        return ctx.last_download_key

    def get_last_result_metadata(self, conversation_id: str) -> Optional[ResultMetadata]:
        """Get last result metadata (for SUMMARIZE_LAST_RESULT)."""
        ctx = self.get_context(conversation_id)
        if ctx is None:
            return None
        return ctx.last_result_metadata

    def get_last_assistant_suggestion(self, conversation_id: str) -> Optional[str]:
        """Get last assistant suggestion (for BROADEN resolution)."""
        ctx = self.get_context(conversation_id)
        if ctx is None:
            return None
        return ctx.last_assistant_suggestion

    # -------------------------------------------------------------------------
    # FOLLOW-UP RESOLUTION HELPERS
    # -------------------------------------------------------------------------

    def resolve_row_reference(
        self, conversation_id: str, ordinal: int = 0
    ) -> Optional[Dict[str, Any]]:
        """Resolve 'first shipment', 'second one', etc. from displayed rows.

        Args:
            conversation_id: The conversation session.
            ordinal: 0-based index (0 = first, 1 = second, etc.)

        Returns:
            The row dict if available, None otherwise.
        """
        rows = self.get_last_displayed_rows(conversation_id)
        if rows is None or ordinal >= len(rows):
            return None
        return rows[ordinal]

    def can_broaden(self, conversation_id: str) -> bool:
        """Check if broadening is possible (requires previous date range)."""
        state = self.get_conversation_state(conversation_id)
        if state is None:
            return False
        return state.last_date_range is not None

    def can_summarize(self, conversation_id: str) -> bool:
        """Check if summarization is possible (requires previous result)."""
        ctx = self.get_context(conversation_id)
        if ctx is None:
            return False
        return ctx.last_total_row_count is not None and ctx.last_total_row_count > 0

    def can_download(self, conversation_id: str) -> bool:
        """Check if download is possible (requires download key)."""
        ctx = self.get_context(conversation_id)
        if ctx is None:
            return False
        return ctx.last_download_key is not None

    # -------------------------------------------------------------------------
    # SERIALIZATION (for future Delta persistence)
    # -------------------------------------------------------------------------

    def serialize_context(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """Serialize business context to dict (for future Delta storage)."""
        ctx = self.get_context(conversation_id)
        if ctx is None:
            return None
        return asdict(ctx)

    @property
    def active_session_count(self) -> int:
        """Number of active (non-expired) sessions."""
        # Lazy cleanup: don't iterate all sessions unless asked
        return len(self._sessions)
