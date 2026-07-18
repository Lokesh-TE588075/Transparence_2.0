"""P1 regression tests for result_shape_validator.py (2026-07-16).

Covers:
  1. has_aggregation_metrics() false-positive fix — "amount" token removed
  2. Raw measure columns correctly classified via _RAW_SHIPMENT_SIGNALS
  3. Valid aggregation schemas still pass (no false negatives)
  4. is_analytical_shape_mismatch() fires correctly for all relevant intents
  5. SUMMARY_REQUEST intent coverage in shape validation

Raw browser schema used as the canonical regression fixture:
  [shipment_number_id, part_number, source_, destination,
   transportation_mode_desc, execution_status, business_unit_id,
   actual_pgi_date, eta, ata, final_gr_date,
   sales_functional_currency_amount, chargeable_weight, currency_code]
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.result_shape_validator import (
    has_aggregation_metrics,
    has_raw_shipment_signals,
    has_analytical_vocabulary,
    is_aggregation_shape_mismatch,
    is_analytical_shape_mismatch,
    build_retry_prompt,
)

# ─── Canonical raw browser schema (exactly as returned by Genie for US shipments)
_RAW_BROWSER_SCHEMA = [
    "shipment_number_id",
    "part_number",
    "source_",
    "destination",
    "transportation_mode_desc",
    "execution_status",
    "business_unit_id",
    "actual_pgi_date",
    "eta",
    "ata",
    "final_gr_date",
    "sales_functional_currency_amount",
    "chargeable_weight",
    "currency_code",
]

# ─── Valid aggregation schemas
_REVENUE_AGG_SCHEMA  = ["business_unit_id", "total_revenue", "shipment_count"]
_LANE_AGG_SCHEMA     = ["source_", "destination", "lane", "shipment_count"]
_STATUS_AGG_SCHEMA   = ["execution_status", "shipment_count"]
_SIMPLE_COUNT_SCHEMA = ["destination", "count"]


# =============================================================================
# Part 1 — has_aggregation_metrics() P1 false-positive fix
# =============================================================================

class TestHasAggregationMetricsFix:
    """P1: raw per-shipment measure columns must NOT be treated as agg metrics."""

    @pytest.mark.parametrize("col", [
        "sales_functional_currency_amount",  # was triggering via "amount" token
        "sales_budget_rate_amount",           # same token
        "shipment_quantity",
        "chargeable_weight",
        "actual_weight",
    ])
    def test_raw_measure_column_returns_false(self, col):
        assert has_aggregation_metrics([col]) is False, (
            f"has_aggregation_metrics([{col!r}]) should be False — "
            f"it is a raw per-shipment measure, not an aggregation output"
        )

    @pytest.mark.parametrize("col", [
        "total_revenue",
        "shipment_count",
        "total_sales_functional_currency_amount",  # has "total" token -> still True
        "total_sales_amount",
        "avg_delay",
        "average_delay",
        # delay_percentage: "percentage" token not in _AGGREGATION_METRIC_WORDS;
        # covered separately by _AGGREGATION_EXACT_COLS if needed
        "total_shipments",
        "n_shipments",
        "total_quantity",
        "avg_chargeable_weight",
    ])
    def test_valid_agg_column_returns_true(self, col):
        assert has_aggregation_metrics([col]) is True, (
            f"has_aggregation_metrics([{col!r}]) should be True — "
            f"it is a genuine aggregation output column"
        )

    def test_raw_schema_with_amount_col_returns_false(self):
        """The raw browser schema must NOT produce has_aggregation_metrics=True."""
        result = has_aggregation_metrics(_RAW_BROWSER_SCHEMA)
        assert result is False, (
            "has_aggregation_metrics(RAW_BROWSER_SCHEMA) must be False after "
            "removing 'amount' from _AGGREGATION_METRIC_WORDS"
        )

    def test_valid_revenue_schema_returns_true(self):
        assert has_aggregation_metrics(_REVENUE_AGG_SCHEMA) is True

    def test_valid_lane_schema_returns_true(self):
        assert has_aggregation_metrics(_LANE_AGG_SCHEMA) is True

    def test_valid_status_schema_returns_true(self):
        assert has_aggregation_metrics(_STATUS_AGG_SCHEMA) is True


# =============================================================================
# Part 2 — has_raw_shipment_signals()
# =============================================================================

class TestHasRawShipmentSignals:

    def test_raw_browser_schema_detected(self):
        assert has_raw_shipment_signals(_RAW_BROWSER_SCHEMA) is True

    @pytest.mark.parametrize("col", [
        "sales_functional_currency_amount",
        "sales_budget_rate_amount",
        "shipment_quantity",
        "chargeable_weight",
        "actual_weight",
    ])
    def test_measure_columns_are_raw_signals(self, col):
        assert has_raw_shipment_signals([col]) is True, (
            f"has_raw_shipment_signals([{col!r}]) should be True — "
            f"raw per-shipment column added in P1 fix"
        )

    def test_aggregation_schemas_have_no_raw_signals(self):
        assert has_raw_shipment_signals(_REVENUE_AGG_SCHEMA) is False
        assert has_raw_shipment_signals(_LANE_AGG_SCHEMA) is False
        assert has_raw_shipment_signals(_STATUS_AGG_SCHEMA) is False


# =============================================================================
# Part 3 — is_aggregation_shape_mismatch() core fix
# =============================================================================

class TestIsAggregationShapeMismatch:

    def test_raw_browser_schema_aggregation_intent_is_mismatch(self):
        """Key P1 regression: raw schema + AGGREGATION -> mismatch must be True."""
        result = is_aggregation_shape_mismatch("AGGREGATION", _RAW_BROWSER_SCHEMA)
        assert result is True, (
            "is_aggregation_shape_mismatch('AGGREGATION', RAW_BROWSER_SCHEMA) "
            "must be True after P1 fix — was False before (false positive from 'amount')"
        )

    def test_valid_revenue_schema_is_not_mismatch(self):
        assert is_aggregation_shape_mismatch("AGGREGATION", _REVENUE_AGG_SCHEMA) is False

    def test_valid_lane_schema_is_not_mismatch(self):
        assert is_aggregation_shape_mismatch("AGGREGATION", _LANE_AGG_SCHEMA) is False

    def test_non_aggregation_intent_never_mismatch(self):
        assert is_aggregation_shape_mismatch("BROAD_LISTING", _RAW_BROWSER_SCHEMA) is False
        assert is_aggregation_shape_mismatch("GENERAL", _RAW_BROWSER_SCHEMA) is False

    def test_empty_headers_never_mismatch(self):
        assert is_aggregation_shape_mismatch("AGGREGATION", []) is False


# =============================================================================
# Part 4 — is_analytical_shape_mismatch() with all required prompts
# =============================================================================

class TestIsAnalyticalShapeMismatch:

    @pytest.mark.parametrize("prompt,intent", [
        ("Total revenue by business unit this month", "AGGREGATION"),
        ("Top lanes by volume", "AGGREGATION"),
        ("give analysis on shipments via air and their delay", "AGGREGATION"),
        ("summarise the shipments from US", "AGGREGATION"),
        ("summarize the shipments from US", "AGGREGATION"),
        ("give me a summary of shipments from US", "AGGREGATION"),
        ("summarise the shipments from US", "BROAD_LISTING"),  # safety net
        ("summarise the shipments from US", "SUMMARY_REQUEST"),  # P1: new
    ])
    def test_analytical_prompts_with_raw_schema_trigger_mismatch(self, prompt, intent):
        """All analytical/summary prompts must trigger mismatch with raw browser schema."""
        result = is_analytical_shape_mismatch(intent, _RAW_BROWSER_SCHEMA, prompt)
        assert result is True, (
            f"is_analytical_shape_mismatch({intent!r}, raw_schema, {prompt!r}) "
            f"should be True — retry should fire"
        )

    @pytest.mark.parametrize("prompt", [
        "which shipments are from US?",
        "show shipments from US",
        "show me delayed shipments",
        "list air shipments",
        "which shipments are in transit?",
    ])
    def test_listing_prompts_with_raw_schema_do_not_trigger_mismatch(self, prompt):
        """Pure listing prompts must NOT trigger mismatch with raw browser schema."""
        result = is_analytical_shape_mismatch("BROAD_LISTING", _RAW_BROWSER_SCHEMA, prompt)
        assert result is False, (
            f"is_analytical_shape_mismatch('BROAD_LISTING', raw_schema, {prompt!r}) "
            f"should be False — listing prompts allow raw rows"
        )

    def test_valid_aggregation_schema_does_not_trigger_mismatch(self):
        assert is_analytical_shape_mismatch("AGGREGATION", _REVENUE_AGG_SCHEMA, "revenue by BU") is False
        assert is_analytical_shape_mismatch("AGGREGATION", _LANE_AGG_SCHEMA, "top lanes by volume") is False


# =============================================================================
# Part 5 — has_analytical_vocabulary() P1 additions (summarise/summarize/summary)
# =============================================================================

class TestHasAnalyticalVocabularyP1:

    @pytest.mark.parametrize("prompt", [
        "summarise the shipments from US",
        "summarize the shipments from US",
        "give me a summary of shipments from US",
        "summary of air shipments",
        "summarise delayed shipments",
        "summarize air shipments",
    ])
    def test_summary_vocab_detected(self, prompt):
        assert has_analytical_vocabulary(prompt) is True, (
            f"has_analytical_vocabulary({prompt!r}) should be True after P1 fix"
        )

    @pytest.mark.parametrize("prompt", [
        "which shipments are from US?",
        "show shipments from US",
        "list all delayed shipments",
    ])
    def test_listing_vocab_not_detected(self, prompt):
        assert has_analytical_vocabulary(prompt) is False


# =============================================================================
# Part 6 — build_retry_prompt() P1 coverage
# =============================================================================

class TestBuildRetryPromptP1:

    def test_revenue_prompt_builds_explicit_group_by(self):
        p = build_retry_prompt("Total revenue by business unit this month", "AGGREGATION")
        assert "business_unit_id" in p
        assert "total_revenue" in p
        assert "Do not return individual shipment-level rows" in p

    def test_lane_prompt_builds_explicit_group_by(self):
        p = build_retry_prompt("Top lanes by volume", "AGGREGATION")
        assert "source_" in p
        assert "destination" in p
        assert "shipment_count" in p
        assert "Do not return individual shipment-level rows" in p

    def test_summary_prompt_builds_summary_template(self):
        p = build_retry_prompt("summarise the shipments from US", "AGGREGATION")
        assert "Do not return individual shipment-level rows" in p

    def test_generic_fallback_has_no_raw_rows(self):
        p = build_retry_prompt("what is the busiest corridor", "AGGREGATION")
        assert "Do not return individual shipment-level rows" in p
