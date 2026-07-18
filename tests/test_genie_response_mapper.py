"""Unit tests for app/services/genie_response_mapper.py.

All tests use directly-constructed GenieMessage/GenieQueryResult dataclasses.
No HTTP calls are made. No Databricks SDK is required.

Test cases:
    1.  Text-only message maps to message string, is_table=False.
    2.  Query attachment extracts SQL and statement_id.
    3.  Query result List[List] rows map to table_data correctly.
    4.  Query result List[Dict] rows map to table_data correctly.
    5.  Suggested questions map to a list of strings.
    6.  Viz attachment maps to visualization reference (not a chart spec).
    7.  Viz attachment does not claim a Vega/Plotly/image spec.
    8.  Full message (text + table + viz) maps all fields together.
    9.  FAILED Genie message returns safe error status.
    10. CANCELLED Genie message returns safe error status.
    11. debug=False: debug_info is None.
    12. debug=True: debug_info includes SQL and thoughts.
    13. Quick-summary text-only response works with no SQL attachment.
    14. None message returns safe error response.
    15. Multiple text attachments are concatenated.
    16. Row count reflects actual returned rows, not attachment metadata.
"""

import sys
import os

sys.path.insert(
    0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app"
)

# ---------------------------------------------------------------------------
# Patch SDK imports before loading genie_client
# ---------------------------------------------------------------------------
os.environ.setdefault("DATABRICKS_TOKEN", "test-fake-token-mapper-tests")
os.environ.setdefault("DATABRICKS_HOST", "https://fake-workspace.databricks.com")

from unittest.mock import patch, MagicMock

_mock_ws = MagicMock()
_mock_ws.config.authenticate.return_value = {
    "Authorization": "Bearer test-fake-token-mapper-tests"
}

with patch("databricks.sdk.WorkspaceClient", return_value=_mock_ws):
    from app.services.genie_client import (
        GenieMessage,
        GenieTextAttachment,
        GenieQueryAttachment,
        GenieVizAttachment,
        GenieQueryResult,
    )

from app.services.genie_response_mapper import map_genie_message_to_chat_response


# ---------------------------------------------------------------------------
# Fixtures: canonical test objects
# ---------------------------------------------------------------------------

APP_CONV_ID   = "app-conv-unit-test-001"
GENIE_CONV_ID = "genie-conv-unit-test-abc"
MSG_ID        = "genie-msg-unit-test-xyz"
STMT_ID       = "01f17c4b-291b-127a-8c98-8971a158dbdb"
ATT_QUERY_ID  = "att-query-id-001"
ATT_VIZ_ID    = "att-viz-id-001"

TEST_SQL = (
    "SELECT * FROM `onedata_fn_ion_dev`.`ion_l0_raw`.`lbn_with_scorecard` "
    "WHERE source_ = 'US' AND transportation_mode_desc ILIKE '%air%'"
)

_COLUMNS = [
    {"name": "shipment_number_id", "type_text": "STRING", "position": 0},
    {"name": "source_",           "type_text": "STRING", "position": 1},
    {"name": "shipment_quantity",  "type_text": "DOUBLE", "position": 2},
]

_ROWS_LIST = [
    ["SHP-001", "US", "127.0"],
    ["SHP-002", "US", "588.0"],
    ["SHP-003", "US", "2000.0"],
]

_ROWS_DICT = [
    {"shipment_number_id": "SHP-001", "source_": "US", "shipment_quantity": "127.0"},
    {"shipment_number_id": "SHP-002", "source_": "US", "shipment_quantity": "588.0"},
]


def _make_text_only_message(text: str = "**5 000 shipments** found.") -> GenieMessage:
    return GenieMessage(
        id=MSG_ID,
        space_id="space-001",
        conversation_id=GENIE_CONV_ID,
        status="COMPLETED",
        content="shipments from US via air",
        message_id=MSG_ID,
        text_attachments=[GenieTextAttachment(content=text)],
        query_attachments=[],
        viz_attachments=[],
        suggested_questions=[],
    )


def _make_full_message() -> GenieMessage:
    """Message with all four attachment types."""
    return GenieMessage(
        id=MSG_ID,
        space_id="space-001",
        conversation_id=GENIE_CONV_ID,
        status="COMPLETED",
        content="shipments from US via air",
        message_id=MSG_ID,
        text_attachments=[
            GenieTextAttachment(
                content="**5 000 shipments** found from the US via air.",
                attachment_id="att-text-001",
            )
        ],
        query_attachments=[
            GenieQueryAttachment(
                attachment_id=ATT_QUERY_ID,
                sql=TEST_SQL,
                description="US air shipments.",
                statement_id=STMT_ID,
                row_count=5000,
                thoughts=[
                    {"thought_type": "THOUGHT_TYPE_DESCRIPTION",
                     "content": "Filter US air shipments."},
                    {"thought_type": "THOUGHT_TYPE_STEPS",
                     "content": "Apply source_ = US and mode ILIKE air."},
                ],
            )
        ],
        viz_attachments=[
            GenieVizAttachment(
                attachment_id=ATT_VIZ_ID,
                query_attachment_id=ATT_QUERY_ID,
            )
        ],
        suggested_questions=[
            "What are the top destinations?",
            "Which are in transit?",
            "Show revenue by BU.",
        ],
    )


def _make_query_result_list_rows() -> GenieQueryResult:
    return GenieQueryResult(
        statement_id=STMT_ID,
        status="SUCCEEDED",
        columns=_COLUMNS,
        rows=_ROWS_LIST,
        row_count=len(_ROWS_LIST),
        total_row_count=5000,
        format="JSON_ARRAY",
    )


def _make_query_result_dict_rows() -> GenieQueryResult:
    return GenieQueryResult(
        statement_id=STMT_ID,
        status="SUCCEEDED",
        columns=_COLUMNS,
        rows=_ROWS_DICT,
        row_count=len(_ROWS_DICT),
        total_row_count=5000,
        format="JSON_ARRAY",
    )


# =============================================================================
# TEST 1: Text-only message maps to message string, is_table=False
# =============================================================================


class TestTextOnlyMessage:
    def test_message_text_is_primary_content(self):
        msg = _make_text_only_message("**42 shipments** found in Q1.")
        resp = map_genie_message_to_chat_response(
            msg, app_conversation_id=APP_CONV_ID
        )

        assert resp["status"] == "success"
        assert "42 shipments" in resp["message"]
        assert resp["is_table"] is False
        assert resp["table_data"] is None
        assert resp["row_count"] == 0

    def test_existing_contract_fields_all_present(self):
        """All ChatResponse fields from the existing contract must be present."""
        msg = _make_text_only_message()
        resp = map_genie_message_to_chat_response(msg)

        required_fields = [
            "status", "message", "is_table", "table_data",
            "row_count", "download_key", "execution_time_ms",
            "conversation_id", "clarification",
        ]
        for f in required_fields:
            assert f in resp, f"Missing existing contract field: {f}"

    def test_source_is_genie(self):
        msg = _make_text_only_message()
        resp = map_genie_message_to_chat_response(msg)
        assert resp["source"] == "genie"


# =============================================================================
# TEST 2: Query attachment extracts SQL and statement_id
# =============================================================================


class TestQueryAttachment:
    def test_generated_sql_is_extracted(self):
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg)

        assert resp["generated_sql"] is not None
        assert "SELECT" in resp["generated_sql"].upper()
        assert "lbn_with_scorecard" in resp["generated_sql"]

    def test_query_attachment_types_includes_query(self):
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg)
        assert "query" in resp["attachment_types"]

    def test_no_sql_when_text_only(self):
        msg = _make_text_only_message()
        resp = map_genie_message_to_chat_response(msg)
        assert resp["generated_sql"] is None
        assert "query" not in resp["attachment_types"]


# =============================================================================
# TEST 3: Query result List[List] rows map to table_data
# =============================================================================


class TestQueryResultListRows:
    def test_table_data_has_correct_headers(self):
        msg = _make_full_message()
        qr  = _make_query_result_list_rows()
        resp = map_genie_message_to_chat_response(msg, query_result=qr)

        assert resp["is_table"] is True
        assert resp["table_data"] is not None
        assert resp["table_data"]["headers"] == [
            "shipment_number_id", "source_", "shipment_quantity"
        ]

    def test_table_data_rows_are_list_of_list(self):
        msg = _make_full_message()
        qr  = _make_query_result_list_rows()
        resp = map_genie_message_to_chat_response(msg, query_result=qr)

        rows = resp["table_data"]["rows"]
        assert isinstance(rows, list)
        assert all(isinstance(r, list) for r in rows)
        assert rows[0][0] == "SHP-001"

    def test_row_count_matches_returned_rows(self):
        msg = _make_full_message()
        qr  = _make_query_result_list_rows()
        resp = map_genie_message_to_chat_response(msg, query_result=qr)
        assert resp["row_count"] == len(_ROWS_LIST)


# =============================================================================
# TEST 4: Query result List[Dict] rows map to table_data
# =============================================================================


class TestQueryResultDictRows:
    def test_dict_rows_convert_to_list_rows(self):
        msg = _make_full_message()
        qr  = _make_query_result_dict_rows()
        resp = map_genie_message_to_chat_response(msg, query_result=qr)

        rows = resp["table_data"]["rows"]
        assert isinstance(rows, list)
        assert all(isinstance(r, list) for r in rows)

    def test_dict_row_values_in_header_order(self):
        msg = _make_full_message()
        qr  = _make_query_result_dict_rows()
        resp = map_genie_message_to_chat_response(msg, query_result=qr)

        rows = resp["table_data"]["rows"]
        # First row should be: SHP-001, US, 127.0
        assert rows[0][0] == "SHP-001"
        assert rows[0][1] == "US"
        assert rows[0][2] == "127.0"


# =============================================================================
# TEST 5: Suggested questions map to a list of strings
# =============================================================================


class TestSuggestedQuestions:
    def test_questions_are_a_list_of_strings(self):
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg)

        assert isinstance(resp["suggested_questions"], list)
        assert len(resp["suggested_questions"]) == 3
        assert all(isinstance(q, str) for q in resp["suggested_questions"])

    def test_questions_included_in_attachment_types(self):
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg)
        assert "suggested_questions" in resp["attachment_types"]

    def test_no_questions_returns_empty_list(self):
        msg = _make_text_only_message()
        resp = map_genie_message_to_chat_response(msg)
        assert resp["suggested_questions"] == []


# =============================================================================
# TEST 6: Viz attachment maps to visualization reference
# =============================================================================


class TestVizAttachment:
    def test_has_visualization_true_when_viz_present(self):
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg)
        assert resp["has_visualization"] is True

    def test_visualization_is_genie_viz_reference_type(self):
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg)

        viz = resp["visualization"]
        assert viz is not None
        assert viz["type"] == "genie_viz_reference"

    def test_visualization_has_query_attachment_id(self):
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg)

        viz = resp["visualization"]
        assert viz["query_attachment_id"] == ATT_QUERY_ID

    def test_render_strategy_is_client_side(self):
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg)

        viz = resp["visualization"]
        assert viz["render_strategy"] == "client_side_from_query_result"

    def test_can_render_client_side_false_without_rows(self):
        """Without query result, can_render_client_side should be False."""
        msg = _make_full_message()
        # No query_result passed
        resp = map_genie_message_to_chat_response(msg, query_result=None)
        assert resp["visualization"]["can_render_client_side"] is False

    def test_can_render_client_side_true_with_rows(self):
        msg = _make_full_message()
        qr  = _make_query_result_list_rows()
        resp = map_genie_message_to_chat_response(msg, query_result=qr)
        assert resp["visualization"]["can_render_client_side"] is True

    def test_no_viz_when_no_viz_attachment(self):
        msg = _make_text_only_message()
        resp = map_genie_message_to_chat_response(msg)
        assert resp["has_visualization"] is False
        assert resp["visualization"] is None


# =============================================================================
# TEST 7: Viz does not claim a Vega/Plotly/image spec
# =============================================================================


class TestVizNoChartSpec:
    def test_visualization_has_no_chart_spec_keys(self):
        """The visualization dict must not contain chart spec keys.

        Genie does not return a chart spec via API. Any presence of
        vega_lite, plotly, chart_spec, image_url, or spec would be
        a false claim and should be caught here.
        """
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg)

        viz = resp.get("visualization", {})
        if viz is None:
            return

        forbidden_keys = {"vega_lite", "plotly", "chart_spec", "spec",
                          "image_url", "image_data", "chart_json"}
        found = forbidden_keys & set(viz.keys())
        assert not found, (
            f"visualization dict contains forbidden chart spec keys: {found}"
        )

    def test_visualization_type_is_reference_not_rendered(self):
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg)
        viz = resp["visualization"]
        # The type must signal it's a reference, not a fully-rendered spec
        assert "reference" in viz["type"].lower() or "strategy" in str(viz.get("render_strategy", "")).lower()


# =============================================================================
# TEST 8: Full message (text + table + viz) maps all fields
# =============================================================================


class TestFullMessageMapping:
    def test_all_extension_fields_present(self):
        msg = _make_full_message()
        qr  = _make_query_result_list_rows()
        resp = map_genie_message_to_chat_response(
            msg,
            query_result=qr,
            app_conversation_id=APP_CONV_ID,
            genie_conversation_id=GENIE_CONV_ID,
            execution_time_ms=24000,
        )

        # Existing fields
        assert resp["status"]           == "success"
        assert resp["is_table"]         is True
        assert resp["row_count"]        == 3
        assert resp["conversation_id"] == APP_CONV_ID
        assert resp["execution_time_ms"] == 24000

        # Genie extensions
        assert resp["source"]                  == "genie"
        assert resp["genie_conversation_id"]   == GENIE_CONV_ID
        assert resp["genie_message_id"]        == MSG_ID
        assert resp["generated_sql"]           is not None
        assert len(resp["suggested_questions"]) == 3
        assert resp["has_visualization"]       is True
        assert resp["attachment_types"]        == ["text", "query", "viz", "suggested_questions"]

    def test_table_data_has_three_rows(self):
        msg = _make_full_message()
        qr  = _make_query_result_list_rows()
        resp = map_genie_message_to_chat_response(msg, query_result=qr)
        assert len(resp["table_data"]["rows"]) == 3

    def test_message_is_from_text_attachment(self):
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg)
        assert "5 000 shipments" in resp["message"] or "5000" in resp["message"]


# =============================================================================
# TEST 9 & 10: FAILED / CANCELLED → safe error status
# =============================================================================


class TestFailedMessage:
    def _make_failed_message(self, status: str) -> GenieMessage:
        return GenieMessage(
            id=MSG_ID,
            space_id="space-001",
            conversation_id=GENIE_CONV_ID,
            status=status,
            content="query",
            message_id=MSG_ID,
        )

    def test_failed_status_returns_error(self):
        msg = self._make_failed_message("FAILED")
        resp = map_genie_message_to_chat_response(
            msg,
            app_conversation_id=APP_CONV_ID,
        )
        assert resp["status"] == "error"
        assert resp["is_table"] is False
        assert resp["table_data"] is None

    def test_cancelled_status_returns_error(self):
        msg = self._make_failed_message("CANCELLED")
        resp = map_genie_message_to_chat_response(msg)
        assert resp["status"] == "error"

    def test_no_stack_trace_in_error_message(self):
        msg = self._make_failed_message("FAILED")
        resp = map_genie_message_to_chat_response(msg)
        assert "Traceback" not in resp["message"]
        assert "Exception" not in resp["message"]
        assert "File \"" not in resp["message"]

    def test_error_response_has_all_contract_fields(self):
        msg = self._make_failed_message("FAILED")
        resp = map_genie_message_to_chat_response(
            msg,
            app_conversation_id=APP_CONV_ID,
            genie_conversation_id=GENIE_CONV_ID,
        )
        assert resp["conversation_id"]        == APP_CONV_ID
        assert resp["genie_conversation_id"]  == GENIE_CONV_ID
        assert resp["source"]                 == "genie"


# =============================================================================
# TEST 11: debug=False → debug_info is None
# =============================================================================


class TestDebugFalse:
    def test_debug_info_is_none(self):
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg, debug=False)
        assert resp["debug_info"] is None

    def test_generated_sql_still_present_for_audit(self):
        """generated_sql is always returned (for audit logging), even in non-debug mode."""
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg, debug=False)
        assert resp["generated_sql"] is not None
        assert "SELECT" in resp["generated_sql"].upper()


# =============================================================================
# TEST 12: debug=True → debug_info includes SQL and thoughts
# =============================================================================


class TestDebugTrue:
    def test_debug_info_is_populated(self):
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg, debug=True)

        debug = resp["debug_info"]
        assert debug is not None
        assert isinstance(debug, dict)

    def test_debug_info_contains_sql(self):
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg, debug=True)

        debug = resp["debug_info"]
        assert "generated_sql" in debug
        assert debug["generated_sql"] is not None

    def test_debug_info_contains_thoughts(self):
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg, debug=True)

        debug = resp["debug_info"]
        assert "thoughts" in debug
        assert len(debug["thoughts"]) == 2

    def test_debug_info_contains_statement_id(self):
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg, debug=True)

        debug = resp["debug_info"]
        assert debug.get("statement_id") == STMT_ID

    def test_debug_info_contains_attachment_types(self):
        msg = _make_full_message()
        resp = map_genie_message_to_chat_response(msg, debug=True)

        debug = resp["debug_info"]
        assert "attachment_types" in debug
        assert "query" in debug["attachment_types"]


# =============================================================================
# TEST 13: Quick-summary text-only response works without SQL
# =============================================================================


class TestQuickSummaryTextOnly:
    """Validates the T3 'quick summary' scenario from the live smoke test.

    Genie answered from conversation context with only a text attachment
    and no SQL attachment. The mapper must handle this cleanly.
    """

    def test_text_only_summary_returns_success(self):
        msg = _make_text_only_message(
            "There are **4,044 shipments** from the US via air currently in transit."
        )
        resp = map_genie_message_to_chat_response(
            msg,
            app_conversation_id=APP_CONV_ID,
            genie_conversation_id=GENIE_CONV_ID,
            execution_time_ms=8778,
        )

        assert resp["status"] == "success"
        assert "4,044" in resp["message"] or "4044" in resp["message"]

    def test_no_table_when_no_query_result(self):
        msg = _make_text_only_message("Summary: 4,044 shipments in transit.")
        resp = map_genie_message_to_chat_response(msg)

        assert resp["is_table"] is False
        assert resp["table_data"] is None
        assert resp["row_count"] == 0

    def test_no_sql_when_text_only(self):
        msg = _make_text_only_message("Summary text.")
        resp = map_genie_message_to_chat_response(msg)
        assert resp["generated_sql"] is None

    def test_text_only_includes_only_text_in_attachment_types(self):
        msg = _make_text_only_message("Summary.")
        resp = map_genie_message_to_chat_response(msg)
        assert resp["attachment_types"] == ["text"]


# =============================================================================
# TEST 14: None message returns safe error
# =============================================================================


class TestNoneMessage:
    def test_none_returns_error_status(self):
        resp = map_genie_message_to_chat_response(
            None, app_conversation_id=APP_CONV_ID
        )
        assert resp["status"] == "error"
        assert resp["message"] != ""

    def test_none_contract_fields_present(self):
        resp = map_genie_message_to_chat_response(None)
        for field in ["status", "message", "is_table", "table_data",
                      "row_count", "download_key", "execution_time_ms",
                      "conversation_id", "clarification"]:
            assert field in resp, f"Missing field: {field}"


# =============================================================================
# TEST 15: Multiple text attachments are concatenated
# =============================================================================


class TestMultipleTextAttachments:
    def test_two_text_attachments_concatenated(self):
        msg = GenieMessage(
            id=MSG_ID,
            space_id="space-001",
            conversation_id=GENIE_CONV_ID,
            status="COMPLETED",
            content="query",
            message_id=MSG_ID,
            text_attachments=[
                GenieTextAttachment(content="Part one of the answer."),
                GenieTextAttachment(content="Part two with details."),
            ],
        )
        resp = map_genie_message_to_chat_response(msg)

        assert "Part one" in resp["message"]
        assert "Part two" in resp["message"]


# =============================================================================
# TEST 16: Row count reflects actual returned rows
# =============================================================================


class TestRowCountAccuracy:
    def test_row_count_matches_rows_not_total_row_count(self):
        """row_count must reflect the rows actually returned (3),
        not the total_row_count from the manifest (5000).
        """
        msg = _make_full_message()
        qr  = _make_query_result_list_rows()  # 3 rows, total=5000
        resp = map_genie_message_to_chat_response(msg, query_result=qr)

        assert resp["row_count"] == 3  # actual rows, not 5000

    def test_zero_rows_returns_no_table(self):
        msg = _make_full_message()
        qr  = GenieQueryResult(
            statement_id=STMT_ID, status="SUCCEEDED",
            columns=_COLUMNS, rows=[], row_count=0,
        )
        resp = map_genie_message_to_chat_response(msg, query_result=qr)

        assert resp["is_table"] is False
        assert resp["table_data"] is None
        assert resp["row_count"] == 0


# =============================================================================
# STANDALONE RUNNER
# =============================================================================

if __name__ == "__main__":
    import traceback

    test_classes = [
        TestTextOnlyMessage,
        TestQueryAttachment,
        TestQueryResultListRows,
        TestQueryResultDictRows,
        TestSuggestedQuestions,
        TestVizAttachment,
        TestVizNoChartSpec,
        TestFullMessageMapping,
        TestFailedMessage,
        TestDebugFalse,
        TestDebugTrue,
        TestQuickSummaryTextOnly,
        TestNoneMessage,
        TestMultipleTextAttachments,
        TestRowCountAccuracy,
    ]

    passed = failed = total = 0
    print("=" * 70)
    print("PHASE G2: genie_response_mapper.py unit tests")
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
        import sys
        sys.exit(1)
