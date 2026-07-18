"""Unit tests for pre_genie_router (Phase E1 + E4-fix).

Pure-Python router tests. No Databricks access required.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pre_genie_router import route_pre_genie, _sanitize_local_response


_FORBIDDEN_WORDS = ["databricks", "genie", "claude", "openai", "model", "llm"]
# Table name must also not appear in identity responses
_FORBIDDEN_WORDS_EXTENDED = _FORBIDDEN_WORDS + ["lbn_with_scorecard"]


class TestIdentityRouting:
    @pytest.mark.parametrize(
        "prompt",
        [
            # Original prompts
            "Who built you?",
            "who made you",
            "who developed this app?",
            "what are you?",
            # Issue-1 additions: patterns that previously leaked to Genie
            "who are you",
            "who are you?",
            "Who Are You?",
            "what is this app",
            "what is this chatbot",
            "what is TransparencE",
            "what is transparence?",
            "tell me about yourself",
            "introduce yourself",
            "what can you do",
        ],
    )
    def test_identity_prompts_return_controlled_local_response(self, prompt):
        result = route_pre_genie(prompt)
        assert result["intent"] == "IDENTITY_OR_ABOUT_APP", (
            f"Expected IDENTITY_OR_ABOUT_APP for {prompt!r}, got {result['intent']!r}"
        )
        assert result["should_call_genie"] is False
        assert result["local_response"]
        assert "GLOG team" in result["local_response"]
        assert "TransparencE Shipment Intelligence" in result["local_response"]
        assert result["enriched_prompt"] is None

    @pytest.mark.parametrize(
        "prompt",
        [
            "Are you Genie?",
            "Are you Claude?",
            "Are you Databricks?",
            "Which model are you?",
            "What powers you?",
            # Issue-1 additions
            "who are you",
            "what is this app",
            "tell me about yourself",
        ],
    )
    def test_identity_prompts_do_not_expose_internal_backend_terms(self, prompt):
        result = route_pre_genie(prompt)
        assert result["intent"] == "IDENTITY_OR_ABOUT_APP"
        text = result["local_response"].lower()
        for word in _FORBIDDEN_WORDS_EXTENDED:
            assert word not in text, (
                f"Forbidden word {word!r} found in identity response for {prompt!r}: {text!r}"
            )

    def test_sanitizer_replaces_forbidden_terms_in_any_local_response(self):
        """Safety net: _sanitize_local_response replaces a contaminated response."""
        contaminated = "I am Genie, an AI built by Databricks."
        result = _sanitize_local_response(
            contaminated,
            "TransparencE Shipment Intelligence",
            "GLOG team",
        )
        assert "Genie" not in result
        assert "Databricks" not in result
        assert "GLOG team" in result
        assert "TransparencE Shipment Intelligence" in result

    def test_sanitizer_passes_clean_response_unchanged(self):
        clean = "This is the TransparencE Shipment Intelligence application developed by the GLOG team."
        result = _sanitize_local_response(
            clean,
            "TransparencE Shipment Intelligence",
            "GLOG team",
        )
        assert result == clean

    def test_route_pre_genie_applies_sanitizer_via_wrapper(self):
        """The public route_pre_genie() wrapper sanitizes even if a future
        code path accidentally produces a forbidden term in a local response."""
        # We can't inject a forbidden term through normal routing,
        # so we verify the sanitizer is wired by checking the known identity
        # prompts return clean responses.
        for prompt in ["who are you", "what are you", "are you Genie?"]:
            result = route_pre_genie(prompt)
            assert result["should_call_genie"] is False
            text = result["local_response"].lower()
            for word in _FORBIDDEN_WORDS_EXTENDED:
                assert word not in text, (
                    f"Forbidden word {word!r} in sanitized response for {prompt!r}"
                )


class TestGreetingRouting:
    @pytest.mark.parametrize(
        "prompt",
        ["hi", "hello", "hola", "hey", "good morning", "thanks", "thank you"],
    )
    def test_greetings_are_handled_locally(self, prompt):
        result = route_pre_genie(prompt)
        assert result["intent"] == "GREETING"
        assert result["should_call_genie"] is False
        assert result["local_response"]
        assert result["enriched_prompt"] is None
        assert result["entities"] == []
        assert result["filters"] == {}


class TestDownloadRouting:
    def test_download_request_uses_previous_download_key(self):
        result = route_pre_genie("download this result", last_download_key="abc-123")
        assert result["intent"] == "DOWNLOAD_REQUEST"
        assert result["should_call_genie"] is False
        assert result["download_key"] == "abc-123"
        assert result["download_url"] == "/api/download/abc-123"
        assert "download" in result["local_response"].lower()

    def test_download_request_without_previous_result_asks_user_to_run_query(self):
        result = route_pre_genie("export this")
        assert result["intent"] == "DOWNLOAD_REQUEST"
        assert result["should_call_genie"] is False
        assert result["download_key"] is None
        assert "run a shipment or table query first" in result["local_response"].lower()

    # ------------------------------------------------------------------
    # Issue-2 regression tests: export_row_count must propagate through
    # ------------------------------------------------------------------

    def test_download_carries_export_row_count_from_previous_table_result(self):
        """Issue 2: after table query + quick summary, download must return
        the row count from the table query, not 0."""
        result = route_pre_genie(
            "download this result",
            last_download_key="key-xyz",
            last_export_id="exp-abc",
            last_export_status="ready",
            last_export_mode="returned_rows_only",
            last_export_row_count=347,
        )
        assert result["intent"] == "DOWNLOAD_REQUEST"
        assert result["should_call_genie"] is False
        assert result["download_key"] == "key-xyz"
        assert result["export_row_count"] == 347, (
            f"Expected export_row_count=347, got {result['export_row_count']}"
        )

    def test_download_with_no_prior_table_has_no_row_count(self):
        """If the user never ran a table query, export_row_count is None."""
        result = route_pre_genie("download this result")
        assert result["intent"] == "DOWNLOAD_REQUEST"
        assert result["should_call_genie"] is False
        assert result["download_key"] is None
        assert result["export_row_count"] is None

    def test_download_after_greeting_asks_for_table_query(self):
        """B: hi → download this result → must ask to run a table query."""
        result = route_pre_genie(
            "download this result",
            # No last_download_key, no export_id — simulates post-greeting state
        )
        assert result["intent"] == "DOWNLOAD_REQUEST"
        assert result["should_call_genie"] is False
        assert result["download_key"] is None
        assert "run a shipment or table query first" in result["local_response"].lower()

    def test_download_after_identity_response_asks_for_table_query(self):
        """C: who built you → download this result → must ask to run a table query."""
        result = route_pre_genie(
            "download this result",
            # No export state — identity responses don't produce downloads
        )
        assert result["intent"] == "DOWNLOAD_REQUEST"
        assert result["should_call_genie"] is False
        assert result["download_key"] is None
        assert "run a shipment or table query first" in result["local_response"].lower()

    def test_download_while_export_running_carries_row_count(self):
        """Export still preparing — row count should still be available."""
        result = route_pre_genie(
            "export this",
            last_export_id="exp-queued",
            last_export_status="running",
            last_export_mode="returned_rows_only",
            last_export_row_count=500,
        )
        assert result["intent"] == "DOWNLOAD_REQUEST"
        assert result["should_call_genie"] is False
        assert result["export_status"] == "running"
        assert result["export_row_count"] == 500

    def test_download_export_row_count_none_when_not_provided(self):
        """export_row_count defaults to None if not in session context."""
        result = route_pre_genie("download this result", last_download_key="k1")
        assert result["export_row_count"] is None

    def test_download_export_status_set_to_ready_when_key_present(self):
        """When download_key is present, export_status is forced to ready."""
        result = route_pre_genie(
            "download this result",
            last_download_key="k2",
            last_export_status="queued",   # stale status should be overridden
            last_export_row_count=120,
        )
        assert result["export_status"] == "ready"
        assert result["export_row_count"] == 120


class TestCorrectionRouting:
    def test_correction_with_previous_entities_rewrites_as_part_number_lookup(self):
        result = route_pre_genie(
            "these are part numbers, not shipment ids",
            is_follow_up=True,
            previous_entities=["363097-000", "NB15524001", "605705-31"],
            previous_entity_type="shipment_id",
            previous_filters={"destination": "US"},
        )
        assert result["intent"] == "CORRECTION"
        assert result["is_correction"] is True
        assert result["should_call_genie"] is True
        assert result["entity_type"] == "part_number"
        assert result["entities"] == ["363097-000", "NB15524001", "605705-31"]
        assert "part_number" in result["enriched_prompt"]
        assert "destination is US" in result["enriched_prompt"]
        assert "shipment IDs" not in result["enriched_prompt"]

    def test_correction_without_previous_entities_asks_user_to_resend_values(self):
        result = route_pre_genie("no, those are part numbers")
        assert result["intent"] == "CORRECTION"
        assert result["should_call_genie"] is False
        assert "resend the values" in result["local_response"].lower()


# =============================================================================
# E4-fix2: NON_BUSINESS_OR_SMALL_TALK guardrail
# =============================================================================


class TestNonBusinessRouting:
    """Verify that the NON_BUSINESS_OR_SMALL_TALK tier blocks low-information
    prompts before they reach Genie, while allowing genuine short business
    queries through."""

    @pytest.mark.parametrize(
        "prompt",
        [
            "bro",
            "bruh",
            "hmm",
            "test",
            "???",
            "what",
            "why",
            "yes",
            "no",
            "good",
            "fine",
            "how are you",
            "hey bro",
            "hows you",
        ],
    )
    def test_low_info_prompts_return_non_business_local_response(self, prompt):
        """Low-information / small-talk prompts must NOT call Genie."""
        result = route_pre_genie(prompt)
        assert result["intent"] == "NON_BUSINESS_OR_SMALL_TALK", (
            f"Expected NON_BUSINESS_OR_SMALL_TALK for {prompt!r}, got {result['intent']!r}"
        )
        assert result["should_call_genie"] is False
        assert result["local_response"]
        assert "shipment" in result["local_response"].lower()
        assert result["enriched_prompt"] is None
        assert result["download_key"] is None

    @pytest.mark.parametrize(
        "prompt",
        [
            "delayed?",
            "in transit?",
            "by destination",
            "by status",
            "air",
            "ocean",
            "revenue",
            "top lanes",
            "ETA",
            "status",
            "only delayed ones",
            "which are in transit?",
            "show by destination",
            "what about air?",
        ],
    )
    def test_short_business_prompts_are_not_blocked_by_non_business_guard(self, prompt):
        """Short prompts containing at least one logistics term must reach Genie."""
        result = route_pre_genie(prompt)
        assert result["intent"] != "NON_BUSINESS_OR_SMALL_TALK", (
            f"Expected Genie-bound intent for {prompt!r}, got {result['intent']!r}"
        )
        assert result["should_call_genie"] is True

    def test_long_prompt_without_domain_keyword_is_not_blocked(self):
        """Prompts over 40 characters are allowed through even without a domain keyword."""
        result = route_pre_genie("can you show me what happened recently in the system")
        assert result["intent"] != "NON_BUSINESS_OR_SMALL_TALK"
        assert result["should_call_genie"] is True

    def test_prompt_with_entity_value_is_not_blocked(self):
        """A prompt containing an entity reference bypasses the low-info guard."""
        result = route_pre_genie("363097-000")
        assert result["intent"] != "NON_BUSINESS_OR_SMALL_TALK"
        assert result["should_call_genie"] is True

    def test_non_business_response_does_not_expose_forbidden_terms(self):
        """The clarification response must not leak internal backend terms."""
        result = route_pre_genie("bro")
        text = result["local_response"].lower()
        for word in ["databricks", "genie", "claude", "openai", "llm", "lbn_with_scorecard"]:
            assert word not in text, (
                f"Forbidden word {word!r} found in NON_BUSINESS response: {text!r}"
            )

    def test_non_business_carries_no_export_fields(self):
        """Low-info prompt must not produce a download_key, export_id, or row count."""
        result = route_pre_genie("bro")
        assert result["download_key"] is None
        assert result["export_id"] is None
        assert result["export_status"] is None
        assert result["export_row_count"] is None


# =============================================================================
# Typed-download async export bug tests (Option D / Part 2)
#
# These tests verify the live ExportJobManager fallback in
# _handle_download_request when latest_table_result is stale (queued/running)
# but the background thread has already completed.
# =============================================================================


class TestDownloadWithExportJobManager:
    """Part 2: live ExportJobManager fallback for stale latest_table_result."""

    def _make_manager(self):
        """Import locally to avoid module-level import issues."""
        import os, sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from app.services.export_job_manager import ExportJobManager
        return ExportJobManager(ttl_hours=24)

    def test_queued_ltr_with_ejm_ready_returns_download(self):
        """Bug scenario: latest_table_result says queued, but EJM says ready.
        Expected: return the ready download immediately, NOT 'still preparing'."""
        from app.services.export_job_manager import ExportJobManager
        manager = ExportJobManager(ttl_hours=24)
        job = manager.create_job(app_conversation_id="conv-1", mode="returned_rows_only")
        manager.mark_running(job.export_id)
        manager.mark_ready(
            job.export_id,
            file_path="/tmp/test.csv",
            download_key="live-key-abc",
            row_count=250,
        )

        result = route_pre_genie(
            "i want to download this data",
            latest_table_result={
                "download_key": None,
                "export_id": job.export_id,
                "export_status": "queued",
                "export_mode": "returned_rows_only",
                "export_row_count": None,
                "query_description": "US shipments",
            },
            export_job_manager=manager,
        )

        assert result["intent"] == "DOWNLOAD_REQUEST"
        assert result["should_call_genie"] is False
        assert result["download_key"] == "live-key-abc", (
            f"Expected live download_key, got {result['download_key']!r}. "
            f"Message: {result['local_response']!r}"
        )
        assert result["export_status"] == "ready"
        assert result["export_row_count"] == 250
        assert "ready" in result["local_response"].lower()
        assert "preparing" not in result["local_response"].lower()

    def test_running_ltr_with_ejm_ready_returns_download(self):
        """Same as above but ltr.export_status='running' instead of 'queued'."""
        from app.services.export_job_manager import ExportJobManager
        manager = ExportJobManager(ttl_hours=24)
        job = manager.create_job(app_conversation_id="conv-2", mode="returned_rows_only")
        manager.mark_running(job.export_id)
        manager.mark_ready(
            job.export_id,
            file_path="/tmp/test2.csv",
            download_key="live-key-def",
            row_count=100,
        )

        result = route_pre_genie(
            "download this",
            latest_table_result={
                "download_key": None,
                "export_id": job.export_id,
                "export_status": "running",
                "export_mode": "returned_rows_only",
                "export_row_count": None,
                "query_description": None,
            },
            export_job_manager=manager,
        )

        assert result["download_key"] == "live-key-def"
        assert result["export_status"] == "ready"
        assert result["export_row_count"] == 100

    def test_queued_ltr_with_ejm_still_running_says_preparing(self):
        """latest_table_result says queued, EJM also still running.
        Expected: still preparing message."""
        from app.services.export_job_manager import ExportJobManager
        manager = ExportJobManager(ttl_hours=24)
        job = manager.create_job(app_conversation_id="conv-3", mode="returned_rows_only")
        manager.mark_running(job.export_id)  # still running, not ready

        result = route_pre_genie(
            "download this data",
            latest_table_result={
                "download_key": None,
                "export_id": job.export_id,
                "export_status": "queued",
                "export_mode": "returned_rows_only",
                "export_row_count": None,
                "query_description": None,
            },
            export_job_manager=manager,
        )

        assert result["should_call_genie"] is False
        assert result["download_key"] is None
        assert "prepared" in result["local_response"].lower() or "preparing" in result["local_response"].lower(), (
            f"Expected preparing/prepared in response, got: {result['local_response']!r}"
        )

    def test_queued_ltr_with_ejm_failed_says_failed(self):
        """latest_table_result says queued, but EJM shows failed.
        Expected: export failed message."""
        from app.services.export_job_manager import ExportJobManager
        manager = ExportJobManager(ttl_hours=24)
        job = manager.create_job(app_conversation_id="conv-4", mode="returned_rows_only")
        manager.mark_failed(job.export_id, "disk quota exceeded")

        result = route_pre_genie(
            "export this",
            latest_table_result={
                "download_key": None,
                "export_id": job.export_id,
                "export_status": "queued",
                "export_mode": "returned_rows_only",
                "export_row_count": None,
                "query_description": None,
            },
            export_job_manager=manager,
        )

        assert result["should_call_genie"] is False
        assert result["download_key"] is None
        assert result["export_status"] == "failed"
        assert "failed" in result["local_response"].lower(), (
            f"Expected 'failed' in response, got: {result['local_response']!r}"
        )

    def test_queued_ltr_without_export_job_manager_falls_back_to_preparing(self):
        """When export_job_manager is None, stale queued LTR returns still-preparing.
        Grid-level polling behavior remains unchanged."""
        result = route_pre_genie(
            "download",
            latest_table_result={
                "download_key": None,
                "export_id": "exp-orphan",
                "export_status": "queued",
                "export_mode": "returned_rows_only",
                "export_row_count": None,
                "query_description": None,
            },
            export_job_manager=None,
        )

        assert result["should_call_genie"] is False
        assert result["download_key"] is None
        assert "prepared" in result["local_response"].lower() or "preparing" in result["local_response"].lower()

    def test_ready_ltr_with_download_key_returned_immediately_no_ejm_check(self):
        """If latest_table_result already has download_key (ready), serve it
        without touching ExportJobManager at all."""
        from app.services.export_job_manager import ExportJobManager
        manager = ExportJobManager(ttl_hours=24)
        # Create a job but deliberately do NOT mark it ready — ltr has the key
        job = manager.create_job(app_conversation_id="conv-6", mode="returned_rows_only")

        result = route_pre_genie(
            "i want to download this data",
            latest_table_result={
                "download_key": "already-ready-key",
                "export_id": job.export_id,
                "export_status": "ready",
                "export_mode": "returned_rows_only",
                "export_row_count": 75,
                "query_description": "some query",
            },
            export_job_manager=manager,
        )

        assert result["download_key"] == "already-ready-key"
        assert result["export_status"] == "ready"
        assert result["export_row_count"] == 75

    def test_low_info_after_prior_business_context_still_blocked(self):
        """'bro' following a real conversation still gets the clarification response."""
        result = route_pre_genie(
            "bro",
            is_follow_up=True,
            previous_intent="BROAD_LISTING",
            previous_entities=["NB15524001"],
        )
        assert result["intent"] == "NON_BUSINESS_OR_SMALL_TALK"
        assert result["should_call_genie"] is False


class TestExplicitEntitySearch:
    def test_part_number_keyword_overrides_generic_alphanumeric_detection(self):
        result = route_pre_genie(
            "i need the shipments for these part numbers which are going to US - 363097-000, NB15524001, 605705-31"
        )
        assert result["intent"] == "EXPLICIT_ENTITY_SEARCH"
        assert result["entity_type"] == "part_number"
        assert result["should_call_genie"] is True
        assert result["entities"] == ["363097-000", "NB15524001", "605705-31"]
        assert result["filters"]["destination"] == "US"
        assert "part_number" in result["enriched_prompt"]
        assert "shipment number" in result["enriched_prompt"].lower()

    def test_material_number_maps_to_part_number_entity_type(self):
        result = route_pre_genie("find shipments for material number NB19684001")
        assert result["intent"] == "EXPLICIT_ENTITY_SEARCH"
        assert result["entity_type"] == "part_number"
        assert result["entities"] == ["NB19684001"]

    def test_shipment_id_keyword_routes_as_explicit_entity_search(self):
        result = route_pre_genie("shipment number 42570415")
        assert result["intent"] == "EXPLICIT_ENTITY_SEARCH"
        assert result["entity_type"] == "shipment_id"
        assert result["entities"] == ["42570415"]
        assert "shipment_number_id" in result["enriched_prompt"]


class TestDirectLookup:
    def test_plain_numeric_lookup_still_works(self):
        result = route_pre_genie("42570415")
        assert result["intent"] == "DIRECT_LOOKUP"
        assert result["should_call_genie"] is True
        assert result["enriched_prompt"] == "42570415"


class TestTrueFollowUp:
    @pytest.mark.parametrize(
        "prompt",
        [
            "which are in transit?",
            "what about air?",
            "only delayed ones",
            "quick summary",
            "show by destination",
            "show the same by status",
            "summarize this",
        ],
    )
    def test_true_follow_up_prompts_preserve_context(self, prompt):
        result = route_pre_genie(prompt, is_follow_up=True, previous_intent="BROAD_LISTING")
        assert result["intent"] == "TRUE_FOLLOW_UP"
        assert result["should_call_genie"] is True

    @pytest.mark.parametrize(
        "prompt, expected_intent",
        [
            ("shipment status distribution", "AGGREGATION"),
            ("top 10 shipments by revenue", "AGGREGATION"),
            ("busiest lanes", "AGGREGATION"),
            ("shipments from Mexico", "BROAD_LISTING"),
            ("total revenue by business unit", "AGGREGATION"),
        ],
    )
    def test_new_standalone_business_query_in_same_chat_is_not_forced_into_true_follow_up(
        self, prompt, expected_intent
    ):
        result = route_pre_genie(prompt, is_follow_up=True, previous_intent="BROAD_LISTING")
        assert result["intent"] == expected_intent
        assert result["should_call_genie"] is True


class TestRouterBehavior:
    def test_router_returns_required_structure(self):
        result = route_pre_genie("shipments from US via air")
        assert {
            "original_prompt",
            "normalized_prompt",
            "intent",
            "entity_type",
            "entities",
            "filters",
            "is_follow_up",
            "is_correction",
            "should_call_genie",
            "local_response",
            "enriched_prompt",
            "reason",
        } <= set(result.keys())

    def test_router_returns_export_row_count_field(self):
        """export_row_count must always be present in the decision dict."""
        result = route_pre_genie("shipments from US via air")
        assert "export_row_count" in result

    def test_safety_routing_still_works_when_prompt_enrichment_disabled(self):
        result = route_pre_genie("Who built you?", enable_prompt_enrichment=False)
        assert result["intent"] == "IDENTITY_OR_ABOUT_APP"
        assert result["should_call_genie"] is False

    def test_explicit_entity_search_still_produces_safe_prompt_when_enrichment_disabled(self):
        result = route_pre_genie(
            "use part numbers NB15524001, NB19684001 going to US",
            enable_prompt_enrichment=False,
        )
        assert result["intent"] == "EXPLICIT_ENTITY_SEARCH"
        assert result["entity_type"] == "part_number"
        assert result["should_call_genie"] is True
        assert "part_number" in result["enriched_prompt"]
        assert "destination is US" in result["enriched_prompt"]


# =============================================================================
# E6: Analytical intent vocabulary routing (analysis / insights / overview …)
# =============================================================================

class TestE6AnalyticalRouting:
    """E6: Prompts containing analytical vocabulary must route as AGGREGATION,
    not BROAD_LISTING, so Genie receives the stronger analytics-framing template
    and shape-validation retry is available.
    """

    # ----- Core phrases from the P1 diagnosis -----

    def test_give_analysis_on_air_delay_is_aggregation(self):
        result = route_pre_genie(
            "give analysis on shipments via air and their delay"
        )
        assert result["intent"] == "AGGREGATION", result["intent"]
        assert result["should_call_genie"] is True

    def test_analyze_air_delay_is_aggregation(self):
        result = route_pre_genie("analyze air shipment delays")
        assert result["intent"] == "AGGREGATION"

    def test_analyse_spelling_is_aggregation(self):
        result = route_pre_genie("analyse air shipment delays")
        assert result["intent"] == "AGGREGATION"

    def test_air_shipment_delay_analysis_is_aggregation(self):
        result = route_pre_genie("air shipment delay analysis")
        assert result["intent"] == "AGGREGATION"

    def test_delay_analysis_for_air_is_aggregation(self):
        result = route_pre_genie("delay analysis for air shipments")
        assert result["intent"] == "AGGREGATION"

    def test_insights_on_delayed_air_shipments_is_aggregation(self):
        result = route_pre_genie("insights on delayed air shipments")
        assert result["intent"] == "AGGREGATION"

    def test_give_insights_on_delays_is_aggregation(self):
        result = route_pre_genie("give insights on shipment delays")
        assert result["intent"] == "AGGREGATION"

    def test_overview_of_delays_by_mode_is_aggregation(self):
        result = route_pre_genie("overview of shipment delays by mode")
        assert result["intent"] == "AGGREGATION"

    def test_report_on_shipment_delays_is_aggregation(self):
        result = route_pre_genie("report on shipment delays")
        assert result["intent"] == "AGGREGATION"

    def test_how_delayed_are_air_shipments_is_aggregation(self):
        result = route_pre_genie("how delayed are air shipments")
        assert result["intent"] == "AGGREGATION"

    def test_examine_air_shipment_delays_is_aggregation(self):
        result = route_pre_genie("examine air shipment delays")
        assert result["intent"] == "AGGREGATION"

    def test_give_me_analysis_of_delays_is_aggregation(self):
        result = route_pre_genie("give me analysis of air shipment delays")
        assert result["intent"] == "AGGREGATION"

    def test_overview_is_aggregation(self):
        result = route_pre_genie("overview of shipment delays")
        assert result["intent"] == "AGGREGATION"

    # ----- Enriched prompt must use AGGREGATION template, not BROAD_LISTING -----

    def test_analytical_prompt_enrichment_uses_aggregation_template(self):
        result = route_pre_genie(
            "give analysis on shipments via air and their delay",
            enable_prompt_enrichment=True,
        )
        ep = result["enriched_prompt"] or ""
        # AGGREGATION template contains "business analytics summary"
        assert "business analytics summary" in ep.lower() or "aggregat" in ep.lower()
        # Must NOT use the BROAD_LISTING "Summarise X: total count" template prefix
        assert not ep.startswith("Summarise give analysis")

    def test_aggregation_enrichment_has_hard_no_raw_rows_instruction(self):
        """AGGREGATION template must use the hard 'Do not return individual
        shipment-level rows.' contract (P1 fix).
        The soft 'avoid raw shipment-level rows unless necessary' phrase must
        NOT appear — it gave Genie an escape hatch to return raw rows.
        """
        result = route_pre_genie("analyze air shipment delays")
        ep = result["enriched_prompt"] or ""
        # Hard constraint must be present
        assert "do not return individual shipment-level rows" in ep.lower(), (
            f"AGGREGATION enriched prompt must contain hard no-raw-rows contract: {ep!r}"
        )
        # Soft escape hatch must be absent
        assert "unless necessary" not in ep.lower(), (
            f"AGGREGATION enriched prompt must not contain soft 'unless necessary': {ep!r}"
        )

    # ----- BROAD_LISTING template must no longer have raw-rows escape hatch -----

    def test_broad_listing_template_no_longer_allows_sample_table(self):
        """_build_broad_prompt must not contain 'sample table if useful'.

        E6-revert: the template ends cleanly with 'status distribution.'
        The 'Return aggregated results, not raw shipment rows.' phrase was
        removed because it over-corrected listing prompts (show me delayed
        shipments, show shipments from China) that are expected to return
        shipment-level rows.  The P1 fix is in _AGGREGATION_RE, not here.
        """
        result = route_pre_genie(
            "show shipments from US via air",
            enable_prompt_enrichment=True,
        )
        ep = result["enriched_prompt"] or ""
        assert "sample table if useful" not in ep
        # No "Return aggregated results" — that phrase over-corrects listing prompts
        assert "not raw shipment rows" not in ep
        assert "aggregated results" not in ep
        assert "status distribution." in ep  # clean end, no raw-row prohibition

    # ----- Regression: listing prompts must NOT be promoted to AGGREGATION -----

    def test_show_shipments_from_us_stays_broad_listing(self):
        result = route_pre_genie("show shipments from US")
        assert result["intent"] == "BROAD_LISTING"

    def test_list_air_shipments_stays_detail_listing(self):
        result = route_pre_genie("list all air shipments")
        # Should be DETAIL_LISTING (matches "list all")
        assert result["intent"] in ("DETAIL_LISTING", "BROAD_LISTING")
        assert result["intent"] != "AGGREGATION"

    def test_delayed_shipments_stays_broad_listing(self):
        result = route_pre_genie("delayed shipments from CN")
        assert result["intent"] == "BROAD_LISTING"

    def test_in_transit_shipments_stays_broad_listing(self):
        result = route_pre_genie("in-transit shipments")
        assert result["intent"] == "BROAD_LISTING"


# =============================================================================
# E6-REVERT REGRESSION: listing prompts must allow raw shipment rows
# =============================================================================

class TestListingPromptsRawRowSafety:
    """Explicit regression guard for the four specific listing prompts requested
    after the E6 BROAD_LISTING template over-correction was identified and fixed.

    Guarantees:
    1. None of the prompts is classified as AGGREGATION.
    2. For prompts that hit BROAD_LISTING, the enriched template does NOT contain
       the over-corrected phrase 'Return aggregated results, not raw shipment rows.'
    3. The BROAD_LISTING template ends cleanly at 'status distribution.' so Genie
       is free to return shipment-level rows when appropriate.
    """

    # ── classification checks ──────────────────────────────────────────────

    def test_show_me_delayed_shipments_not_aggregation(self):
        result = route_pre_genie("show me delayed shipments")
        assert result["intent"] != "AGGREGATION", (
            "'show me delayed shipments' must stay BROAD_LISTING/GENERAL, not AGGREGATION"
        )

    def test_show_shipments_from_china_not_aggregation(self):
        result = route_pre_genie("show shipments from China")
        assert result["intent"] != "AGGREGATION", (
            "'show shipments from China' must stay BROAD_LISTING/GENERAL, not AGGREGATION"
        )

    def test_list_air_shipments_not_aggregation(self):
        result = route_pre_genie("list air shipments")
        assert result["intent"] != "AGGREGATION", (
            "'list air shipments' must stay DETAIL_LISTING/GENERAL, not AGGREGATION"
        )

    def test_which_shipments_in_transit_not_aggregation(self):
        result = route_pre_genie("which shipments are in transit?")
        assert result["intent"] != "AGGREGATION", (
            "'which shipments are in transit?' must stay BROAD_LISTING/GENERAL, not AGGREGATION"
        )

    # ── template content checks (only for BROAD_LISTING routes) ──────────

    def test_show_me_delayed_shipments_template_does_not_block_raw_rows(self):
        """BROAD_LISTING template for a delayed-shipments query must not contain
        the raw-row prohibition that over-corrects listing behaviour."""
        result = route_pre_genie("show me delayed shipments", enable_prompt_enrichment=True)
        ep = result.get("enriched_prompt") or ""
        if result["intent"] == "BROAD_LISTING":
            assert "not raw shipment rows" not in ep, (
                "BROAD_LISTING template must not block raw rows: " + ep
            )
            assert "Return aggregated results" not in ep, (
                "BROAD_LISTING template must not force aggregation: " + ep
            )
            assert "status distribution." in ep, (
                "BROAD_LISTING template must end with 'status distribution.': " + ep
            )

    def test_show_shipments_from_china_template_does_not_block_raw_rows(self):
        result = route_pre_genie("show shipments from China", enable_prompt_enrichment=True)
        ep = result.get("enriched_prompt") or ""
        if result["intent"] == "BROAD_LISTING":
            assert "not raw shipment rows" not in ep
            assert "Return aggregated results" not in ep
            assert "status distribution." in ep


# =============================================================================
# P1 regression: summary vocabulary routing fix (2026-07-16)
# =============================================================================

class TestSummaryRoutingP1Fix:
    """P1: summarise/summarize/summary prompts with topics must route as AGGREGATION,
    not BROAD_LISTING or TRUE_FOLLOW_UP."""

    @pytest.mark.parametrize("prompt", [
        "summarise the shipments from US",
        "summarize the shipments from US",
        "give me a summary of shipments from US",
        "summary of shipments from US",
        "summarise delayed shipments",
        "summarize air shipments",
        "summarise the current result by destination and status",
    ])
    def test_summary_topic_prompts_route_aggregation(self, prompt):
        """Standalone summary prompts with a topic must classify as AGGREGATION."""
        result = route_pre_genie(prompt, is_follow_up=False)
        assert result["intent"] in ("AGGREGATION", "SUMMARY_REQUEST"), (
            f"Expected AGGREGATION or SUMMARY_REQUEST for {prompt!r}, "
            f"got {result['intent']!r} — must not be BROAD_LISTING"
        )
        assert result["intent"] != "BROAD_LISTING", (
            f"Got BROAD_LISTING for {prompt!r} — P1 routing fix not applied"
        )

    @pytest.mark.parametrize("prompt", [
        "summarise the shipments from US",
        "summarize the shipments from US",
        "give me a summary of shipments from US",
        "summary of shipments from US",
    ])
    def test_summary_topic_prompts_not_true_follow_up_when_in_session(self, prompt):
        """In-session summary prompts with a topic must NOT classify as TRUE_FOLLOW_UP."""
        result = route_pre_genie(prompt, is_follow_up=True, previous_intent="BROAD_LISTING")
        assert result["intent"] != "TRUE_FOLLOW_UP", (
            f"Got TRUE_FOLLOW_UP for {prompt!r} with is_follow_up=True — "
            f"this is the post-idle regression bug; P1 fix should prevent it"
        )
        assert result["intent"] in ("AGGREGATION", "SUMMARY_REQUEST"), (
            f"Expected AGGREGATION or SUMMARY_REQUEST for {prompt!r}, "
            f"got {result['intent']!r}"
        )

    @pytest.mark.parametrize("prompt", [
        "summarise this",
        "summarize this",
    ])
    def test_context_only_summaries_remain_true_follow_up(self, prompt):
        """Context-dependent summaries ('summarise this') must remain TRUE_FOLLOW_UP."""
        result = route_pre_genie(prompt, is_follow_up=True, previous_intent="BROAD_LISTING")
        assert result["intent"] == "TRUE_FOLLOW_UP", (
            f"Expected TRUE_FOLLOW_UP for {prompt!r} with is_follow_up=True, "
            f"got {result['intent']!r}"
        )

    def test_summary_enriched_prompt_no_double_prefix(self):
        """Enriched prompt must not start with 'Summarise summarise ...'."""
        result = route_pre_genie("summarise the shipments from US", is_follow_up=False)
        ep = result.get("enriched_prompt") or ""
        ep_lower = ep.lower()
        assert not ep_lower.startswith("summarise summarise"), (
            f"Double 'Summarise summarise' prefix found in enriched_prompt: {ep!r}"
        )
        assert not ep_lower.startswith("summarize summarize"), (
            f"Double 'Summarize summarize' prefix found in enriched_prompt: {ep!r}"
        )
