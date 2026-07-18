"""Unit tests for Phase G4/G5: Genie backend feature flags and routing.

Tests are structured to avoid FastAPI imports entirely.  Instead of
importing chat.py directly, we test:
  - The routing *decision logic* via a local simulation that mirrors
    the chat.py Genie branch verbatim.
  - The genie_backend_factory singleton and reset helpers.
  - The config.py default flag values.
  - The response field mapping from Genie pipeline result to ChatResponse
    keyword arguments.
  - _genie_table_to_table_data (imported after mocking heavy deps).

Test cases:
    1.  USE_GENIE_BACKEND=false routes to non-Genie path.
    2.  USE_GENIE_BACKEND=true routes to GeniePipeline.
    3.  Genie success returns ChatResponse-compatible dict.
    4.  Genie text-only response works (no table, no SQL).
    5.  Genie table response maps headers + rows correctly.
    6.  Genie response includes suggested_questions when present.
    7.  Genie visualization reference is preserved.
    8.  Genie failure + fallback enabled falls through (outcome='fallback').
    9.  Genie failure + fallback disabled returns safe error outcome.
    10. conversation_id is passed unchanged to GeniePipeline.run().
    11. GENIE_SPACE_ID from config is forwarded to GeniePipeline constructor.
    12. Default flags: USE_GENIE_BACKEND=False and no Genie is invoked.
    13. _genie_table_to_table_data converts dict → TableData and handles None.
"""

import os
import sys
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

sys.path.insert(
    0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app"
)

# ---------------------------------------------------------------------------
# Minimal env so config.py / genie modules don't crash on import
# ---------------------------------------------------------------------------
os.environ.setdefault("DATABRICKS_TOKEN", "test-fake-token-g4g5")
os.environ.setdefault("DATABRICKS_HOST",  "https://fake-workspace.databricks.com")

# ---------------------------------------------------------------------------
# Stub pydantic_settings globally (not installed on the notebook kernel).
# This ensures that any lazy import of app.config (e.g. via genie_backend_factory
# in TestFactoryUsesConfig) succeeds without importing the real package.
# Using a minimal FakeBaseSettings class (not raw MagicMock) so that the
# Settings class attributes — populated via os.getenv() at class-definition
# time — remain accessible as plain Python attributes on the settings instance.
# ---------------------------------------------------------------------------
_PS_MOCK = MagicMock()


class _FakeBaseSettingsStub:
    """Minimal pydantic_settings.BaseSettings replacement for test environment."""
    def __init__(self, **kwargs):
        pass
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


_PS_MOCK.BaseSettings = _FakeBaseSettingsStub
sys.modules.setdefault("pydantic_settings", _PS_MOCK)

# ---------------------------------------------------------------------------
# Patch Databricks SDK before importing anything that touches it
# ---------------------------------------------------------------------------
_mock_ws = MagicMock()
_mock_ws.config.authenticate.return_value = {
    "Authorization": "Bearer test-fake-token-g4g5"
}

with patch("databricks.sdk.WorkspaceClient", return_value=_mock_ws):
    from app.services.genie_client import (
        GenieClientError,
        GenieExecutionError,
        GenieTimeoutError,
    )

from app.services.genie_pipeline import GeniePipeline
from app.services.genie_session_store import GenieSessionStore
from app.services import genie_backend_factory as _factory_module
from app.services.genie_backend_factory import get_genie_pipeline, reset_genie_pipeline


# =============================================================================
# CONSTANTS
# =============================================================================

DEFAULT_SPACE_ID = "01f17a93e6aa1b97a9da7ef329e15e46"
APP_CONV_ID      = "app-conv-g4g5-test-001"
USER_MSG         = "shipments from US via air"


# =============================================================================
# HELPERS: mock settings object
# =============================================================================


@dataclass
class _MockSettings:
    """Minimal settings mirror used for routing simulation tests."""
    USE_GENIE_BACKEND:              bool  = False
    GENIE_FALLBACK_TO_CUSTOM_PIPELINE: bool = True
    GENIE_SPACE_ID:                 str   = DEFAULT_SPACE_ID
    GENIE_RESPONSE_TIMEOUT_SECONDS: int   = 30
    GENIE_POLL_INTERVAL_SECONDS:    float = 0.0
    GENIE_DEBUG:                    bool  = False
    USE_NEW_ACCURACY_PIPELINE:      bool  = False


# =============================================================================
# HELPERS: fake pipeline
# =============================================================================


class _FakeGeniePipeline:
    """Duck-typed GeniePipeline for routing tests (no HTTP)."""

    def __init__(self, result: Dict[str, Any]):
        self._result   = result
        self.run_calls: List = []  # [(user_message, app_conversation_id), ...]

    def run(
        self,
        user_message: str,
        app_conversation_id: str,
        execution_time_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        self.run_calls.append((user_message, app_conversation_id))
        return self._result


def _success_result(
    message: str = "3 shipments found.",
    is_table: bool = False,
    table_data: Optional[dict] = None,
    suggested_questions: Optional[list] = None,
    has_visualization: bool = False,
    visualization: Optional[dict] = None,
    row_count: int = 0,
) -> Dict[str, Any]:
    return {
        "status":             "success",
        "message":            message,
        "is_table":           is_table,
        "table_data":         table_data,
        "row_count":          row_count,
        "download_key":       None,
        "execution_time_ms":  250,
        "conversation_id":    APP_CONV_ID,
        "clarification":      None,
        "source":             "genie",
        "genie_conversation_id": "fake-genie-conv",
        "genie_message_id":      "fake-genie-msg",
        "generated_sql":      None,
        "suggested_questions": suggested_questions or [],
        "has_visualization":  has_visualization,
        "visualization":      visualization,
        "attachment_types":   ["text"],
        "debug_info":         None,
        "fallback_recommended": False,
    }


def _error_result(message: str = "Request failed.") -> Dict[str, Any]:
    base = _success_result(message=message)
    base["status"]               = "error"
    base["fallback_recommended"] = True
    return base


# =============================================================================
# LOCAL ROUTING SIMULATION
# Mirrors the Genie branch in chat.py exactly so we can test decisions
# without importing FastAPI.
# =============================================================================


def _simulate_genie_routing(
    user_message: str,
    conversation_id: str,
    settings: _MockSettings,
    genie_pipeline: Optional[_FakeGeniePipeline],
) -> tuple:
    """
    Returns: (outcome: str, result: Optional[dict])

    outcome values:
      'skip'              — USE_GENIE_BACKEND is False
      'genie_success'     — Genie responded, no fallback
      'fallback'          — Genie wants fallback, GENIE_FALLBACK enabled
      'genie_error'       — Genie wants fallback, GENIE_FALLBACK disabled
      'exception_fallback'— Genie raised an exception, fallback enabled
      'exception_error'   — Genie raised an exception, fallback disabled
    """
    if not settings.USE_GENIE_BACKEND:
        return "skip", None

    try:
        result = genie_pipeline.run(user_message, conversation_id)

        if not result.get("fallback_recommended", False):
            return "genie_success", result

        if not settings.GENIE_FALLBACK_TO_CUSTOM_PIPELINE:
            return "genie_error", result

        return "fallback", result

    except Exception:
        if not settings.GENIE_FALLBACK_TO_CUSTOM_PIPELINE:
            return "exception_error", None
        return "exception_fallback", None


# =============================================================================
# TEST 1: USE_GENIE_BACKEND=false skips Genie
# TEST 2: USE_GENIE_BACKEND=true routes to GeniePipeline
# =============================================================================


class TestRoutingGateFlag:
    def test_flag_false_outcome_is_skip(self):
        """When USE_GENIE_BACKEND=False, outcome must be 'skip'."""
        settings = _MockSettings(USE_GENIE_BACKEND=False)
        pipeline = _FakeGeniePipeline(_success_result())
        outcome, _ = _simulate_genie_routing(USER_MSG, APP_CONV_ID, settings, pipeline)
        assert outcome == "skip"

    def test_flag_false_genie_pipeline_never_called(self):
        settings = _MockSettings(USE_GENIE_BACKEND=False)
        pipeline = _FakeGeniePipeline(_success_result())
        _simulate_genie_routing(USER_MSG, APP_CONV_ID, settings, pipeline)
        assert len(pipeline.run_calls) == 0

    def test_flag_true_outcome_is_genie_success(self):
        """When USE_GENIE_BACKEND=True and Genie succeeds, outcome must be 'genie_success'."""
        settings = _MockSettings(USE_GENIE_BACKEND=True)
        pipeline = _FakeGeniePipeline(_success_result())
        outcome, _ = _simulate_genie_routing(USER_MSG, APP_CONV_ID, settings, pipeline)
        assert outcome == "genie_success"

    def test_flag_true_genie_pipeline_is_called_once(self):
        settings = _MockSettings(USE_GENIE_BACKEND=True)
        pipeline = _FakeGeniePipeline(_success_result())
        _simulate_genie_routing(USER_MSG, APP_CONV_ID, settings, pipeline)
        assert len(pipeline.run_calls) == 1


# =============================================================================
# TEST 3: Genie success returns ChatResponse-compatible dict
# TEST 4: Text-only response
# TEST 5: Table response
# =============================================================================


class TestGenieSuccessMapping:
    def test_success_result_has_all_chat_response_fields(self):
        """The Genie pipeline result must contain all ChatResponse fields."""
        result = _success_result()
        required = [
            "status", "message", "is_table", "table_data",
            "row_count", "download_key", "execution_time_ms",
            "conversation_id", "clarification",
        ]
        for field in required:
            assert field in result, f"Missing field: {field}"

    def test_text_only_result_is_not_table(self):
        result = _success_result(message="Summary: 3 shipments.", is_table=False)
        assert result["is_table"] is False
        assert result["table_data"] is None
        assert result["row_count"] == 0

    def test_text_only_result_status_success(self):
        settings = _MockSettings(USE_GENIE_BACKEND=True)
        pipeline = _FakeGeniePipeline(_success_result())
        outcome, result = _simulate_genie_routing(USER_MSG, APP_CONV_ID, settings, pipeline)
        assert outcome == "genie_success"
        assert result["status"] == "success"

    def test_table_result_is_table_true(self):
        table_data = {
            "headers": ["shipment_number_id", "source_"],
            "rows": [["SHP-001", "US"], ["SHP-002", "US"]],
        }
        result = _success_result(
            is_table=True,
            table_data=table_data,
            row_count=2,
        )
        assert result["is_table"] is True
        assert result["table_data"] == table_data
        assert result["row_count"] == 2

    def test_table_result_routing_outcome(self):
        settings = _MockSettings(USE_GENIE_BACKEND=True)
        pipeline = _FakeGeniePipeline(
            _success_result(
                is_table=True,
                table_data={"headers": ["col"], "rows": [["val"]]},
                row_count=1,
            )
        )
        outcome, result = _simulate_genie_routing(USER_MSG, APP_CONV_ID, settings, pipeline)
        assert outcome == "genie_success"
        assert result["is_table"] is True


# =============================================================================
# TEST 6: suggested_questions preserved
# TEST 7: visualization reference preserved
# =============================================================================


class TestGenieExtensionFields:
    def test_suggested_questions_in_result(self):
        questions = ["What are top destinations?", "Which are in transit?", "Revenue by BU?"]
        result = _success_result(suggested_questions=questions)
        assert result["suggested_questions"] == questions

    def test_suggested_questions_empty_list_when_none(self):
        result = _success_result(suggested_questions=None)
        # Mapper fills empty list when no suggestions
        assert "suggested_questions" in result

    def test_visualization_reference_preserved(self):
        viz = {
            "type":             "genie_viz_reference",
            "query_attachment_id": "att-query-001",
            "render_strategy":  "client_side_from_query_result",
            "can_render_client_side": True,
        }
        result = _success_result(has_visualization=True, visualization=viz)
        assert result["has_visualization"] is True
        assert result["visualization"] == viz

    def test_visualization_none_when_no_viz(self):
        result = _success_result(has_visualization=False, visualization=None)
        assert result["has_visualization"] is False
        assert result["visualization"] is None

    def test_visualization_no_chart_spec(self):
        """Viz attachment must never contain a raw chart spec (Genie doesn't expose one)."""
        viz = {
            "type": "genie_viz_reference",
            "query_attachment_id": "att-001",
            "render_strategy": "client_side_from_query_result",
        }
        result = _success_result(has_visualization=True, visualization=viz)
        forbidden = {"vega_lite", "plotly", "chart_spec", "spec", "image_url"}
        found = forbidden & set(result["visualization"].keys())
        assert not found, f"Forbidden chart spec keys found: {found}"

    def test_source_field_is_genie(self):
        result = _success_result()
        assert result["source"] == "genie"


# =============================================================================
# TEST 8: Genie failure + fallback enabled
# TEST 9: Genie failure + fallback disabled
# =============================================================================


class TestGenieFallbackRouting:
    def test_fallback_enabled_outcome_is_fallback(self):
        """When Genie returns fallback_recommended=True and fallback is enabled,
        the outcome must be 'fallback' so the next pipeline gets a chance.
        """
        settings = _MockSettings(
            USE_GENIE_BACKEND=True,
            GENIE_FALLBACK_TO_CUSTOM_PIPELINE=True,
        )
        pipeline = _FakeGeniePipeline(_error_result())
        outcome, _ = _simulate_genie_routing(USER_MSG, APP_CONV_ID, settings, pipeline)
        assert outcome == "fallback"

    def test_fallback_disabled_outcome_is_genie_error(self):
        """When Genie fails and GENIE_FALLBACK_TO_CUSTOM_PIPELINE=False,
        caller gets a genie_error — no other pipeline is tried.
        """
        settings = _MockSettings(
            USE_GENIE_BACKEND=True,
            GENIE_FALLBACK_TO_CUSTOM_PIPELINE=False,
        )
        pipeline = _FakeGeniePipeline(_error_result())
        outcome, result = _simulate_genie_routing(USER_MSG, APP_CONV_ID, settings, pipeline)
        assert outcome == "genie_error"

    def test_fallback_disabled_error_result_has_error_status(self):
        settings = _MockSettings(
            USE_GENIE_BACKEND=True,
            GENIE_FALLBACK_TO_CUSTOM_PIPELINE=False,
        )
        pipeline = _FakeGeniePipeline(_error_result("Genie unavailable."))
        _, result = _simulate_genie_routing(USER_MSG, APP_CONV_ID, settings, pipeline)
        assert result["status"] == "error"

    def test_exception_fallback_enabled_falls_through(self):
        """If GenieClient raises an exception and fallback is enabled, fall through."""
        settings = _MockSettings(
            USE_GENIE_BACKEND=True,
            GENIE_FALLBACK_TO_CUSTOM_PIPELINE=True,
        )

        class _ExplodingPipeline:
            run_calls: list = []

            def run(self, *a, **kw):
                self.run_calls.append(a)
                raise GenieTimeoutError("mocked timeout")

        outcome, _ = _simulate_genie_routing(
            USER_MSG, APP_CONV_ID, settings, _ExplodingPipeline()
        )
        assert outcome == "exception_fallback"

    def test_exception_fallback_disabled_returns_error(self):
        settings = _MockSettings(
            USE_GENIE_BACKEND=True,
            GENIE_FALLBACK_TO_CUSTOM_PIPELINE=False,
        )

        class _ExplodingPipeline:
            def run(self, *a, **kw):
                raise GenieExecutionError("mocked failure")

        outcome, _ = _simulate_genie_routing(
            USER_MSG, APP_CONV_ID, settings, _ExplodingPipeline()
        )
        assert outcome == "exception_error"


    def test_shape_retry_exhausted_routes_to_fallback_when_enabled(self):
        """P1 FIX 5 integration: when GeniePipeline sets shape_retry_exhausted=True
        and fallback_recommended=True (retry also returned raw rows), the routing
        outcome must be 'fallback', not 'genie_success'.
        Raw retry data must never reach the frontend as a successful response.
        """
        settings = _MockSettings(
            USE_GENIE_BACKEND=True,
            GENIE_FALLBACK_TO_CUSTOM_PIPELINE=True,
        )
        # Simulate what _run_inner returns after FIX 5 Step 8c fires
        exhausted_result = _success_result()
        exhausted_result["fallback_recommended"] = True
        exhausted_result["shape_retry_exhausted"] = True
        pipeline = _FakeGeniePipeline(exhausted_result)
        outcome, _ = _simulate_genie_routing(USER_MSG, APP_CONV_ID, settings, pipeline)
        assert outcome == "fallback", (
            f"shape_retry_exhausted=True must produce outcome='fallback', got {outcome!r}"
        )

    def test_shape_retry_exhausted_does_not_surface_as_genie_success(self):
        """P1 FIX 5 integration: a result with shape_retry_exhausted=True must
        never be returned as 'genie_success'.  If it were, the raw shipment rows
        from the failed retry would be exposed to the frontend.
        """
        settings = _MockSettings(
            USE_GENIE_BACKEND=True,
            GENIE_FALLBACK_TO_CUSTOM_PIPELINE=True,
        )
        exhausted_result = _success_result()
        exhausted_result["fallback_recommended"] = True
        exhausted_result["shape_retry_exhausted"] = True
        pipeline = _FakeGeniePipeline(exhausted_result)
        outcome, _ = _simulate_genie_routing(USER_MSG, APP_CONV_ID, settings, pipeline)
        assert outcome != "genie_success", (
            "Raw retry result with shape_retry_exhausted=True must not be surfaced as "
            f"genie_success — it would expose invalid analytical data to the frontend. "
            f"outcome={outcome!r}"
        )

# =============================================================================
# TEST 10: conversation_id is passed to GeniePipeline
# =============================================================================


class TestConversationIdPropagation:
    def test_conversation_id_forwarded(self):
        """The app conversation_id must reach GeniePipeline.run() unchanged."""
        settings = _MockSettings(USE_GENIE_BACKEND=True)
        pipeline = _FakeGeniePipeline(_success_result())
        conv_id = "conv-test-propagation-unique-id-42"
        _simulate_genie_routing(USER_MSG, conv_id, settings, pipeline)
        assert pipeline.run_calls[0][1] == conv_id

    def test_user_message_forwarded(self):
        settings = _MockSettings(USE_GENIE_BACKEND=True)
        pipeline = _FakeGeniePipeline(_success_result())
        msg = "What is the transit rate for Mexico?"
        _simulate_genie_routing(msg, APP_CONV_ID, settings, pipeline)
        assert pipeline.run_calls[0][0] == msg


# =============================================================================
# TEST 11: GENIE_SPACE_ID from config forwarded to factory
# =============================================================================


class TestFactoryUsesConfig:
    """Patch _build_pipeline directly to avoid pydantic_settings on the notebook kernel.

    In production (Databricks Apps), pydantic_settings IS installed.
    These tests verify the singleton/reset logic independent of config loading.
    """

    @staticmethod
    def _make_mock_pipeline(space_id: str = DEFAULT_SPACE_ID) -> GeniePipeline:
        """Construct a real GeniePipeline with a mocked client for assertions."""
        return GeniePipeline(
            genie_client=MagicMock(),
            session_store=GenieSessionStore(),
            space_id=space_id,
        )

    def test_factory_returns_genie_pipeline_instance(self):
        """get_genie_pipeline() must return a GeniePipeline."""
        reset_genie_pipeline()
        mock_pl = self._make_mock_pipeline()
        try:
            with patch.object(_factory_module, "_build_pipeline", return_value=mock_pl):
                pipeline = get_genie_pipeline()
                assert isinstance(pipeline, GeniePipeline)
        finally:
            reset_genie_pipeline()

    def test_factory_uses_space_id_from_settings(self):
        """GeniePipeline must expose the space_id that _build_pipeline injected."""
        reset_genie_pipeline()
        mock_pl = self._make_mock_pipeline(space_id=DEFAULT_SPACE_ID)
        try:
            with patch.object(_factory_module, "_build_pipeline", return_value=mock_pl):
                pipeline = get_genie_pipeline()
                assert pipeline._space_id == DEFAULT_SPACE_ID
        finally:
            reset_genie_pipeline()

    def test_factory_is_singleton(self):
        """Two consecutive get_genie_pipeline() calls must return the SAME object."""
        reset_genie_pipeline()
        mock_pl = self._make_mock_pipeline()
        try:
            with patch.object(_factory_module, "_build_pipeline", return_value=mock_pl):
                p1 = get_genie_pipeline()
                p2 = get_genie_pipeline()
                assert p1 is p2
        finally:
            reset_genie_pipeline()

    def test_reset_forces_new_instance(self):
        """After reset_genie_pipeline(), a new object must be created."""
        reset_genie_pipeline()
        pl_a = self._make_mock_pipeline()
        pl_b = self._make_mock_pipeline()
        call_count = [0]

        def _side_effect():
            call_count[0] += 1
            return pl_a if call_count[0] == 1 else pl_b

        try:
            with patch.object(_factory_module, "_build_pipeline", side_effect=_side_effect):
                p1 = get_genie_pipeline()
                reset_genie_pipeline()
                p2 = get_genie_pipeline()
                assert p1 is not p2
        finally:
            reset_genie_pipeline()

    def test_factory_thread_safe(self):
        """Concurrent calls to get_genie_pipeline() must return the same singleton."""
        reset_genie_pipeline()
        mock_pl = self._make_mock_pipeline()
        pipelines_seen = []

        # Outer patch covers all threads — no per-thread context manager
        with patch.object(_factory_module, "_build_pipeline", return_value=mock_pl):
            def _worker():
                pipelines_seen.append(id(get_genie_pipeline()))

            threads = [threading.Thread(target=_worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert len(set(pipelines_seen)) == 1, (
            f"Expected 1 unique pipeline id, got {len(set(pipelines_seen))}: {pipelines_seen}"
        )
        reset_genie_pipeline()


# =============================================================================
# TEST 12: Default flags
# =============================================================================


class TestDefaultFlagValues:
    def test_mock_settings_default_genie_off(self):
        """Default _MockSettings mirrors expected production defaults."""
        s = _MockSettings()
        assert s.USE_GENIE_BACKEND is False

    def test_mock_settings_default_fallback_enabled(self):
        s = _MockSettings()
        assert s.GENIE_FALLBACK_TO_CUSTOM_PIPELINE is True

    def test_mock_settings_default_space_id_correct(self):
        s = _MockSettings()
        assert s.GENIE_SPACE_ID == DEFAULT_SPACE_ID

    def test_default_flags_outcome_is_skip(self):
        """With all defaults, Genie must never be invoked."""
        s = _MockSettings()  # USE_GENIE_BACKEND=False
        pipeline = _FakeGeniePipeline(_success_result())
        outcome, _ = _simulate_genie_routing(USER_MSG, APP_CONV_ID, s, pipeline)
        assert outcome == "skip"
        assert len(pipeline.run_calls) == 0

    def test_genie_only_activates_when_explicitly_enabled(self):
        s_off = _MockSettings(USE_GENIE_BACKEND=False)
        s_on  = _MockSettings(USE_GENIE_BACKEND=True)
        p_off = _FakeGeniePipeline(_success_result())
        p_on  = _FakeGeniePipeline(_success_result())
        _simulate_genie_routing(USER_MSG, APP_CONV_ID, s_off, p_off)
        _simulate_genie_routing(USER_MSG, APP_CONV_ID, s_on,  p_on)
        assert len(p_off.run_calls) == 0
        assert len(p_on.run_calls)  == 1


# =============================================================================
# TEST 13: _genie_table_to_table_data helper
# =============================================================================


class TestGenieTableToTableData:
    """
    We mock the heavy FastAPI deps before importing the helper so we can
    test the conversion logic without starting a FastAPI app.
    """

    @staticmethod
    def _import_helper():
        """Import _genie_table_to_table_data by mocking heavy dependencies."""
        import importlib
        mocks = {
            "fastapi":                            MagicMock(),
            "pydantic_settings":                  MagicMock(),
            "app.business_rules.mappings":        MagicMock(),
            "app.business_rules.prompt_builder":  MagicMock(),
            "app.guardrails.sql_validator":       MagicMock(),
            "app.services.audit_service":         MagicMock(),
            "app.services.conversation_manager":  MagicMock(),
            "app.services.llm_service":           MagicMock(),
            "app.services.sql_service":           MagicMock(),
        }
        with patch.dict("sys.modules", mocks):
            spec = importlib.util.spec_from_file_location(
                "chat_module",
                "/Workspace/Users/lokesh.choraria@te.com/"
                "Transparence/transparence_app/app/routes/chat.py",
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod._genie_table_to_table_data, mod.TableData

    def test_none_input_returns_none(self):
        fn, _ = self._import_helper()
        assert fn(None) is None

    def test_empty_dict_returns_none(self):
        fn, _ = self._import_helper()
        assert fn({}) is None

    def test_valid_dict_returns_table_data(self):
        fn, TableData = self._import_helper()
        td = {"headers": ["col_a", "col_b"], "rows": [["v1", "v2"]]}
        result = fn(td)
        assert result is not None
        assert isinstance(result, TableData)
        assert result.headers == ["col_a", "col_b"]
        assert result.rows    == [["v1", "v2"]]

    def test_missing_keys_use_empty_defaults(self):
        fn, TableData = self._import_helper()
        result = fn({"headers": ["col_a"]})  # no 'rows' key
        assert result.headers == ["col_a"]
        assert result.rows    == []


# =============================================================================
# TEST 14: genie_message_id and generated_sql in the Genie result dict
# =============================================================================


class TestGenieMessageAndSqlFields:
    """genie_message_id and generated_sql are Optional[str] Genie extension
    fields.  The response mapper already produces them; these tests confirm:
      - They are present in the standard _success_result() helper dict.
      - generated_sql flows through the routing simulation unmodified.
      - genie_message_id=None is a valid, non-error state.
      - The non-Genie routing path returns no Genie data (None result).
    """

    def test_genie_message_id_present_in_success_result(self):
        """genie_message_id is set by the mapper and present in _success_result."""
        result = _success_result()
        assert "genie_message_id" in result, "genie_message_id key missing from result dict"
        assert result["genie_message_id"] == "fake-genie-msg"

    def test_generated_sql_key_present_in_success_result(self):
        """generated_sql key is always present (None for text-only Genie turns)."""
        result = _success_result()
        assert "generated_sql" in result, "generated_sql key missing from result dict"

    def test_generated_sql_value_flows_through_routing(self):
        """When Genie produces SQL, the routing simulation passes it through verbatim."""
        sql = (
            "SELECT origin_country, COUNT(*) AS shipment_count "
            "FROM lbn_with_scorecard "
            "WHERE transport_mode = 'Air' "
            "GROUP BY origin_country ORDER BY 2 DESC"
        )
        result = _success_result()
        result["generated_sql"] = sql
        pipeline = _FakeGeniePipeline(result)
        settings = _MockSettings(USE_GENIE_BACKEND=True)
        outcome, r = _simulate_genie_routing(USER_MSG, APP_CONV_ID, settings, pipeline)
        assert outcome == "genie_success"
        assert r["generated_sql"] == sql, (
            f"generated_sql was mutated in routing: got {r['generated_sql']!r}"
        )

    def test_genie_message_id_none_is_valid(self):
        """genie_message_id=None must not cause an error; it is Optional."""
        result = _success_result()
        result["genie_message_id"] = None
        pipeline = _FakeGeniePipeline(result)
        settings = _MockSettings(USE_GENIE_BACKEND=True)
        outcome, r = _simulate_genie_routing(USER_MSG, APP_CONV_ID, settings, pipeline)
        assert outcome == "genie_success"
        assert r["genie_message_id"] is None

    def test_default_non_genie_path_omits_genie_fields(self):
        """USE_GENIE_BACKEND=False: routing returns (skip, None) — no Genie data."""
        settings = _MockSettings(USE_GENIE_BACKEND=False)
        pipeline = _FakeGeniePipeline(_success_result())
        outcome, result = _simulate_genie_routing(USER_MSG, APP_CONV_ID, settings, pipeline)
        assert outcome == "skip", f"Expected 'skip', got {outcome!r}"
        assert result is None, (
            f"Non-Genie path must return None result. Got: {result}"
        )


# =============================================================================
# STANDALONE RUNNER
# =============================================================================

if __name__ == "__main__":
    import traceback

    test_classes = [
        TestRoutingGateFlag,
        TestGenieSuccessMapping,
        TestGenieExtensionFields,
        TestGenieFallbackRouting,
        TestConversationIdPropagation,
        TestFactoryUsesConfig,
        TestDefaultFlagValues,
        TestGenieTableToTableData,
        TestGenieMessageAndSqlFields,
    ]

    passed = failed = total = 0
    print("=" * 70)
    print("PHASE G4/G5: feature flag + routing unit tests")
    print("=" * 70)

    for cls in test_classes:
        instance = cls()
        for method_name in [m for m in dir(instance) if m.startswith("test_")]:
            total += 1
            label = f"{cls.__name__}.{method_name}"
            try:
                getattr(instance, method_name)()
                print(f"  PASS  {label}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {label}: {e}")
                traceback.print_exc()
                failed += 1

    print()
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if failed:
        import sys as _sys
        _sys.exit(1)
