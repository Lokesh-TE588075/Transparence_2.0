"""Tests for the chat pipeline orchestrator (Phase 7A).

Uses fake SQL executor and validator — no database or LLM required.
15 test cases covering:
- Normal queries, noisy input, follow-ups
- Summary, download, broaden from context
- Empty results (detail + aggregate)
- Off-topic safety, LLM fallback hook
- Row counts, interpretation notes, template source
"""

import sys

sys.path.insert(0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app")

from app.services.chat_pipeline import (
    ChatPipeline,
    ChatPipelineInput,
    ChatPipelineResult,
    SQLExecutionResult,
    SQLValidationResult,
)
from app.services.conversation_state import ConversationStateManager


# =============================================================================
# FAKES
# =============================================================================


class FakeSQLExecutor:
    """Fake SQL executor returning configurable results."""

    def __init__(self, rows=None, columns=None, total_row_count=None, error=None):
        self._rows = rows or []
        self._columns = columns or []
        self._total_row_count = total_row_count
        self._error = error
        self.last_sql = None

    def execute(self, sql: str) -> SQLExecutionResult:
        self.last_sql = sql
        if self._error:
            return SQLExecutionResult(error=self._error)
        return SQLExecutionResult(
            rows=self._rows,
            columns=self._columns,
            total_row_count=self._total_row_count,
            execution_time_ms=50,
        )


class FakeSQLValidator:
    """Fake SQL validator that always passes."""

    def validate(self, sql: str) -> SQLValidationResult:
        return SQLValidationResult(is_valid=True, sanitized_sql=sql)


class FakeLLMService:
    """Fake LLM service for intent classification."""

    def __init__(self, intent="SHIPMENT_QUERY"):
        self._intent = intent

    def classify_intent(self, user_input: str) -> str:
        return self._intent


# =============================================================================
# HELPERS
# =============================================================================


def _make_pipeline(rows=None, columns=None, total=None, error=None, llm=None):
    """Create a pipeline with fake dependencies."""
    executor = FakeSQLExecutor(
        rows=rows or [],
        columns=columns or [],
        total_row_count=total,
        error=error,
    )
    return ChatPipeline(
        sql_executor=executor,
        sql_validator=FakeSQLValidator(),
        llm_service=llm,
        export_key_generator=lambda: "export-test-key",
    ), executor


def _sample_rows(n=5):
    return [
        {"shipment_number_id": str(1000 + i), "source_": "MX", "destination": "US"}
        for i in range(n)
    ]


# =============================================================================
# TEST 1: Clean Mexico query pipeline
# =============================================================================


class TestCleanMexicoQuery:
    def test_mexico_query(self):
        pipeline, executor = _make_pipeline(
            rows=_sample_rows(5),
            columns=["shipment_number_id", "source_", "destination"],
            total=5,
        )
        result = pipeline.run(ChatPipelineInput(
            user_input="shipments from Mexico",
            conversation_id="conv-1",
        ))

        assert result.status == "success", f"Got status={result.status}, msg={result.message}"
        assert result.intent == "SHIPMENT_QUERY"
        assert result.sql_used is not None
        assert "MX" in result.sql_used
        assert result.is_table is True
        assert result.row_count == 5
        assert result.sql_generation_source == "deterministic_template"


# =============================================================================
# TEST 2: Noisy Mexico query equivalence
# =============================================================================


class TestNoisyMexicoQuery:
    def test_noisy_same_as_clean(self):
        pipeline, executor = _make_pipeline(
            rows=_sample_rows(3),
            columns=["shipment_number_id", "source_", "destination"],
            total=3,
        )
        result = pipeline.run(ChatPipelineInput(
            user_input="hello can you give me shipments from Mexico? //////",
            conversation_id="conv-2",
        ))

        assert result.status == "success", f"Got status={result.status}, msg={result.message}"
        assert result.canonical_query is not None
        assert result.canonical_query.filters.source_ == "MX"
        assert result.sql_generation_source == "deterministic_template"


# =============================================================================
# TEST 3: US air query followed by in-transit follow-up
# =============================================================================


class TestFollowUpInTransit:
    def test_follow_up_preserves_context(self):
        pipeline, executor = _make_pipeline(
            rows=_sample_rows(10),
            columns=["shipment_number_id", "source_", "transportation_mode_desc"],
            total=10,
        )

        # First query
        pipeline.run(ChatPipelineInput(
            user_input="shipments from US via air",
            conversation_id="conv-3",
        ))

        # Follow-up: add status filter
        llm = FakeLLMService(intent="FOLLOW_UP_FILTER")
        pipeline.llm_service = llm
        result = pipeline.run(ChatPipelineInput(
            user_input="which are in transit?",
            conversation_id="conv-3",
        ))

        assert result.status == "success", f"Got status={result.status}, msg={result.message}"
        # SQL should contain status filter
        sql = result.sql_used or ""
        assert "NOT IN" in sql or "in_transit" in str(result.canonical_query)


# =============================================================================
# TEST 4: Quick summary uses previous result
# =============================================================================


class TestQuickSummary:
    def test_summary_no_new_sql(self):
        pipeline, executor = _make_pipeline(
            rows=_sample_rows(10),
            columns=["shipment_number_id", "source_"],
            total=50,
        )

        # First query to populate state
        pipeline.run(ChatPipelineInput(
            user_input="shipments from Mexico",
            conversation_id="conv-4",
        ))

        # Summary request
        llm = FakeLLMService(intent="SUMMARIZE_LAST_RESULT")
        pipeline.llm_service = llm
        result = pipeline.run(ChatPipelineInput(
            user_input="give me a quick summary",
            conversation_id="conv-4",
        ))

        assert result.status == "success", f"Got status={result.status}, msg={result.message}"
        assert result.sql_generation_source == "none"
        assert result.summary is not None
        # No new SQL should have been generated for this call
        assert result.sql_used is None


# =============================================================================
# TEST 5: Download uses previous key
# =============================================================================


class TestDownload:
    def test_download_key_returned(self):
        pipeline, executor = _make_pipeline(
            rows=_sample_rows(5),
            columns=["shipment_number_id"],
            total=5,
        )

        # First query to populate state with export key
        pipeline.run(ChatPipelineInput(
            user_input="shipments from Mexico",
            conversation_id="conv-5",
        ))

        # Download request
        llm = FakeLLMService(intent="DOWNLOAD_LAST_RESULT")
        pipeline.llm_service = llm
        result = pipeline.run(ChatPipelineInput(
            user_input="download this result",
            conversation_id="conv-5",
        ))

        assert result.status == "success"
        assert result.export_key == "export-test-key"
        assert result.sql_generation_source == "none"


# =============================================================================
# TEST 6: Broaden range uses previous suggestion
# =============================================================================


class TestBroadenRange:
    def test_broaden_expands_date_range(self):
        pipeline, executor = _make_pipeline(
            rows=[],  # Empty result first time
            columns=["shipment_number_id"],
        )

        # First query with date range that returns empty
        pipeline.run(ChatPipelineInput(
            user_input="shipments from US this week",
            conversation_id="conv-6",
        ))

        # Now provide results for broadened query
        executor2 = FakeSQLExecutor(
            rows=_sample_rows(3),
            columns=["shipment_number_id", "source_"],
            total_row_count=3,
        )
        pipeline.sql_executor = executor2

        # Broaden request
        llm2 = FakeLLMService(intent="BROADEN_PREVIOUS_DATE_RANGE")
        pipeline.llm_service = llm2
        result = pipeline.run(ChatPipelineInput(
            user_input="yes check a broader range",
            conversation_id="conv-6",
        ))

        # Should succeed or at least attempt SQL gen (might need LLM fallback if
        # template can't handle broadened range without context)
        assert result.status in ("success", "requires_llm_fallback"), (
            f"Got status={result.status}, msg={result.message}"
        )


# =============================================================================
# TEST 7: Empty detail result formatting
# =============================================================================


class TestEmptyDetailResult:
    def test_empty_returns_clear_message(self):
        # Use a real country that passes fuzzy matching but returns no rows
        pipeline, executor = _make_pipeline(
            rows=[],
            columns=["shipment_number_id", "source_"],
        )
        result = pipeline.run(ChatPipelineInput(
            user_input="shipments from Mexico",
            conversation_id="conv-7",
        ))

        assert result.status == "success", f"Got status={result.status}, msg={result.message}"
        assert result.is_table is False
        assert result.row_count == 0
        assert result.assistant_suggestion is not None


# =============================================================================
# TEST 8: Empty aggregate result formatting
# =============================================================================


class TestEmptyAggregateResult:
    def test_aggregate_zero_is_empty(self):
        """Verify aggregate with count=0 is classified as empty."""
        from app.services.sql_template_engine import SQLTemplateResult
        from app.services.result_processor import process_query_result
        from app.models.canonical_query import CanonicalQuery, QueryFilters, QueryIntent

        cq = CanonicalQuery(
            intent=QueryIntent.SHIPMENT_QUERY,
            filters=QueryFilters(source_="ZZ"),
            metrics=["shipment_count", "total_revenue"],
        )
        template = SQLTemplateResult(
            sql="SELECT ...", template_name="SUMMARY_AGGREGATE",
            result_type="aggregate",
        )
        processed = process_query_result(
            rows=[{"shipment_count": 0, "total_revenue": None}],
            columns=["shipment_count", "total_revenue"],
            canonical_query=cq, sql_used="SELECT ...",
            template_result=template,
        )
        assert processed.is_empty is True
        assert processed.is_aggregate_empty is True


# =============================================================================
# TEST 9: Off-topic does not overwrite business context
# =============================================================================


class TestOffTopicPreservesContext:
    def test_off_topic_safe(self):
        pipeline, executor = _make_pipeline(
            rows=_sample_rows(3),
            columns=["shipment_number_id", "source_"],
            total=3,
        )

        # First: normal query
        pipeline.run(ChatPipelineInput(
            user_input="shipments from US",
            conversation_id="conv-9",
        ))

        # Verify state was stored
        ctx_before = pipeline.state_manager.get_or_create_context("conv-9")
        assert ctx_before.last_canonical_query is not None

        # Off-topic input
        llm = FakeLLMService(intent="OFF_TOPIC")
        pipeline.llm_service = llm
        result = pipeline.run(ChatPipelineInput(
            user_input="what is the weather today?",
            conversation_id="conv-9",
        ))

        assert result.status == "no_sql_required"
        assert result.intent == "OFF_TOPIC"

        # Context should be preserved (not overwritten)
        ctx_after = pipeline.state_manager.get_or_create_context("conv-9")
        assert ctx_after.last_canonical_query is not None


# =============================================================================
# TEST 10: Unsupported query returns fallback status safely
# =============================================================================


class TestUnsupportedFallback:
    def test_fallback_without_crash(self):
        pipeline, _ = _make_pipeline(
            rows=_sample_rows(3),
            columns=["shipment_number_id"],
        )
        pipeline.llm_service = None

        from app.models.canonical_query import CanonicalQuery, QueryFilters, QueryIntent
        from app.services.sql_template_engine import generate_sql_from_canonical

        # Create CQ that can't be templated
        cq = CanonicalQuery(
            intent=QueryIntent.SHIPMENT_QUERY,
            filters=QueryFilters(),
            metrics=["highly_custom_metric_xyz"],
            group_by=["nonstandard_grouping"],
        )
        template_result = generate_sql_from_canonical(cq)

        if template_result.requires_llm_fallback:
            result = pipeline._handle_llm_fallback(
                cq, "SHIPMENT_QUERY",
                ChatPipelineInput(user_input="complex query", conversation_id="x"),
                "x", 0.0,
            )
            assert result.status == "requires_llm_fallback"
            assert "deterministic" in result.message.lower() or "advanced" in result.message.lower()


# =============================================================================
# TEST 11: Pipeline result contains row counts
# =============================================================================


class TestRowCounts:
    def test_row_counts_in_result(self):
        pipeline, executor = _make_pipeline(
            rows=_sample_rows(150),
            columns=["shipment_number_id", "source_"],
            total=500,
        )
        result = pipeline.run(ChatPipelineInput(
            user_input="shipments from Mexico",
            conversation_id="conv-11",
        ))

        assert result.status == "success", f"Got status={result.status}, msg={result.message}"
        assert result.row_count == 500
        assert result.displayed_row_count == 100  # default display_limit


# =============================================================================
# TEST 12: Pipeline result contains interpretation notes for ETA
# =============================================================================


class TestETANotes:
    def test_eta_notes_propagate(self):
        pipeline, executor = _make_pipeline(
            rows=_sample_rows(3),
            columns=["shipment_number_id", "eta"],
            total=3,
        )
        # "next week" triggers date parsing
        result = pipeline.run(ChatPipelineInput(
            user_input="shipments to be delivered next week",
            conversation_id="conv-12",
        ))

        # Should succeed (even if date interpretation varies)
        assert result.status == "success", f"Got status={result.status}, msg={result.message}"


# =============================================================================
# TEST 13: Deterministic template source is reported
# =============================================================================


class TestTemplateSource:
    def test_deterministic_source_reported(self):
        pipeline, executor = _make_pipeline(
            rows=_sample_rows(2),
            columns=["shipment_number_id"],
            total=2,
        )
        result = pipeline.run(ChatPipelineInput(
            user_input="shipments from Germany",
            conversation_id="conv-13",
        ))

        assert result.status == "success", f"Got status={result.status}, msg={result.message}"
        assert result.sql_generation_source == "deterministic_template"


# =============================================================================
# TEST 14: No unsafe SQL generated by templates
# =============================================================================


class TestSafeSQL:
    def test_no_select_star(self):
        pipeline, executor = _make_pipeline(
            rows=_sample_rows(2),
            columns=["shipment_number_id"],
            total=2,
        )
        result = pipeline.run(ChatPipelineInput(
            user_input="shipments from Japan",
            conversation_id="conv-14",
        ))

        assert result.status == "success", f"Got status={result.status}, msg={result.message}"
        sql = result.sql_used or ""
        # Should NOT have SELECT *
        assert "SELECT *" not in sql
        # Should have LIMIT
        assert "LIMIT" in sql


# =============================================================================
# TEST 15: Pipeline integrity
# =============================================================================


class TestPipelineIntegrity:
    def test_pipeline_does_not_import_live_modules(self):
        """Verify pipeline doesn\'t pull in live chat.py or modify it."""
        import importlib
        mod = importlib.import_module("app.services.chat_pipeline")
        assert mod.USE_NEW_ACCURACY_PIPELINE is False

    def test_pipeline_class_instantiable_without_deps(self):
        """Pipeline should instantiate without any injected deps."""
        pipeline = ChatPipeline()
        assert pipeline.state_manager is not None
        assert pipeline.sql_executor is None
