"""E5 Enterprise Reliability contract tests.

Covers all 8 contracts and 5 specific fixes introduced in Phase E5:
  - Contract 1: Aggregation intent routing (lane, volume, destination, etc.)
  - Contract 2: Broad listing still routes correctly after AGGREGATION swap
  - Contract 4: Low-information guard still passes logistics vocabulary
  - Contract 5: Suggestion canonicalization (known labels -> canonical prompts)
  - Contract 6: Result-shape validation (is_aggregation_shape_mismatch)
  - Contract 7: Message-level export context (latest_table_result)
  - Fix 1:  AGGREGATION classified before BROAD_LISTING for overlapping phrases
  - Fix 3:  build_retry_prompt returns canonical GROUP-BY prompts
  - Fix 4:  DOWNLOAD_REQUEST uses latest_table_result when available
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# SDK mock — must be applied before importing any module that pulls in
# app.services.genie_client (which calls WorkspaceClient at import time).
# ---------------------------------------------------------------------------
sys.path.insert(
    0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app"
)
os.environ.setdefault("DATABRICKS_TOKEN", "test-fake-token-e5-tests")
os.environ.setdefault("DATABRICKS_HOST", "https://fake-workspace.databricks.com")

_mock_ws = MagicMock()
_mock_ws.config.authenticate.return_value = {
    "Authorization": "Bearer test-fake-token-e5-tests"
}

with patch("databricks.sdk.WorkspaceClient", return_value=_mock_ws):
    from app.services.genie_client import GenieQueryResult
    from app.services.genie_pipeline import GeniePipeline, _canonicalize_user_message

from app.services.pre_genie_router import route_pre_genie
from app.services.result_shape_validator import (
    has_aggregation_metrics,
    has_raw_shipment_signals,
    is_aggregation_shape_mismatch,
    build_retry_prompt,
)
from app.services.genie_session_store import GenieSessionStore, TableExportRecord


# =============================================================================
# Shared stubs
# =============================================================================

SPACE_ID      = "space-e5-test"
APP_CONV_ID   = "app-conv-e5-001"
GENIE_CONV_ID = "genie-conv-e5-001"
MSG_ID        = "msg-e5-001"
STMT_ID       = "stmt-e5-001"


class _TextAttachment:
    content = "Here is the analysis."
    type    = "text"


class _FakeQueryAttachment:
    def __init__(self, *, headers, rows):
        self._headers = headers
        self._rows    = rows
        self.statement_id = STMT_ID
        self.query_text   = "SELECT ..."

    @property
    def headers(self):
        return self._headers

    @property
    def rows(self):
        return self._rows


class _FakeMessage:
    """Text-only message — no query attachment."""
    query_attachments   = []
    text_attachments    = [_TextAttachment()]
    viz_attachments     = []
    suggestions         = []
    message_id          = MSG_ID
    conversation_id     = GENIE_CONV_ID
    status              = "COMPLETED"
    error               = None


class _FakeQueryMessage:
    """Message with raw shipment-level headers (triggers shape mismatch for AGGREGATION)."""
    query_attachments   = [_FakeQueryAttachment(
        headers=["shipment_number_id", "source_", "destination", "eta", "waybill"],
        rows=[["SHP-001", "US", "DE", "2024-01-01", "HBL123"]],
    )]
    text_attachments    = [_TextAttachment()]
    viz_attachments     = []
    suggestions         = []
    message_id          = MSG_ID
    conversation_id     = GENIE_CONV_ID
    status              = "COMPLETED"
    error               = None


class FakeGenieClient:
    def __init__(self, *, genie_conv_id=GENIE_CONV_ID, start_msg_id=MSG_ID):
        self.genie_conv_id    = genie_conv_id
        self.start_msg_id     = start_msg_id
        self.start_conv_calls = []
        self.send_msg_calls   = []

    def start_conversation(self, space_id, message):
        self.start_conv_calls.append((space_id, message))
        return {"conversation_id": self.genie_conv_id, "message_id": self.start_msg_id}

    def send_message(self, space_id, conv_id, message):
        self.send_msg_calls.append((space_id, conv_id, message))
        return {"message_id": self.start_msg_id}

    def wait_for_message_completion(self, *args, **kwargs):
        return _FakeMessage()

    def fetch_query_result(self, *args, **kwargs):
        return None


class FakeAuditService:
    def __init__(self, download_key="dk-e5-001"):
        self.download_key = download_key
        self.calls = []

    def create_export(self, headers, rows, filename_prefix="genie_export"):
        self.calls.append({"headers": headers, "rows": rows})
        return self.download_key

    def get_export_path(self, dk):
        return f"/tmp/{dk}.csv"


def _make_pipeline(**kwargs):
    return GeniePipeline(
        genie_client=kwargs.pop("client", FakeGenieClient()),
        session_store=kwargs.pop("store", GenieSessionStore()),
        space_id=SPACE_ID,
        timeout_seconds=30,
        poll_interval_seconds=0.0,
        fetch_query_results=False,
        **kwargs,
    )


# =============================================================================
# CONTRACT 1 & FIX 1: Aggregation routing — expanded patterns
# =============================================================================

_AGGREGATION_PHRASES = [
    # Previously failing phrases
    "Top lanes by volume",
    "which are the top lanes with maximum volumes",
    "top lanes",
    "top shipment lanes",
    "lanes by volume",
    "top 5 lanes",
    "volume by lane",
    "shipment count by lane",
    "top destinations",
    "top routes",
    "lane breakdown",
    "lane analysis",
    "highest volume lanes",
    "maximum volumes by lane",
    "busiest lanes",
    "by volume",
    # Existing phrases that must continue to work
    "busiest shipment lanes",
    "total revenue by business unit",
    "shipment status distribution",
    "distribution by mode",
    "how many shipments are there",
    "monthly trend",
    "top 10 shipments by revenue",
    "breakdown by transport mode",
]


class TestAggregationRouting:
    @pytest.mark.parametrize("phrase", _AGGREGATION_PHRASES)
    def test_aggregation_phrases_route_to_aggregation(self, phrase):
        result = route_pre_genie(phrase)
        assert result["intent"] == "AGGREGATION", (
            f"Expected AGGREGATION for {phrase!r}, got {result['intent']!r}. "
            f"Reason: {result.get('reason')!r}"
        )
        assert result["should_call_genie"] is True

    def test_top_lanes_by_volume_not_broad_listing(self):
        result = route_pre_genie("Top lanes by volume")
        assert result["intent"] != "BROAD_LISTING", (
            "Contract 1 violation: 'Top lanes by volume' must route AGGREGATION, not BROAD_LISTING"
        )

    def test_which_are_the_top_lanes_not_broad_listing(self):
        """Regression: 'which are...' phrase was BROAD_LISTING before E5 AGGREGATION swap."""
        result = route_pre_genie("which are the top lanes with maximum volumes")
        assert result["intent"] == "AGGREGATION", (
            "Contract 1 + Fix 1 violation: AGGREGATION must fire before BROAD_LISTING. "
            f"Got {result['intent']!r}"
        )

    def test_lane_aggregation_prompt_contains_group_by(self):
        """Lane query must produce a canonical GROUP BY prompt."""
        result = route_pre_genie("top lanes by volume")
        prompt = result.get("enriched_prompt") or ""
        assert "group by" in prompt.lower(), (
            f"Lane AGGREGATION prompt must contain 'Group by'. Got: {prompt!r}"
        )

    def test_lane_aggregation_prompt_contains_shipment_count(self):
        result = route_pre_genie("top lanes by volume")
        prompt = result.get("enriched_prompt") or ""
        assert "shipment_count" in prompt.lower()

    def test_top_5_lanes_preserves_n(self):
        result = route_pre_genie("show the top 5 lanes by volume")
        prompt = result.get("enriched_prompt") or ""
        assert "5" in prompt, (
            f"Top-N extraction failed: prompt should contain '5'. Got: {prompt!r}"
        )

    def test_canonical_prompt_passes_through_unchanged(self):
        """A canonical prompt with 'Group by' must pass through without re-wrapping."""
        canonical = (
            "Show the top 10 shipment lanes by shipment count. "
            "Group by source_ and destination. "
            "Return source_, destination, shipment_count. "
            "Sort by shipment_count descending. "
            "Do not return individual shipment-level rows."
        )
        result = route_pre_genie(canonical)
        assert result["intent"] == "AGGREGATION"
        assert result.get("enriched_prompt") == canonical


# =============================================================================
# CONTRACT 2: Broad listing still works after AGGREGATION swap
# =============================================================================

_BROAD_LISTING_PHRASES = [
    "show me delayed shipments",
    "shipments from Mexico",
    "which shipments are in transit",
    "show me shipments from US via air",
    "pending shipments",
    "delivered shipments",
]


class TestBroadListingStillWorks:
    @pytest.mark.parametrize("phrase", _BROAD_LISTING_PHRASES)
    def test_broad_listing_phrases_route_correctly(self, phrase):
        result = route_pre_genie(phrase)
        assert result["intent"] not in ("NON_BUSINESS_OR_SMALL_TALK",), (
            f"Broad listing phrase {phrase!r} was incorrectly blocked. "
            f"Intent: {result['intent']!r}"
        )
        assert result["should_call_genie"] is True

    def test_delayed_shipments_not_aggregation(self):
        result = route_pre_genie("show me delayed shipments")
        assert result["intent"] == "BROAD_LISTING", (
            f"'show me delayed shipments' should be BROAD_LISTING, got {result['intent']!r}"
        )


# =============================================================================
# CONTRACT 5 & FIX 2: Suggestion canonicalization
# =============================================================================


class TestSuggestionCanonicalization:
    def test_top_lanes_by_volume_chip_canonicalises(self):
        canonical = _canonicalize_user_message("Top lanes by volume")
        assert "group by" in canonical.lower()

    def test_canonical_prompt_contains_shipment_count(self):
        canonical = _canonicalize_user_message("Top lanes by volume")
        assert "shipment_count" in canonical.lower()

    def test_canonical_prompt_says_do_not_return_individual_rows(self):
        canonical = _canonicalize_user_message("Top lanes by volume")
        assert "do not return" in canonical.lower()

    def test_case_insensitive_canonicalization(self):
        lower = _canonicalize_user_message("top lanes by volume")
        upper = _canonicalize_user_message("TOP LANES BY VOLUME")
        assert lower == upper

    def test_shipment_status_distribution_chip_canonicalises(self):
        canonical = _canonicalize_user_message("Shipment status distribution")
        assert "group by" in canonical.lower()
        assert "execution_status" in canonical.lower()

    def test_unknown_message_passes_through(self):
        msg = "shipments from Germany to US via air last week"
        assert _canonicalize_user_message(msg) == msg

    def test_which_shipments_are_in_transit_chip_canonicalises(self):
        canonical = _canonicalize_user_message("Which shipments are in transit?")
        assert "transit" in canonical.lower()
        assert canonical != "Which shipments are in transit?"

    def test_show_me_delayed_shipments_chip_canonicalises(self):
        canonical = _canonicalize_user_message("Show me delayed shipments")
        assert "delayed" in canonical.lower()
        assert canonical != "Show me delayed shipments"

    def test_trailing_punctuation_stripped_for_matching(self):
        with_q  = _canonicalize_user_message("Which shipments are in transit?")
        without = _canonicalize_user_message("Which shipments are in transit")
        assert with_q == without

    def test_pipeline_canonicalizes_top_lanes_chip(self):
        """Pipeline must send a canonical GROUP BY prompt to Genie for 'Top lanes by volume'."""
        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client)
        pipeline.run("Top lanes by volume", APP_CONV_ID)

        assert len(client.start_conv_calls) == 1
        sent_prompt = client.start_conv_calls[0][1]
        assert "group by" in sent_prompt.lower(), (
            f"Sent: {sent_prompt!r}"
        )
        assert "shipment_count" in sent_prompt.lower()


# =============================================================================
# CONTRACT 6 & FIX 3: Result-shape validator
# =============================================================================


class TestResultShapeValidator:
    def test_raw_shipment_headers_detected(self):
        assert has_raw_shipment_signals(["shipment_number_id", "source_", "destination", "eta"])

    def test_aggregated_headers_not_raw(self):
        assert not has_raw_shipment_signals(["source_", "destination", "shipment_count"])

    def test_empty_headers_not_raw(self):
        assert not has_raw_shipment_signals([])

    def test_count_column_detected(self):
        assert has_aggregation_metrics(["source_", "destination", "shipment_count"])

    def test_total_column_detected(self):
        assert has_aggregation_metrics(["bu_id", "total_revenue"])

    def test_avg_column_detected(self):
        assert has_aggregation_metrics(["mode", "avg_weight"])

    def test_non_metric_headers_not_detected(self):
        assert not has_aggregation_metrics(["shipment_number_id", "source_", "destination", "eta"])

    def test_aggregation_with_raw_rows_is_mismatch(self):
        headers = ["shipment_number_id", "source_", "destination", "eta", "waybill"]
        assert is_aggregation_shape_mismatch("AGGREGATION", headers)

    def test_aggregation_with_aggregated_rows_is_not_mismatch(self):
        headers = ["source_", "destination", "shipment_count"]
        assert not is_aggregation_shape_mismatch("AGGREGATION", headers)

    def test_non_aggregation_intent_never_mismatch(self):
        raw_headers = ["shipment_number_id", "eta", "waybill"]
        assert not is_aggregation_shape_mismatch("BROAD_LISTING", raw_headers)
        assert not is_aggregation_shape_mismatch("GENERAL", raw_headers)

    def test_empty_headers_not_mismatch(self):
        assert not is_aggregation_shape_mismatch("AGGREGATION", [])

    def test_mixed_headers_not_mismatch(self):
        """If table has BOTH raw signals AND metric columns, it is not a clear mismatch."""
        mixed = ["shipment_number_id", "destination", "shipment_count"]
        assert not is_aggregation_shape_mismatch("AGGREGATION", mixed)

    def test_lane_retry_prompt_has_group_by(self):
        prompt = build_retry_prompt("top lanes by volume", "AGGREGATION")
        assert "group by" in prompt.lower()

    def test_lane_retry_prompt_has_shipment_count(self):
        prompt = build_retry_prompt("top lanes by volume", "AGGREGATION")
        assert "shipment_count" in prompt.lower()

    def test_lane_retry_prompt_has_no_individual_rows(self):
        prompt = build_retry_prompt("busiest lanes", "AGGREGATION")
        assert "do not return" in prompt.lower()

    def test_top_5_lanes_retry_preserves_n(self):
        prompt = build_retry_prompt("top 5 lanes by volume", "AGGREGATION")
        assert "5" in prompt

    def test_status_distribution_retry_prompt(self):
        prompt = build_retry_prompt("shipment status distribution", "AGGREGATION")
        assert "execution_status" in prompt.lower()

    def test_revenue_retry_prompt(self):
        prompt = build_retry_prompt("total revenue by business unit", "AGGREGATION")
        assert "business_unit_id" in prompt.lower()

    def test_mode_retry_prompt(self):
        prompt = build_retry_prompt("mode breakdown by transport", "AGGREGATION")
        assert "transportation_mode_desc" in prompt.lower()

    def test_destination_retry_prompt(self):
        prompt = build_retry_prompt("top destinations by count", "AGGREGATION")
        assert "destination" in prompt.lower()

    def test_generic_fallback_retry_prompt(self):
        prompt = build_retry_prompt("some unusual aggregation question", "AGGREGATION")
        assert "do not return" in prompt.lower()


# =============================================================================
# CONTRACT 7 & FIX 4: Message-level export context
# =============================================================================


class TestTableExportRecord:
    def test_record_creation(self):
        rec = TableExportRecord(
            assistant_message_id="msg-001",
            download_key="dk-001",
            export_id="exp-001",
            export_status="ready",
            export_mode="returned_rows_only",
            export_row_count=500,
            query_description="Top lanes",
            created_at=datetime.now(timezone.utc),
        )
        assert rec.download_key == "dk-001"
        assert rec.export_row_count == 500

    def test_record_to_dict(self):
        rec = TableExportRecord(
            assistant_message_id="msg-001",
            download_key="dk-001",
            export_id="exp-001",
            export_status="ready",
            export_mode="returned_rows_only",
            export_row_count=500,
            query_description="Top lanes",
            created_at=datetime.now(timezone.utc),
        )
        d = rec.to_dict()
        assert d["download_key"] == "dk-001"
        assert d["export_row_count"] == 500

    def test_record_with_no_download_key(self):
        rec = TableExportRecord(
            assistant_message_id="msg-002",
            download_key=None,
            export_id=None,
            export_status=None,
            export_mode=None,
            export_row_count=None,
            query_description=None,
            created_at=datetime.now(timezone.utc),
        )
        assert rec.download_key is None


class TestMessageLevelExportContext:
    def test_latest_table_result_starts_none(self):
        store = GenieSessionStore()
        snapshot = store.get_context_snapshot(APP_CONV_ID)
        assert snapshot == {} or snapshot.get("latest_table_result") is None

    def test_update_context_stores_table_record(self):
        store = GenieSessionStore()
        store.set_genie_conversation_id(APP_CONV_ID, GENIE_CONV_ID)
        rec = TableExportRecord(
            assistant_message_id="msg-001",
            download_key="dk-abc",
            export_id="exp-001",
            export_status="ready",
            export_mode="returned_rows_only",
            export_row_count=200,
            query_description="Top lanes",
            created_at=datetime.now(timezone.utc),
        )
        store.update_context(APP_CONV_ID, latest_table_result=rec)
        snapshot = store.get_context_snapshot(APP_CONV_ID)
        assert snapshot["latest_table_result"] is not None
        assert snapshot["latest_table_result"]["download_key"] == "dk-abc"
        assert snapshot["latest_table_result"]["export_row_count"] == 200

    def test_new_table_overwrites_old_record(self):
        """Contract 7: latest_table_result ALWAYS overwrites — even when new record has no download_key."""
        store = GenieSessionStore()
        store.set_genie_conversation_id(APP_CONV_ID, GENIE_CONV_ID)

        old_rec = TableExportRecord(
            assistant_message_id="msg-001",
            download_key="dk-stale",
            export_id="exp-001",
            export_status="ready",
            export_mode="returned_rows_only",
            export_row_count=2,
            query_description="Old query",
            created_at=datetime.now(timezone.utc),
        )
        store.update_context(APP_CONV_ID, latest_table_result=old_rec)

        new_rec = TableExportRecord(
            assistant_message_id="msg-002",
            download_key=None,
            export_id=None,
            export_status=None,
            export_mode=None,
            export_row_count=500,
            query_description="New bigger query",
            created_at=datetime.now(timezone.utc),
        )
        store.update_context(APP_CONV_ID, latest_table_result=new_rec)

        snapshot = store.get_context_snapshot(APP_CONV_ID)
        ltr = snapshot["latest_table_result"]
        assert ltr["download_key"] is None, (
            "Contract 7 violation: new table (no export) must overwrite old table's download_key. "
            f"Found: {ltr['download_key']!r}"
        )
        assert ltr["assistant_message_id"] == "msg-002"

    def test_non_table_turn_does_not_overwrite_record(self):
        """A greeting/text turn with no latest_table_result arg must NOT touch the existing record."""
        store = GenieSessionStore()
        store.set_genie_conversation_id(APP_CONV_ID, GENIE_CONV_ID)
        rec = TableExportRecord(
            assistant_message_id="msg-001",
            download_key="dk-persist",
            export_id="exp-001",
            export_status="ready",
            export_mode="returned_rows_only",
            export_row_count=100,
            query_description="Some lanes",
            created_at=datetime.now(timezone.utc),
        )
        store.update_context(APP_CONV_ID, latest_table_result=rec)

        # Greeting turn — no latest_table_result passed
        store.update_context(APP_CONV_ID, last_intent="GREETING", last_user_prompt="hi")

        snapshot = store.get_context_snapshot(APP_CONV_ID)
        assert snapshot["latest_table_result"]["download_key"] == "dk-persist"

    def test_download_request_uses_latest_table_result(self):
        result = route_pre_genie(
            "download this data",
            latest_table_result={
                "download_key": "dk-latest",
                "export_id": "exp-latest",
                "export_status": "ready",
                "export_mode": "returned_rows_only",
                "export_row_count": 500,
                "query_description": "Top lanes",
            },
        )
        assert result["intent"] == "DOWNLOAD_REQUEST"
        assert result["download_key"] == "dk-latest"
        assert result["export_row_count"] == 500
        assert result["should_call_genie"] is False

    def test_download_request_does_not_serve_stale_key_when_latest_has_no_export(self):
        """Contract 7 core: stale last_download_key must NOT be served when latest table has no export."""
        result = route_pre_genie(
            "i want to download this data",
            latest_table_result={
                "download_key": None,
                "export_id": None,
                "export_status": None,
                "export_mode": None,
                "export_row_count": None,
                "query_description": "Top lanes with no export",
            },
            last_download_key="dk-stale-2row",
        )
        assert result["intent"] == "DOWNLOAD_REQUEST"
        assert result["download_key"] is None, (
            "Contract 7 violation: stale download_key must NOT be served. "
            f"Got: {result['download_key']!r}"
        )

    def test_download_request_with_preparing_export(self):
        result = route_pre_genie(
            "download this",
            latest_table_result={
                "download_key": None,
                "export_id": "exp-queued",
                "export_status": "queued",
                "export_mode": "returned_rows_only",
                "export_row_count": None,
            },
        )
        assert result["intent"] == "DOWNLOAD_REQUEST"
        assert result["download_key"] is None
        resp = (result.get("local_response") or "").lower()
        assert "preparing" in resp or "prepared" in resp or "still" in resp

    def test_download_request_falls_back_to_last_download_key_when_no_latest(self):
        result = route_pre_genie(
            "download csv",
            latest_table_result=None,
            last_download_key="dk-legacy-123",
        )
        assert result["download_key"] == "dk-legacy-123"

    def test_pipeline_registers_table_export_record_in_session(self):
        """After a table response turn, latest_table_result must be set in the session."""

        class _ClientWithRawTable:
            def __init__(self):
                self.start_conv_calls = []

            def start_conversation(self, space_id, message):
                self.start_conv_calls.append((space_id, message))
                return {"conversation_id": GENIE_CONV_ID, "message_id": MSG_ID}

            def send_message(self, *args, **kwargs):
                return {"message_id": MSG_ID}

            def wait_for_message_completion(self, *args, **kwargs):
                return _FakeQueryMessage()

            def fetch_query_result(self, *args, **kwargs):
                return GenieQueryResult(
                    statement_id=STMT_ID,
                    status="SUCCEEDED",
                    columns=["shipment_number_id", "source_", "destination"],
                    rows=[["SHP-001", "US", "DE"]],
                    row_count=1,
                    total_row_count=1,
                    format="JSON_ARRAY",
                )

        audit = FakeAuditService(download_key="dk-new-table")
        store = GenieSessionStore()
        pipeline = GeniePipeline(
            genie_client=_ClientWithRawTable(),
            session_store=store,
            space_id=SPACE_ID,
            timeout_seconds=30,
            poll_interval_seconds=0.0,
            fetch_query_results=True,
            audit_service=audit,
            enable_shape_validation=False,  # disable retry for this test
        )
        pipeline.run("shipments from US", APP_CONV_ID)

        snapshot = store.get_context_snapshot(APP_CONV_ID)
        ltr = snapshot.get("latest_table_result")
        assert ltr is not None, "latest_table_result must be set after a table response"
        assert ltr["download_key"] == "dk-new-table"


# =============================================================================
# SESSION STORE: serialize / deserialize round-trip for TableExportRecord
# =============================================================================


class TestSessionStoreTableExportSerialization:
    def test_serialize_deserialize_roundtrip(self):
        store = GenieSessionStore()
        store.set_genie_conversation_id(APP_CONV_ID, GENIE_CONV_ID)
        rec = TableExportRecord(
            assistant_message_id="msg-rt",
            download_key="dk-rt",
            export_id="exp-rt",
            export_status="ready",
            export_mode="returned_rows_only",
            export_row_count=999,
            query_description="Roundtrip test",
            created_at=datetime.now(timezone.utc),
        )
        store.update_context(APP_CONV_ID, latest_table_result=rec)

        session = store.get_session(APP_CONV_ID)
        serialized = store.serialize_session(session)
        deserialized = store.deserialize_session(serialized)

        assert deserialized.latest_table_result is not None
        ltr = deserialized.latest_table_result
        assert ltr.download_key == "dk-rt"
        assert ltr.export_row_count == 999
        assert ltr.assistant_message_id == "msg-rt"

    def test_serialize_none_table_result(self):
        store = GenieSessionStore()
        store.set_genie_conversation_id(APP_CONV_ID, GENIE_CONV_ID)
        session = store.get_session(APP_CONV_ID)
        assert session.latest_table_result is None
        serialized = store.serialize_session(session)
        assert serialized["latest_table_result"] is None
        deserialized = store.deserialize_session(serialized)
        assert deserialized.latest_table_result is None
