"""Factory for conversation state managers.

Returns either:
- DeltaConversationStateManager (when USE_DELTA_CONVERSATION_STATE=true)
- ConversationStateManager (in-memory, default)

If Delta initialization fails, falls back to in-memory silently.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.services.conversation_state import ConversationStateManager

logger = logging.getLogger(__name__)


def get_conversation_state_manager(
    sql_executor=None,
) -> ConversationStateManager:
    """Create the appropriate conversation state manager.

    Uses app config to determine whether to use Delta persistence.
    Falls back to in-memory if Delta initialization fails.

    Args:
        sql_executor: Optional SQL executor for Delta operations.
            If None and Delta is requested, falls back to in-memory.

    Returns:
        A ConversationStateManager (either Delta-backed or in-memory).
    """
    try:
        from app.config import settings

        if not settings.USE_DELTA_CONVERSATION_STATE:
            logger.debug("Delta conversation state disabled (USE_DELTA_CONVERSATION_STATE=false)")
            return ConversationStateManager(
                session_ttl_seconds=settings.CONVERSATION_STATE_TTL_HOURS * 3600
            )

        # Delta requested — try to initialize
        from app.services.delta_conversation_state import (
            DeltaConversationStateManager,
            DeltaSQLExecutor,
        )

        # Use provided executor or create a DeltaSQLExecutor (direct SDK)
        executor = sql_executor
        if executor is None:
            try:
                executor = DeltaSQLExecutor()
            except Exception as e:
                logger.warning(
                    "Could not create DeltaSQLExecutor, falling back to in-memory: %s",
                    str(e)[:200],
                )
                return ConversationStateManager(
                    session_ttl_seconds=settings.CONVERSATION_STATE_TTL_HOURS * 3600
                )

        manager = DeltaConversationStateManager(
            table_name=settings.CONVERSATION_STATE_TABLE_NAME,
            ttl_hours=settings.CONVERSATION_STATE_TTL_HOURS,
            sql_executor=executor,
        )

        if manager.is_delta_available:
            logger.info("Using Delta-backed conversation state: %s", settings.CONVERSATION_STATE_TABLE_NAME)
        else:
            logger.warning(
                "Delta table init failed, falling back to in-memory state. "
                "Table: %s", settings.CONVERSATION_STATE_TABLE_NAME
            )

        return manager

    except Exception as e:
        logger.warning("State manager factory error, using in-memory: %s", str(e)[:200])
        return ConversationStateManager()
