"""Phase 8 Live Smoke Test: Delta state persistence (optional).

Only runs when RUN_DELTA_STATE_SMOKE=true is set.
Requires a real Databricks SQL Warehouse connection.
"""

import sys
import os
import time
import pytest

sys.path.insert(0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app")

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DELTA_STATE_SMOKE", "false").lower() != "true",
    reason="Live Delta smoke test disabled (set RUN_DELTA_STATE_SMOKE=true to enable)",
)


class TestDeltaStateLiveSmoke:
    """Live integration tests against real Delta table."""

    def test_save_and_read_context(self):
        """Save a conversation context to Delta and read it back."""
        from app.config import settings
        from app.services.delta_conversation_state import (
            DeltaConversationStateManager,
            DeltaSQLExecutor,
        )
        from app.models.canonical_query import CanonicalQuery, QueryFilters, QueryIntent
        from app.services.conversation_state import ResultMetadata

        executor = DeltaSQLExecutor(warehouse_id=settings.SQL_WAREHOUSE_ID)

        manager = DeltaConversationStateManager(
            table_name=settings.CONVERSATION_STATE_TABLE_NAME,
            ttl_hours=settings.CONVERSATION_STATE_TTL_HOURS,
            sql_executor=executor,
        )

        assert manager.is_delta_available, "Delta table not available"

        conv_id = f"smoke-test-{int(time.time())}"

        # Create context
        cq = CanonicalQuery(
            intent=QueryIntent.SHIPMENT_QUERY,
            filters=QueryFilters(source_="US", transportation_mode_desc="Air transport"),
            raw_input="shipments from US via air",
            normalized_input="shipments from US via air",
        )
        manager.update_after_query(
            conv_id,
            user_input="shipments from US via air",
            normalized_input="shipments from US via air",
            canonical_query=cq,
            result_metadata=ResultMetadata(total_row_count=100, displayed_rows=50),
            total_row_count=100,
        )

        # Clear in-memory cache to force Delta read
        manager._sessions.pop(conv_id, None)

        # Read back from Delta
        ctx = manager.get_context(conv_id)
        assert ctx is not None, "Failed to read context from Delta"
        assert ctx.last_filters.source_ == "US"
        assert ctx.last_filters.transportation_mode_desc == "Air transport"
        assert ctx.last_result_metadata.total_row_count == 100

        # Reset
        manager.reset_context(conv_id)
        manager._sessions.pop(conv_id, None)
        ctx_after_reset = manager.get_context(conv_id)
        assert ctx_after_reset is None, "Context should be None after reset"

        print(f"LIVE SMOKE PASSED: saved/loaded/reset context {conv_id}")
