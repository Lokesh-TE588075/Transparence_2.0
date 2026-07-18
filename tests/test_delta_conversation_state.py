"""Phase 8 Tests: Delta conversation state serialization and persistence logic.

Tests serialize/deserialize, TTL, fallback behavior.
Does NOT require real Delta — uses mocked SQL executor.
"""

import sys
import json
import time

sys.path.insert(0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app")

from app.models.canonical_query import CanonicalQuery, DateRange, QueryFilters, QueryIntent
from app.services.conversation_state import BusinessContext, ResultMetadata
from app.services.delta_conversation_state import (
    DeltaConversationStateManager,
    serialize_business_context,
    deserialize_business_context,
)
from app.services.chat_pipeline import SQLExecutionResult


# =============================================================================
# HELPERS
# =============================================================================


def _make_sample_context() -> BusinessContext:
    """Create a BusinessContext with representative data."""
    filters = QueryFilters(
        source_="US",
        destination="DE",
        transportation_mode_desc="Air transport",
        status_category="in_transit",
        date_range=DateRange(field="eta", start="2026-07-01", end="2026-07-31"),
    )
    cq = CanonicalQuery(
        intent=QueryIntent.SHIPMENT_QUERY,
        filters=filters,
        metrics=["count"],
        group_by=[],
        sort=[],
        limit=500,
        confidence=0.95,
        source="deterministic",
        raw_input="shipments from US to Germany via air",
        normalized_input="shipments from US to Germany via air",
    )
    metadata = ResultMetadata(
        total_row_count=150,
        displayed_rows=100,
        columns=["shipment_number_id", "source_", "destination"],
        has_aggregates=False,
        is_empty=False,
        execution_time_ms=230,
    )
    return BusinessContext(
        last_user_input="shipments from US to Germany via air",
        last_normalized_input="shipments from US to Germany via air",
        last_canonical_query=cq,
        last_validated_sql="SELECT * FROM table WHERE source_ = 'US'",
        last_result_metadata=metadata,
        last_displayed_rows=[{"shipment_number_id": "SH001", "source_": "US"}],
        last_total_row_count=150,
        last_download_key="export-abc123",
        last_filters=filters,
        last_assistant_suggestion="Try broadening the date range",
        last_result_summary="Found 150 shipments from US to Germany.",
        last_updated_at=time.time(),
        query_count=3,
    )


class FakeSQLExecutor:
    """Mock SQL executor that records calls."""
    def __init__(self, fail=False, rows=None):
        self.calls = []
        self.fail = fail
        self.rows = rows or []

    def execute(self, sql):
        self.calls.append(sql)
        if self.fail:
            return SQLExecutionResult(error="Simulated failure")
        return SQLExecutionResult(rows=self.rows, columns=[], total_row_count=0)


# =============================================================================
# TEST 1: Serialize preserves canonical query
# =============================================================================


class TestSerializeDeserialize:
    def test_preserves_canonical_query(self):
        ctx = _make_sample_context()
        json_str = serialize_business_context(ctx)
        restored = deserialize_business_context(json_str)
        assert restored.last_canonical_query is not None
        assert restored.last_canonical_query.intent == QueryIntent.SHIPMENT_QUERY
        assert restored.last_canonical_query.confidence == 0.95
        assert restored.last_canonical_query.raw_input == "shipments from US to Germany via air"

    def test_preserves_filters(self):
        ctx = _make_sample_context()
        json_str = serialize_business_context(ctx)
        restored = deserialize_business_context(json_str)
        assert restored.last_filters is not None
        assert restored.last_filters.source_ == "US"
        assert restored.last_filters.destination == "DE"
        assert restored.last_filters.transportation_mode_desc == "Air transport"
        assert restored.last_filters.status_category == "in_transit"

    def test_preserves_date_range(self):
        ctx = _make_sample_context()
        json_str = serialize_business_context(ctx)
        restored = deserialize_business_context(json_str)
        assert restored.last_filters.date_range is not None
        assert restored.last_filters.date_range.field == "eta"
        assert restored.last_filters.date_range.start == "2026-07-01"
        assert restored.last_filters.date_range.end == "2026-07-31"

    def test_preserves_result_metadata(self):
        ctx = _make_sample_context()
        json_str = serialize_business_context(ctx)
        restored = deserialize_business_context(json_str)
        assert restored.last_result_metadata is not None
        assert restored.last_result_metadata.total_row_count == 150
        assert restored.last_result_metadata.displayed_rows == 100
        assert restored.last_result_metadata.columns == ["shipment_number_id", "source_", "destination"]
        assert restored.last_result_metadata.execution_time_ms == 230

    def test_preserves_scalar_fields(self):
        ctx = _make_sample_context()
        json_str = serialize_business_context(ctx)
        restored = deserialize_business_context(json_str)
        assert restored.last_download_key == "export-abc123"
        assert restored.last_assistant_suggestion == "Try broadening the date range"
        assert restored.last_result_summary == "Found 150 shipments from US to Germany."
        assert restored.query_count == 3

    def test_handles_none_fields(self):
        ctx = BusinessContext(last_updated_at=time.time())
        json_str = serialize_business_context(ctx)
        restored = deserialize_business_context(json_str)
        assert restored.last_canonical_query is None
        assert restored.last_filters is None
        assert restored.last_result_metadata is None


# =============================================================================
# TEST 4: Expired context not returned
# =============================================================================


class TestTTLExpiry:
    def test_expired_context_not_returned(self):
        """Context older than TTL should not be returned."""
        executor = FakeSQLExecutor()
        manager = DeltaConversationStateManager(
            table_name="test.schema.state_table",
            ttl_hours=1,
            sql_executor=executor,
        )
        # Manually inject expired context
        ctx = BusinessContext(
            last_updated_at=time.time() - 7200,  # 2 hours ago (> 1 hour TTL)
            last_user_input="old query",
        )
        manager._sessions["conv-expired"] = ctx
        assert manager.get_context("conv-expired") is None


# =============================================================================
# TEST 5: Reset marks inactive
# =============================================================================


class TestReset:
    def test_reset_clears_memory(self):
        executor = FakeSQLExecutor()
        manager = DeltaConversationStateManager(
            table_name="test.schema.state_table",
            ttl_hours=24,
            sql_executor=executor,
        )
        manager._sessions["conv-x"] = _make_sample_context()
        manager.reset_context("conv-x")
        assert manager.get_context("conv-x") is None

    def test_reset_calls_delta_update(self):
        executor = FakeSQLExecutor()
        manager = DeltaConversationStateManager(
            table_name="test.schema.state_table",
            ttl_hours=24,
            sql_executor=executor,
        )
        manager._delta_available = True
        manager._sessions["conv-y"] = _make_sample_context()
        manager.reset_context("conv-y")
        # Should have attempted Delta UPDATE (mark inactive)
        assert any("UPDATE" in sql and "is_active = false" in sql for sql in executor.calls)


# =============================================================================
# TEST 6: Delta write failure falls back to in-memory
# =============================================================================


class TestDeltaFallback:
    def test_write_failure_doesnt_break_query(self):
        """If Delta write fails, in-memory still works."""
        executor = FakeSQLExecutor(fail=True)
        manager = DeltaConversationStateManager(
            table_name="test.schema.state_table",
            ttl_hours=24,
            sql_executor=executor,
        )
        # Force delta_available to test persistence failure path
        manager._delta_available = True

        # This should NOT raise
        cq = CanonicalQuery(
            intent=QueryIntent.SHIPMENT_QUERY,
            filters=QueryFilters(source_="US"),
            raw_input="test",
            normalized_input="test",
        )
        manager.update_after_query(
            "conv-fail",
            user_input="test",
            normalized_input="test",
            canonical_query=cq,
        )
        # In-memory state should still be available
        ctx = manager._sessions.get("conv-fail")
        assert ctx is not None
        assert ctx.last_user_input == "test"
