"""Unit tests for genie_prompt_enricher (Phase Q1).

All tests are pure-Python, no I/O.  Run with:
    python -m pytest tests/test_genie_prompt_enricher.py -v
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.services.genie_prompt_enricher import classify_intent, enrich_prompt, EnrichmentResult


# =============================================================================
# classify_intent — first-pass classification
# =============================================================================

class TestClassifyIntent:

    # --- DIRECT_LOOKUP ---
    def test_pure_numeric_id(self):
        assert classify_intent("42570415") == "DIRECT_LOOKUP"

    def test_short_numeric_not_lookup(self):
        # 6 digits with text is not a standalone ID
        assert classify_intent("show 12345 shipments") != "DIRECT_LOOKUP"

    def test_waybill_keyword(self):
        assert classify_intent("track waybill 123456789") == "DIRECT_LOOKUP"

    def test_hbl_keyword(self):
        assert classify_intent("find HBL ABCD123") == "DIRECT_LOOKUP"

    def test_hawb_keyword(self):
        assert classify_intent("where is hawb 9876543210") == "DIRECT_LOOKUP"

    def test_po_with_number(self):
        assert classify_intent("status for PO 4500123456") == "DIRECT_LOOKUP"

    def test_alpha_ref_code(self):
        assert classify_intent("look up SHP20240001") == "DIRECT_LOOKUP"

    def test_shipment_number_keyword(self):
        assert classify_intent("shipment number XYZ-20240101") == "DIRECT_LOOKUP"

    # --- DOWNLOAD_REQUEST ---
    def test_download_standalone(self):
        assert classify_intent("download") == "DOWNLOAD_REQUEST"

    def test_export_result(self):
        assert classify_intent("export this result") == "DOWNLOAD_REQUEST"

    def test_give_me_csv(self):
        assert classify_intent("give me the csv") == "DOWNLOAD_REQUEST"

    def test_export_all(self):
        assert classify_intent("export all data") == "DOWNLOAD_REQUEST"

    # --- SUMMARY_REQUEST ---
    def test_quick_summary(self):
        assert classify_intent("quick summary") == "SUMMARY_REQUEST"

    def test_summarize_this(self):
        assert classify_intent("summarize this") == "SUMMARY_REQUEST"

    def test_summarise_british(self):
        assert classify_intent("summarise the results") == "SUMMARY_REQUEST"

    def test_give_me_a_summary(self):
        assert classify_intent("give me a summary") == "SUMMARY_REQUEST"

    # --- DETAIL_LISTING ---
    def test_list_all_shipments(self):
        assert classify_intent("list all shipments from US") == "DETAIL_LISTING"

    def test_show_raw_records(self):
        assert classify_intent("show raw records for delayed shipments") == "DETAIL_LISTING"

    def test_full_list(self):
        assert classify_intent("give me the full list") == "DETAIL_LISTING"

    def test_all_shipments_from(self):
        assert classify_intent("all shipments from CN via air") == "DETAIL_LISTING"

    # --- BROAD_LISTING ---
    def test_shipments_from_country(self):
        assert classify_intent("shipments from Germany") == "BROAD_LISTING"

    def test_show_me_delayed(self):
        assert classify_intent("show me delayed shipments") == "BROAD_LISTING"

    def test_in_transit(self):
        assert classify_intent("in-transit shipments to Singapore") == "BROAD_LISTING"

    def test_pending_shipments(self):
        assert classify_intent("pending shipments from US to CN") == "BROAD_LISTING"

    def test_cancelled(self):
        assert classify_intent("cancelled shipments via ocean this month") == "BROAD_LISTING"

    # --- AGGREGATION ---
    def test_status_distribution(self):
        assert classify_intent("what is the shipment status distribution?") == "AGGREGATION"

    def test_top_10_lanes(self):
        assert classify_intent("top 10 lanes by volume") == "AGGREGATION"

    def test_how_many(self):
        assert classify_intent("how many shipments were delayed last week?") == "AGGREGATION"

    def test_revenue_by_bu(self):
        assert classify_intent("show revenue by BU") == "AGGREGATION"

    def test_compare_modes(self):
        assert classify_intent("compare air vs ocean performance") == "AGGREGATION"

    def test_breakdown_by_country(self):
        assert classify_intent("breakdown by country of origin") == "AGGREGATION"

    # --- GENERAL ---
    def test_general_question(self):
        assert classify_intent("which carriers have routes from Germany to Vietnam?") == "GENERAL"

    def test_general_short(self):
        assert classify_intent("which carriers operate from US?") == "GENERAL"


# =============================================================================
# enrich_prompt — structured return, template application
# =============================================================================

class TestEnrichPrompt:

    def test_returns_enrichment_result_type(self):
        result = enrich_prompt("shipments from Germany")
        assert isinstance(result, dict)
        assert {"original_prompt", "enriched_prompt", "intent",
                "enrichment_applied", "reason"} <= result.keys()

    def test_direct_lookup_not_enriched(self):
        result = enrich_prompt("42570415")
        assert result["enrichment_applied"] is False
        assert result["enriched_prompt"] == result["original_prompt"]
        assert result["intent"] == "DIRECT_LOOKUP"

    def test_download_not_enriched(self):
        result = enrich_prompt("export all data")
        assert result["enrichment_applied"] is False
        assert result["intent"] == "DOWNLOAD_REQUEST"

    def test_broad_listing_enriched(self):
        result = enrich_prompt("shipments from Germany")
        assert result["enrichment_applied"] is True
        assert result["intent"] == "BROAD_LISTING"
        assert "Summarise" in result["enriched_prompt"]
        assert "total count" in result["enriched_prompt"]
        assert "top 5 destinations" in result["enriched_prompt"]
        # original is preserved as-is
        assert result["original_prompt"] == "shipments from Germany"

    def test_broad_listing_includes_original_in_enriched(self):
        result = enrich_prompt("pending shipments from US to CN")
        assert result["original_prompt"] in result["enriched_prompt"]

    def test_aggregation_enriched(self):
        result = enrich_prompt("show revenue by BU")
        assert result["enrichment_applied"] is True
        assert result["intent"] == "AGGREGATION"
        assert "business analytics" in result["enriched_prompt"].lower() or \
               "aggregated" in result["enriched_prompt"].lower()

    def test_detail_listing_enriched(self):
        result = enrich_prompt("list all shipments from US")
        assert result["enrichment_applied"] is True
        assert result["intent"] == "DETAIL_LISTING"
        assert "shipment-level" in result["enriched_prompt"].lower()

    def test_summary_request_uses_template(self):
        result = enrich_prompt("quick summary")
        assert result["enrichment_applied"] is True
        assert result["intent"] == "SUMMARY_REQUEST"
        assert "summary" in result["enriched_prompt"].lower()

    def test_general_enriched(self):
        result = enrich_prompt("which carriers have routes from Germany to Vietnam?")
        assert result["enrichment_applied"] is True
        assert result["intent"] == "GENERAL"


# =============================================================================
# is_follow_up behaviour
# =============================================================================

class TestFollowUp:

    def test_broad_listing_followup_not_enriched(self):
        result = enrich_prompt("shipments from Germany", is_follow_up=True)
        assert result["enrichment_applied"] is False
        assert result["enriched_prompt"] == result["original_prompt"]

    def test_aggregation_followup_not_enriched(self):
        result = enrich_prompt("top 10 lanes", is_follow_up=True)
        assert result["enrichment_applied"] is False

    def test_general_followup_not_enriched(self):
        result = enrich_prompt("what about air mode?", is_follow_up=True)
        assert result["enrichment_applied"] is False

    def test_summary_request_followup_still_enriched(self):
        # SUMMARY_REQUEST is the one exception: always enriched
        result = enrich_prompt("quick summary", is_follow_up=True)
        assert result["enrichment_applied"] is True
        assert result["intent"] == "SUMMARY_REQUEST"

    def test_direct_lookup_followup_not_enriched(self):
        result = enrich_prompt("42570415", is_follow_up=True)
        assert result["enrichment_applied"] is False
        assert result["intent"] == "DIRECT_LOOKUP"

    def test_download_followup_not_enriched(self):
        result = enrich_prompt("download", is_follow_up=True)
        assert result["enrichment_applied"] is False
        assert result["intent"] == "DOWNLOAD_REQUEST"


# =============================================================================
# Edge cases
# =============================================================================

class TestEdgeCases:

    def test_empty_string_returns_general(self):
        result = enrich_prompt("")
        assert result["intent"] == "GENERAL"

    def test_whitespace_only_returns_general(self):
        result = enrich_prompt("   ")
        assert result["intent"] == "GENERAL"

    def test_original_never_mutated(self):
        msg = "  shipments from US  "  # leading/trailing spaces
        result = enrich_prompt(msg)
        # original_prompt is stripped
        assert result["original_prompt"] == "shipments from US"

    def test_enriched_prompt_never_empty(self):
        for msg in ["", "   ", "hello", "42570415"]:
            result = enrich_prompt(msg)
            assert result["enriched_prompt"].strip() != ""

    def test_reason_always_present(self):
        for msg in ["42570415", "shipments from US", "top 10 lanes", "quick summary"]:
            result = enrich_prompt(msg)
            assert result["reason"] != ""


# =============================================================================
# E6: Analytical vocabulary classification and enrichment
# =============================================================================

class TestE6AnalyticalClassification:
    """E6: classify_intent and enrich_prompt must handle analytical vocabulary."""

    # ----- classify_intent: core diagnostic prompts must be AGGREGATION -----

    def test_classify_analysis_phrase(self):
        assert classify_intent("give analysis on shipments via air and their delay") == "AGGREGATION"

    def test_classify_analyze(self):
        assert classify_intent("analyze air shipment delays") == "AGGREGATION"

    def test_classify_analyse_british(self):
        assert classify_intent("analyse air shipment delays") == "AGGREGATION"

    def test_classify_air_shipment_delay_analysis(self):
        assert classify_intent("air shipment delay analysis") == "AGGREGATION"

    def test_classify_insights(self):
        assert classify_intent("insights on delayed air shipments") == "AGGREGATION"

    def test_classify_insight_singular(self):
        assert classify_intent("give insight on delayed shipments") == "AGGREGATION"

    def test_classify_overview(self):
        assert classify_intent("overview of shipment delays by mode") == "AGGREGATION"

    def test_classify_report_on(self):
        assert classify_intent("report on shipment delays") == "AGGREGATION"

    def test_classify_how_delayed(self):
        assert classify_intent("how delayed are air shipments") == "AGGREGATION"

    def test_classify_examine(self):
        assert classify_intent("examine air shipment delays") == "AGGREGATION"

    def test_classify_assessment(self):
        assert classify_intent("assessment of delays for air transport") == "AGGREGATION"

    def test_classify_evaluation(self):
        assert classify_intent("evaluation of air shipment performance") == "AGGREGATION"

    # ----- enrich_prompt must use AGGREGATION template for these prompts -----

    def test_enrich_analysis_prompt_uses_aggregation_template(self):
        result = enrich_prompt("give analysis on shipments via air and their delay")
        assert result["intent"] == "AGGREGATION"
        ep = result["enriched_prompt"]
        assert "business analytics summary" in ep.lower() or "aggregat" in ep.lower()
        assert "avoid raw shipment-level rows" in ep.lower() or "do not return" in ep.lower()

    def test_enrich_analyze_uses_aggregation_template(self):
        result = enrich_prompt("analyze air shipment delays")
        assert result["intent"] == "AGGREGATION"
        assert result["enrichment_applied"] is True

    def test_enrich_insights_uses_aggregation_template(self):
        result = enrich_prompt("insights on delayed air shipments")
        assert result["intent"] == "AGGREGATION"
        assert result["enrichment_applied"] is True

    # ----- BROAD_LISTING template must not have sample-table escape hatch -----

    def test_broad_listing_template_hardened(self):
        """E6-revert: BROAD_LISTING template ends with status distribution.

        The 'Return aggregated results, not raw shipment rows.' phrase was
        removed because it blocked raw-row responses for legitimate listing
        prompts.  The 'sample table if useful' escape was removed (approved).
        P1 fix lives in _AGGREGATION_RE, not in the BROAD_LISTING template.
        """
        result = enrich_prompt("show shipments from US via air")
        assert result["intent"] == "BROAD_LISTING"
        ep = result["enriched_prompt"]
        assert "sample table if useful" not in ep
        # Over-corrected phrase must NOT be present
        assert "not raw shipment rows" not in ep
        assert "aggregated results" not in ep
        assert "status distribution." in ep  # clean end, no raw-row prohibition

    def test_broad_listing_summarise_still_fires(self):
        """BROAD_LISTING enrichment should still use the Summarise template."""
        result = enrich_prompt("show shipments from US")
        assert result["intent"] == "BROAD_LISTING"
        ep = result["enriched_prompt"]
        assert ep.lower().startswith("summarise")

    # ----- Regression: listing prompts must NOT be AGGREGATION -----

    def test_show_shipments_from_us_is_broad(self):
        assert classify_intent("show shipments from US") == "BROAD_LISTING"

    def test_list_all_shipments_is_detail(self):
        assert classify_intent("list all shipments from CN") == "DETAIL_LISTING"

    def test_delayed_shipments_is_broad(self):
        assert classify_intent("delayed shipments") == "BROAD_LISTING"

    # ----- Regression: existing AGGREGATION keywords still work -----

    def test_distribution_still_aggregation(self):
        assert classify_intent("shipment status distribution") == "AGGREGATION"

    def test_top_lanes_still_aggregation(self):
        assert classify_intent("top 10 lanes by volume") == "AGGREGATION"

    def test_how_many_still_aggregation(self):
        assert classify_intent("how many shipments are delayed") == "AGGREGATION"


# =============================================================================
# P1 fix: hard aggregation template wording (2026-07-16)
# =============================================================================

class TestP1TemplateHardWording:
    """P1 fix: _TMPL_AGGREGATION must use a hard 'Do not return individual
    shipment-level rows.' constraint, not the soft 'unless necessary' wording
    that gave Genie an escape hatch to return raw rows.

    Also verifies that the enricher and router aggregation templates are
    synchronized so no split-brain divergence can occur.
    """

    @pytest.mark.parametrize("prompt", [
        "give analysis on shipments via air and their delay",
        "analyze air shipment delays",
        "analyse air shipment delays",
        "air shipment delay analysis",
        "insights on delayed air shipments",
        "overview of shipment delays by mode",
        "report on shipment delays",
        "examine air shipment delays",
        "assessment of air shipment delays",
        "evaluation of shipment delay performance",
        "give me an analysis of air shipment delays",
    ])
    def test_aggregation_enriched_prompt_has_hard_no_raw_rows_contract(self, prompt):
        """Every analytical prompt routed as AGGREGATION must produce an
        enriched prompt that explicitly forbids raw shipment-level rows.
        """
        result = enrich_prompt(prompt)
        assert result["intent"] == "AGGREGATION", (
            f"Expected AGGREGATION for {prompt!r}, got {result['intent']!r}"
        )
        ep = result["enriched_prompt"]
        assert "do not return individual shipment-level rows" in ep.lower(), (
            f"Hard no-raw-rows contract missing from enriched prompt for {prompt!r}: {ep!r}"
        )
        assert "unless necessary" not in ep.lower(), (
            f"Soft 'unless necessary' escape found in enriched prompt for {prompt!r}: {ep!r}"
        )

    def test_listing_prompts_do_not_get_hard_aggregation_contract(self):
        """Genuine listing prompts must NOT have the hard aggregation contract;
        raw shipment rows remain a valid response.
        """
        for prompt in ["show shipments from US", "show me delayed shipments"]:
            result = enrich_prompt(prompt)
            ep = result["enriched_prompt"]
            assert "do not return individual shipment-level rows" not in ep.lower(), (
                f"Listing prompt {prompt!r} must not get hard agg contract: {ep!r}"
            )

    def test_router_and_enricher_aggregation_templates_synchronized(self):
        """Router _build_aggregation_prompt and enricher _TMPL_AGGREGATION must
        both use the hard no-raw-rows contract and both avoid soft wording.
        Ensures no split-brain divergence between the two code paths.
        """
        from app.services.pre_genie_router import _build_aggregation_prompt
        from app.services.genie_prompt_enricher import _TMPL_AGGREGATION

        test_q = "delay analysis for air shipments"
        router_ep  = _build_aggregation_prompt(test_q)
        enricher_ep = _TMPL_AGGREGATION.format(question=test_q)

        for label, ep in (("router", router_ep), ("enricher", enricher_ep)):
            assert "do not return individual shipment-level rows" in ep.lower(), (
                f"{label} aggregation template lacks hard no-raw-rows contract: {ep!r}"
            )
            assert "unless necessary" not in ep.lower(), (
                f"{label} aggregation template still has soft 'unless necessary': {ep!r}"
            )
