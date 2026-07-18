"""Unit tests for app/services/genie_client.py.

All Genie REST API calls are mocked — no real HTTP calls are made.
Databricks SDK is mocked at the module level — no workspace connection needed.
DATABRICKS_TOKEN is pre-set to a fake value to bypass SDK credential chain.

Test cases:
    1.  list_spaces returns a list of GenieSpace objects.
    2.  find_space_by_name finds the target space (case-insensitive).
    3.  find_space_by_name returns None when space is missing.
    4.  start_conversation returns conversation_id and message_id.
    5.  send_message returns message_id for follow-up.
    6.  wait_for_message_completion polls until COMPLETED.
    7.  wait_for_message_completion raises GenieTimeoutError on timeout.
    8.  FAILED status raises GenieExecutionError.
    9.  CANCELLED status raises GenieExecutionError.
    10. extract_text_attachments returns markdown text.
    11. extract_query_attachments returns SQL and statement_id.
    12. extract_viz_attachments returns query_attachment_id only — no chart spec.
    13. extract_suggested_questions returns question list.
    14. extract_generated_sql extracts the first SQL string.
    15. extract_statement_ids returns all statement IDs.
    16. parse_query_result_response handles Genie /query-result shape.
    17. parse_query_result_response handles Statement Execution API shape with rows.
    18. No token or secret appears in any logged or raised message.
"""

import sys
import os
import time
from typing import Any, Dict
from unittest.mock import MagicMock, patch, call

sys.path.insert(
    0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app"
)

# ---------------------------------------------------------------------------
# Patch the Databricks SDK import before the module under test is loaded.
# This prevents WorkspaceClient() from trying to contact the workspace.
# Also set a fake DATABRICKS_TOKEN so _get_headers never hits the SDK.
# ---------------------------------------------------------------------------
os.environ["DATABRICKS_TOKEN"] = "test-fake-token-do-not-use"
os.environ["DATABRICKS_HOST"] = "https://fake-workspace.databricks.com"

_mock_ws = MagicMock()
_mock_ws.config.authenticate.return_value = {
    "Authorization": "Bearer test-fake-token-do-not-use"
}

with patch("databricks.sdk.WorkspaceClient", return_value=_mock_ws):
    from app.services.genie_client import (
        GenieClient,
        GenieClientError,
        GenieExecutionError,
        GenieTimeoutError,
        GenieMessage,
        GenieQueryAttachment,
        GenieTextAttachment,
        GenieVizAttachment,
        GenieQueryResult,
    )
    # _parse_message is a private static method on GenieClient, not a
    # module-level export. It is exercised via client.get_message() in all tests.


# ---------------------------------------------------------------------------
# Helpers: canonical mock responses
# ---------------------------------------------------------------------------

SPACE_ID = "01f17a93e6aa1b97a9da7ef329e15e46"
CONV_ID  = "01f17c4b25f511d3afbee0ed4b2444e5"
MSG_ID   = "01f17c4b25fd11058e9ce978cb887fdf"
STMT_ID  = "01f17c4b-291b-127a-8c98-8971a158dbdb"
ATT_QUERY_ID = "01f17c4b28ed169faafff273c1fd9c82"
ATT_VIZ_ID   = "01f17c4b30bb1d32ba232dee251c64cb"


def _make_response(body: Dict[str, Any], status_code: int = 200) -> MagicMock:
    """Build a fake requests.Response."""
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = body
    r.text = str(body)
    return r


def _spaces_response() -> Dict:
    return {
        "spaces": [
            {
                "space_id": SPACE_ID,
                "title": "TransparencE Shipment Intelligence",
                "warehouse_id": "8e46614f7064d8fd",
            },
            {
                "space_id": "other-space-id",
                "title": "Other Space",
                "warehouse_id": "abc123",
            },
        ]
    }


def _start_conv_response() -> Dict:
    return {
        "conversation_id": CONV_ID,
        "message_id": MSG_ID,
        "message": {
            "id": MSG_ID,
            "space_id": SPACE_ID,
            "conversation_id": CONV_ID,
            "status": "SUBMITTED",
            "content": "shipments from US via air",
            "message_id": MSG_ID,
        },
        "conversation": {
            "id": CONV_ID,
            "space_id": SPACE_ID,
            "title": "shipments from US via air",
            "conversation_id": CONV_ID,
        },
    }


def _completed_message() -> Dict:
    return {
        "id": MSG_ID,
        "space_id": SPACE_ID,
        "conversation_id": CONV_ID,
        "user_id": 72686855340605,
        "created_timestamp": 1783679762692,
        "last_updated_timestamp": 1783679800000,
        "status": "COMPLETED",
        "content": "shipments from US via air",
        "message_id": MSG_ID,
        "attachments": [
            {
                "attachment_id": ATT_QUERY_ID,
                "query": {
                    "query": "SELECT * FROM `onedata_fn_ion_dev`.`ion_l0_raw`.`lbn_with_scorecard` WHERE source_ = 'US'",
                    "description": "All US shipments via air.",
                    "statement_id": STMT_ID,
                    "query_result_metadata": {"row_count": 5000},
                    "thoughts": [
                        {"thought_type": "THOUGHT_TYPE_DESCRIPTION",
                         "content": "Filtering on source_='US' and air mode."}
                    ],
                },
            },
            {
                "text": {
                    "content": "**5 000 shipments** found from the US via air."
                }
            },
            {
                "attachment_id": ATT_VIZ_ID,
                "viz": {"query_attachment_id": ATT_QUERY_ID},
            },
            {
                "attachment_id": "sugg-id",
                "suggested_questions": {
                    "questions": [
                        "What are the top destinations?",
                        "Which are in transit?",
                        "Show revenue by BU.",
                    ]
                },
            },
        ],
        "query_result": {"statement_id": STMT_ID, "row_count": 5000},
    }


def _stmt_exec_response() -> Dict:
    """Simulate Statement Execution API response with rows."""
    return {
        "statement_id": STMT_ID,
        "status": {"state": "SUCCEEDED"},
        "manifest": {
            "format": "JSON_ARRAY",
            "total_row_count": 5000,
            "schema": {
                "column_count": 3,
                "columns": [
                    {"name": "shipment_number_id", "type_text": "STRING", "position": 0},
                    {"name": "source_", "type_text": "STRING", "position": 1},
                    {"name": "shipment_quantity", "type_text": "DOUBLE", "position": 2},
                ],
            },
        },
        "result": {
            "data_array": [
                ["SHP-001", "US", "127.0"],
                ["SHP-002", "US", "588.0"],
            ]
        },
    }


def _genie_query_result_response() -> Dict:
    """Simulate Genie /query-result endpoint (schema only, no rows)."""
    return {
        "statement_response": {
            "statement_id": STMT_ID,
            "status": {"state": "SUCCEEDED"},
            "manifest": {
                "format": "PROTOBUF_ARRAY",
                "total_row_count": 5000,
                "schema": {
                    "column_count": 3,
                    "columns": [
                        {"name": "shipment_number_id", "type_text": "STRING", "position": 0},
                        {"name": "source_", "type_text": "STRING", "position": 1},
                        {"name": "shipment_quantity", "type_text": "DOUBLE", "position": 2},
                    ],
                },
            },
        }
    }


def _make_client() -> GenieClient:
    """Return a GenieClient with SDK mocked out."""
    with patch("databricks.sdk.WorkspaceClient", return_value=_mock_ws):
        return GenieClient(host="https://fake-workspace.databricks.com")


# =============================================================================
# TEST 1: list_spaces returns GenieSpace objects
# =============================================================================


class TestListSpaces:
    def test_returns_list_of_spaces(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response(_spaces_response())):
            spaces = client.list_spaces()

        assert len(spaces) == 2
        assert spaces[0].space_id == SPACE_ID
        assert spaces[0].title == "TransparencE Shipment Intelligence"
        assert spaces[0].warehouse_id == "8e46614f7064d8fd"

    def test_empty_workspace_returns_empty_list(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response({"spaces": []})):
            spaces = client.list_spaces()
        assert spaces == []


# =============================================================================
# TEST 2 & 3: find_space_by_name
# =============================================================================


class TestFindSpaceByName:
    def test_finds_transparence_space(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response(_spaces_response())):
            space = client.find_space_by_name("TransparencE Shipment Intelligence")

        assert space is not None
        assert space.space_id == SPACE_ID

    def test_case_insensitive_match(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response(_spaces_response())):
            space = client.find_space_by_name("transparence shipment intelligence")

        assert space is not None
        assert space.space_id == SPACE_ID

    def test_returns_none_when_not_found(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response(_spaces_response())):
            space = client.find_space_by_name("Non-Existent Space")

        assert space is None


# =============================================================================
# TEST 4: start_conversation returns conversation_id and message_id
# =============================================================================


class TestStartConversation:
    def test_returns_ids(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response(_start_conv_response())):
            result = client.start_conversation(SPACE_ID, "shipments from US via air")

        assert result["conversation_id"] == CONV_ID
        assert result["message_id"] == MSG_ID

    def test_posts_to_correct_url(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response(_start_conv_response())) as mock_req:
            client.start_conversation(SPACE_ID, "test query")

        args, kwargs = mock_req.call_args
        url = args[1] if len(args) > 1 else kwargs.get("url", "")
        assert "start-conversation" in url
        assert SPACE_ID in url
        # Confirm message is sent as JSON content field
        assert kwargs.get("json", {}).get("content") == "test query"


# =============================================================================
# TEST 5: send_message returns message_id for follow-up
# =============================================================================


class TestSendMessage:
    _FOLLOWUP_MSG_ID = "01f17c4b54de1b779063d073ba467c0c"

    def _followup_response(self) -> Dict:
        return {
            "message_id": self._FOLLOWUP_MSG_ID,
            "id": self._FOLLOWUP_MSG_ID,
        }

    def test_returns_followup_message_id(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response(self._followup_response())):
            result = client.send_message(SPACE_ID, CONV_ID, "which are in transit?")

        assert result["message_id"] == self._FOLLOWUP_MSG_ID

    def test_posts_to_messages_endpoint(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response(self._followup_response())) as mock_req:
            client.send_message(SPACE_ID, CONV_ID, "which are in transit?")

        args, kwargs = mock_req.call_args
        url = args[1] if len(args) > 1 else kwargs.get("url", "")
        assert "conversations" in url
        assert CONV_ID in url
        assert "messages" in url
        assert kwargs.get("json", {}).get("content") == "which are in transit?"


# =============================================================================
# TEST 6: wait_for_message_completion polls until COMPLETED
# =============================================================================


class TestWaitForCompletion:
    def test_polls_through_intermediate_statuses(self):
        client = _make_client()

        # Simulate: SUBMITTED → ASKING_AI → COMPLETED
        intermediate_1 = dict(_completed_message(), status="SUBMITTED")
        intermediate_2 = dict(_completed_message(), status="ASKING_AI")
        final          = _completed_message()  # status=COMPLETED

        responses = [
            _make_response(intermediate_1),
            _make_response(intermediate_2),
            _make_response(final),
        ]

        with patch("requests.request", side_effect=responses), \
             patch("time.sleep"):  # skip actual sleeps in tests
            msg = client.wait_for_message_completion(
                SPACE_ID, CONV_ID, MSG_ID, timeout_seconds=60, poll_interval_seconds=0.01
            )

        assert msg.status == "COMPLETED"
        assert msg.message_id == MSG_ID

    def test_returns_immediately_if_already_completed(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response(_completed_message())), \
             patch("time.sleep") as mock_sleep:
            msg = client.wait_for_message_completion(
                SPACE_ID, CONV_ID, MSG_ID, timeout_seconds=60
            )

        assert msg.status == "COMPLETED"
        mock_sleep.assert_not_called()  # no sleep needed for immediate completion


# =============================================================================
# TEST 7: wait_for_message_completion raises GenieTimeoutError on timeout
# =============================================================================


class TestWaitTimeout:
    def test_raises_timeout_error(self):
        client = _make_client()
        always_asking = dict(_completed_message(), status="ASKING_AI")

        import pytest
        with patch("requests.request", return_value=_make_response(always_asking)), \
             patch("time.sleep"), \
             patch("time.monotonic", side_effect=[0.0, 0.5, 200.0]):
            try:
                client.wait_for_message_completion(
                    SPACE_ID, CONV_ID, MSG_ID,
                    timeout_seconds=1,
                    poll_interval_seconds=0.01,
                )
                assert False, "Expected GenieTimeoutError"
            except GenieTimeoutError as e:
                assert "did not complete" in str(e).lower() or "timeout" in str(e).lower()


# =============================================================================
# TEST 8 & 9: FAILED / CANCELLED status raises GenieExecutionError
# =============================================================================


class TestFailedCancelledStatus:
    def _run_with_status(self, status: str):
        client = _make_client()
        failed_msg = dict(_completed_message(), status=status)
        with patch("requests.request", return_value=_make_response(failed_msg)), \
             patch("time.sleep"):
            client.wait_for_message_completion(SPACE_ID, CONV_ID, MSG_ID)

    def test_failed_raises_execution_error(self):
        try:
            self._run_with_status("FAILED")
            assert False, "Expected GenieExecutionError"
        except GenieExecutionError as e:
            assert "FAILED" in str(e)

    def test_cancelled_raises_execution_error(self):
        try:
            self._run_with_status("CANCELLED")
            assert False, "Expected GenieExecutionError"
        except GenieExecutionError as e:
            assert "CANCELLED" in str(e)


# =============================================================================
# TEST 10: extract_text_attachments returns markdown text
# =============================================================================


class TestExtractTextAttachments:
    def test_returns_markdown_text(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response(_completed_message())):
            msg = client.get_message(SPACE_ID, CONV_ID, MSG_ID)

        texts = client.extract_text_attachments(msg)
        assert len(texts) == 1
        assert "5 000 shipments" in texts[0].content or "5000" in texts[0].content

    def test_text_is_markdown_string(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response(_completed_message())):
            msg = client.get_message(SPACE_ID, CONV_ID, MSG_ID)

        texts = client.extract_text_attachments(msg)
        # Text may include markdown formatting (**, #, etc.)
        assert isinstance(texts[0].content, str)
        assert len(texts[0].content) > 0


# =============================================================================
# TEST 11: extract_query_attachments returns SQL and statement_id
# =============================================================================


class TestExtractQueryAttachments:
    def test_returns_sql_and_statement_id(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response(_completed_message())):
            msg = client.get_message(SPACE_ID, CONV_ID, MSG_ID)

        queries = client.extract_query_attachments(msg)
        assert len(queries) == 1
        assert queries[0].statement_id == STMT_ID
        assert "SELECT" in queries[0].sql.upper()
        assert queries[0].row_count == 5000
        assert queries[0].description != ""

    def test_thoughts_are_captured(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response(_completed_message())):
            msg = client.get_message(SPACE_ID, CONV_ID, MSG_ID)

        queries = client.extract_query_attachments(msg)
        assert len(queries[0].thoughts) == 1
        assert queries[0].thoughts[0]["thought_type"] == "THOUGHT_TYPE_DESCRIPTION"


# =============================================================================
# TEST 12: extract_viz_attachments returns query_attachment_id only — no chart spec
# =============================================================================


class TestExtractVizAttachments:
    def test_returns_query_attachment_id_only(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response(_completed_message())):
            msg = client.get_message(SPACE_ID, CONV_ID, MSG_ID)

        vizs = client.extract_viz_attachments(msg)
        assert len(vizs) == 1
        assert vizs[0].query_attachment_id == ATT_QUERY_ID

    def test_viz_has_no_chart_spec(self):
        """Genie does not expose a chart spec. The viz object must NOT contain
        chart_spec, vega_lite, plotly, image_url, or any chart format field."""
        client = _make_client()
        with patch("requests.request", return_value=_make_response(_completed_message())):
            msg = client.get_message(SPACE_ID, CONV_ID, MSG_ID)

        vizs = client.extract_viz_attachments(msg)
        viz = vizs[0]
        # Check the dataclass has no unexpected chart spec attributes
        viz_dict = viz.__dict__
        chart_spec_keys = {"chart_spec", "vega_lite", "plotly", "image_url",
                           "spec", "chart_type", "chart_data"}
        assert chart_spec_keys.isdisjoint(viz_dict.keys()), (
            f"Unexpected chart spec keys found in viz attachment: "
            f"{chart_spec_keys & set(viz_dict.keys())}"
        )


# =============================================================================
# TEST 13: extract_suggested_questions returns question list
# =============================================================================


class TestExtractSuggestedQuestions:
    def test_returns_three_questions(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response(_completed_message())):
            msg = client.get_message(SPACE_ID, CONV_ID, MSG_ID)

        questions = client.extract_suggested_questions(msg)
        assert len(questions) == 3
        assert all(isinstance(q, str) for q in questions)
        assert any("transit" in q.lower() for q in questions)


# =============================================================================
# TEST 14: extract_generated_sql extracts the first SQL string
# =============================================================================


class TestExtractGeneratedSQL:
    def test_returns_first_sql_string(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response(_completed_message())):
            msg = client.get_message(SPACE_ID, CONV_ID, MSG_ID)

        sql = client.extract_generated_sql(msg)
        assert sql is not None
        assert "SELECT" in sql.upper()
        assert "lbn_with_scorecard" in sql

    def test_returns_none_when_no_query_attachment(self):
        client = _make_client()
        # Message with only a text attachment
        text_only = dict(_completed_message(), attachments=[
            {"text": {"content": "No SQL was needed."}}
        ])
        with patch("requests.request", return_value=_make_response(text_only)):
            msg = client.get_message(SPACE_ID, CONV_ID, MSG_ID)

        sql = client.extract_generated_sql(msg)
        assert sql is None


# =============================================================================
# TEST 15: extract_statement_ids returns all statement IDs
# =============================================================================


class TestExtractStatementIDs:
    def test_returns_list_of_ids(self):
        client = _make_client()
        with patch("requests.request", return_value=_make_response(_completed_message())):
            msg = client.get_message(SPACE_ID, CONV_ID, MSG_ID)

        ids = client.extract_statement_ids(msg)
        assert STMT_ID in ids

    def test_empty_when_no_query_attachments(self):
        client = _make_client()
        text_only = dict(_completed_message(), attachments=[
            {"text": {"content": "No SQL."}}
        ])
        with patch("requests.request", return_value=_make_response(text_only)):
            msg = client.get_message(SPACE_ID, CONV_ID, MSG_ID)

        ids = client.extract_statement_ids(msg)
        assert ids == []


# =============================================================================
# TEST 16: parse_query_result_response handles Genie /query-result shape
# =============================================================================


class TestParseQueryResultGenieShape:
    def test_parses_schema_columns(self):
        client = _make_client()
        result = client.parse_query_result_response(_genie_query_result_response())

        assert result.statement_id == STMT_ID
        assert result.status == "SUCCEEDED"
        assert len(result.columns) == 3
        assert result.columns[0]["name"] == "shipment_number_id"
        assert result.format == "PROTOBUF_ARRAY"

    def test_total_row_count_from_manifest(self):
        client = _make_client()
        result = client.parse_query_result_response(_genie_query_result_response())
        assert result.total_row_count == 5000

    def test_no_rows_when_schema_only(self):
        """Genie /query-result endpoint returns schema only — no row data."""
        client = _make_client()
        result = client.parse_query_result_response(_genie_query_result_response())
        assert result.rows == []
        assert result.row_count == 0


# =============================================================================
# TEST 17: parse_query_result_response handles Statement Execution API shape
# =============================================================================


class TestParseQueryResultStmtShape:
    def test_parses_rows_and_schema(self):
        client = _make_client()
        result = client.parse_query_result_response(_stmt_exec_response())

        assert result.statement_id == STMT_ID
        assert result.status == "SUCCEEDED"
        assert len(result.columns) == 3
        assert len(result.rows) == 2
        assert result.rows[0][0] == "SHP-001"
        assert result.row_count == 2

    def test_handles_failed_status_with_error(self):
        failed = {
            "statement_id": STMT_ID,
            "status": {
                "state": "FAILED",
                "error": {"message": "Table not found: lbn_with_scorecard"},
            },
            "manifest": {"schema": {"columns": []}},
        }
        client = _make_client()
        result = client.parse_query_result_response(failed)

        assert result.status == "FAILED"
        assert result.error is not None
        assert "lbn_with_scorecard" in result.error


# =============================================================================
# TEST 18: No token or secret appears in logs or exceptions
# =============================================================================


class TestNoSecretLeakage:
    """Verify that auth tokens are never exposed in error messages or logs."""

    def test_http_error_message_has_no_token(self):
        """GenieClientError message should not include the bearer token."""
        client = _make_client()
        error_resp = MagicMock()
        error_resp.status_code = 403
        error_resp.json.side_effect = ValueError
        error_resp.text = "Permission denied"

        with patch("requests.request", return_value=error_resp):
            try:
                client.list_spaces()
                assert False, "Expected GenieClientError"
            except GenieClientError as e:
                # The exception message must not contain the token value
                assert "test-fake-token-do-not-use" not in str(e)
                assert "Bearer" not in str(e)  # no Authorization header leak

    def test_token_not_in_auth_header_value_in_error(self):
        """Even if SDK returns a token, we must not log/print it."""
        # The _get_headers method is designed to never log the token
        client = _make_client()
        # Confirm the method returns a dict with Authorization key
        # but we cannot see the token value from outside the method
        headers = client._get_headers()
        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Bearer ")
        # Confirm it does NOT log the token (via logger inspection)
        import logging
        import io
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        logging.getLogger("app.services.genie_client").addHandler(handler)
        _ = client._get_headers()  # call again to trigger any logging
        log_output = log_stream.getvalue()
        logging.getLogger("app.services.genie_client").removeHandler(handler)
        assert "test-fake-token-do-not-use" not in log_output


# =============================================================================
# STANDALONE RUNNER (for direct execution without pytest)
# =============================================================================

if __name__ == "__main__":
    import traceback

    test_classes = [
        TestListSpaces,
        TestFindSpaceByName,
        TestStartConversation,
        TestSendMessage,
        TestWaitForCompletion,
        TestWaitTimeout,
        TestFailedCancelledStatus,
        TestExtractTextAttachments,
        TestExtractQueryAttachments,
        TestExtractVizAttachments,
        TestExtractSuggestedQuestions,
        TestExtractGeneratedSQL,
        TestExtractStatementIDs,
        TestParseQueryResultGenieShape,
        TestParseQueryResultStmtShape,
        TestNoSecretLeakage,
    ]

    passed = 0
    failed = 0
    total  = 0

    print("=" * 70)
    print("PHASE G1: genie_client.py unit tests")
    print("=" * 70)

    for cls in test_classes:
        instance = cls()
        methods  = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in methods:
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
        sys.exit(1)
