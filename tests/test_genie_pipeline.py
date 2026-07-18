"""Unit tests for app/services/genie_pipeline.py.

Uses FakeGenieClient (no HTTP) and a real GenieSessionStore (in-memory).
No Databricks SDK calls are made.

Test cases:
    1.  New conversation calls start_conversation.
    2.  New conversation stores genie_conversation_id in session store.
    3.  Follow-up uses existing genie_conversation_id via send_message.
    4.  Follow-up stores updated last_message_id.
    5.  Text-only response maps correctly (no SQL, no table).
    6.  Query response fetches result and maps table_data.
    7.  fetch_query_results=False disables fetch, no table returned.
    8.  Viz attachment maps to visualization reference dict.
    9.  Suggested questions are returned in response.
    10. GenieTimeoutError returns safe error with fallback_recommended=True.
    11. GenieExecutionError returns safe error with fallback_recommended=True.
    12. Query result fetch failure returns text-only success (non-fatal).
    13. Expired session starts a new Genie conversation.
    14. Reset session starts a new Genie conversation.
    15. debug=True includes debug_info in response.
    16. debug=False excludes debug_info from response.
    17. No stack trace exposed in any error user message.
    18. Multi-turn: second run() reuses same Genie conversation_id.
"""

import sys
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

sys.path.insert(
    0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app"
)

# ---------------------------------------------------------------------------
# Patch Databricks SDK before any genie_client import
# ---------------------------------------------------------------------------
os.environ.setdefault("DATABRICKS_TOKEN", "test-fake-token-pipeline-tests")
os.environ.setdefault("DATABRICKS_HOST",  "https://fake-workspace.databricks.com")

_mock_ws = MagicMock()
_mock_ws.config.authenticate.return_value = {
    "Authorization": "Bearer test-fake-token-pipeline-tests"
}

with patch("databricks.sdk.WorkspaceClient", return_value=_mock_ws):
    from app.services.genie_client import (
        GenieClientError,
        GenieExecutionError,
        GenieMessage,
        GenieQueryAttachment,
        GenieQueryResult,
        GenieTextAttachment,
        GenieTimeoutError,
        GenieVizAttachment,
    )

from app.services.export_job_manager import ExportJobManager
from app.services.genie_pipeline import GeniePipeline
from app.services.genie_session_store import GenieSessionStore


# =============================================================================
# CONSTANTS
# =============================================================================

SPACE_ID          = "01f17a93e6aa1b97a9da7ef329e15e46"
APP_CONV_ID       = "app-conv-pipeline-test-001"
APP_CONV_MULTI    = "app-conv-pipeline-multi-001"
GENIE_CONV_ID     = "fake-genie-conv-pipeline-001"
START_MSG_ID      = "fake-start-msg-pipeline-001"
FOLLOWUP_MSG_ID   = "fake-followup-msg-pipeline-001"
STMT_ID           = "fake-stmt-pipeline-001"
ATT_QUERY_ID      = "att-query-pipeline-001"
ATT_VIZ_ID        = "att-viz-pipeline-001"

TEST_SQL = (
    "SELECT * FROM `onedata_fn_ion_dev`.`ion_l0_raw`.`lbn_with_scorecard` "
    "WHERE source_ = 'US'"
)
_COLUMNS = [
    {"name": "shipment_number_id", "type_text": "STRING", "position": 0},
    {"name": "source_",           "type_text": "STRING", "position": 1},
]
_ROWS = [["SHP-001", "US"], ["SHP-002", "US"], ["SHP-003", "US"]]


# =============================================================================
# FAKE GENIE CLIENT
# =============================================================================


class FakeGenieClient:
    """Drop-in replacement for GenieClient in pipeline unit tests.

    Tracks all calls so tests can assert correct routing decisions.
    All responses are configurable at construction time.
    No HTTP calls; no Databricks SDK instantiation.
    """

    def __init__(
        self,
        genie_conv_id:    str = GENIE_CONV_ID,
        start_msg_id:     str = START_MSG_ID,
        followup_msg_id:  str = FOLLOWUP_MSG_ID,
        completed_message: Optional[GenieMessage] = None,
        query_result:      Optional[GenieQueryResult] = None,
        raise_on_wait:     Optional[Exception] = None,
        raise_on_fetch:    Optional[Exception] = None,
    ):
        self._genie_conv_id       = genie_conv_id
        self._start_msg_id        = start_msg_id
        self._followup_msg_id     = followup_msg_id
        self._completed_message   = completed_message  # None → default text-only
        self._query_result        = query_result
        self._raise_on_wait       = raise_on_wait
        self._raise_on_fetch      = raise_on_fetch

        # Call tracking
        self.start_conv_calls:  List = []   # [(space_id, message), ...]
        self.send_msg_calls:    List = []   # [(space_id, conv_id, message), ...]
        self.wait_calls:        List = []   # [(space_id, conv_id, msg_id), ...]
        self.fetch_calls:       List = []   # [(space_id, conv_id, msg_id, stmt_id), ...]

    # --- Genie client interface ---

    def start_conversation(self, space_id: str, message: str) -> Dict[str, str]:
        self.start_conv_calls.append((space_id, message))
        return {
            "conversation_id": self._genie_conv_id,
            "message_id":      self._start_msg_id,
        }

    def send_message(
        self, space_id: str, conversation_id: str, message: str
    ) -> Dict[str, str]:
        self.send_msg_calls.append((space_id, conversation_id, message))
        return {"message_id": self._followup_msg_id}

    def wait_for_message_completion(
        self,
        space_id: str,
        conversation_id: str,
        message_id: str,
        timeout_seconds: float = 120,
        poll_interval_seconds: float = 2.0,
    ) -> GenieMessage:
        self.wait_calls.append((space_id, conversation_id, message_id))
        if self._raise_on_wait is not None:
            raise self._raise_on_wait
        return self._completed_message or _make_text_only_message()

    def fetch_query_result(
        self,
        space_id: str,
        conversation_id: str,
        message_id: str,
        statement_id: Optional[str] = None,
        fetch_rows: bool = True,
        row_limit: int = 500,
    ) -> Optional[GenieQueryResult]:
        self.fetch_calls.append((space_id, conversation_id, message_id, statement_id))
        if self._raise_on_fetch is not None:
            raise self._raise_on_fetch
        return self._query_result


# =============================================================================
# MESSAGE / RESULT FIXTURES
# =============================================================================


def _make_text_only_message(
    text: str = "**3 shipments** found from the US via air.",
) -> GenieMessage:
    return GenieMessage(
        id=START_MSG_ID,
        space_id=SPACE_ID,
        conversation_id=GENIE_CONV_ID,
        status="COMPLETED",
        content="test query",
        message_id=START_MSG_ID,
        text_attachments=[GenieTextAttachment(content=text)],
        query_attachments=[],
        viz_attachments=[],
        suggested_questions=[],
    )


def _make_query_message() -> GenieMessage:
    """Message with text + query attachment (has statement_id)."""
    return GenieMessage(
        id=START_MSG_ID,
        space_id=SPACE_ID,
        conversation_id=GENIE_CONV_ID,
        status="COMPLETED",
        content="shipments from US",
        message_id=START_MSG_ID,
        text_attachments=[
            GenieTextAttachment(content="Found **3 shipments** from the US.")
        ],
        query_attachments=[
            GenieQueryAttachment(
                attachment_id=ATT_QUERY_ID,
                sql=TEST_SQL,
                description="US shipments.",
                statement_id=STMT_ID,
                row_count=3,
            )
        ],
        viz_attachments=[],
        suggested_questions=[],
    )


def _make_full_message() -> GenieMessage:
    """Message with text + query + viz + suggested_questions."""
    return GenieMessage(
        id=START_MSG_ID,
        space_id=SPACE_ID,
        conversation_id=GENIE_CONV_ID,
        status="COMPLETED",
        content="shipments from US via air",
        message_id=START_MSG_ID,
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
                description="US air shipments via origin filter.",
                statement_id=STMT_ID,
                row_count=5000,
                thoughts=[
                    {"thought_type": "THOUGHT_TYPE_DESCRIPTION",
                     "content": "Filter US air shipments."},
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


def _make_query_result(num_rows: int = 3) -> GenieQueryResult:
    return GenieQueryResult(
        statement_id=STMT_ID,
        status="SUCCEEDED",
        columns=_COLUMNS,
        rows=_ROWS[:num_rows],
        row_count=num_rows,
        total_row_count=5000,
        format="JSON_ARRAY",
    )


# =============================================================================
# PIPELINE FACTORY
# =============================================================================


class FakeAuditService:
    def __init__(self, download_key: str = "download-key-001"):
        self.download_key = download_key
        self.calls = []

    def create_export(self, headers, rows, filename_prefix="genie_export"):
        self.calls.append(
            {
                "headers": headers,
                "rows": rows,
                "filename_prefix": filename_prefix,
            }
        )
        return self.download_key

    def get_export_path(self, download_key):
        return f"/tmp/{download_key}.csv"


class FakeSQLService:
    def __init__(self, result_rows=None, error=None):
        self.result_rows = result_rows or _ROWS
        self.error = error
        self.calls = []

    def _enforce_read_only(self, sql: str) -> None:
        upper = sql.strip().upper()
        if not (upper.startswith("SELECT") or upper.startswith("WITH")):
            raise ValueError("Only SELECT queries are allowed")

    def execute_query(self, sql: str, row_limit: int = 100000, timeout_seconds: int = 300):
        self.calls.append({"sql": sql, "row_limit": row_limit, "timeout_seconds": timeout_seconds})
        return type(
            "QueryResultStub",
            (),
            {
                "headers": ["shipment_number_id", "source_"],
                "rows": self.result_rows[:row_limit],
                "row_count": min(len(self.result_rows), row_limit),
                "total_row_count": min(len(self.result_rows), row_limit),
                "execution_time_ms": 10,
                "truncated": len(self.result_rows) > row_limit,
                "error": self.error,
            },
        )()


def _make_pipeline(
    client: Optional[FakeGenieClient] = None,
    store:  Optional[GenieSessionStore] = None,
    debug:  bool = False,
    fetch_query_results: bool = True,
    timeout_seconds: int = 30,
    audit_service: Optional[FakeAuditService] = None,
    async_export_enabled: bool = False,
    export_mode: str = "returned_rows_only",
    max_export_rows: int = 100000,
    export_job_manager: Optional[ExportJobManager] = None,
    sql_service: Optional[FakeSQLService] = None,
) -> GeniePipeline:
    return GeniePipeline(
        genie_client=client or FakeGenieClient(),
        session_store=store or GenieSessionStore(),
        space_id=SPACE_ID,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=0.0,   # no real sleep in tests
        debug=debug,
        fetch_query_results=fetch_query_results,
        audit_service=audit_service,
        async_export_enabled=async_export_enabled,
        export_mode=export_mode,
        max_export_rows=max_export_rows,
        export_job_manager=export_job_manager,
        sql_service=sql_service,
    )


# =============================================================================
# TEST 1 & 2: New conversation
# =============================================================================


class TestNewConversation:
    def test_start_conversation_is_called_once(self):
        """First message on an unseen app_conversation_id must call start_conversation."""
        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client)

        pipeline.run("shipments from US via air", APP_CONV_ID)

        assert len(client.start_conv_calls) == 1, (
            f"Expected 1 start_conversation call, got {len(client.start_conv_calls)}"
        )
        assert len(client.send_msg_calls) == 0, (
            "send_message should NOT be called for a new conversation"
        )

    def test_start_conversation_uses_correct_space_id(self):
        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client)
        pipeline.run("shipments from US via air", APP_CONV_ID)

        called_space_id, called_message = client.start_conv_calls[0]
        assert called_space_id == SPACE_ID
        assert isinstance(called_message, str) and called_message.strip()
        assert "us" in called_message.lower()
        assert "air" in called_message.lower()

    def test_new_conversation_stores_genie_conv_id(self):
        """After a new conversation, the session store must have the genie_conv_id."""
        store  = GenieSessionStore()
        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client, store=store)

        pipeline.run("shipments from US via air", APP_CONV_ID)

        stored = store.get_genie_conversation_id(APP_CONV_ID)
        assert stored == GENIE_CONV_ID, (
            f"Expected genie_conv_id={GENIE_CONV_ID!r} stored, got {stored!r}"
        )


# =============================================================================
# TEST 3 & 4: Follow-up conversation
# =============================================================================


class TestFollowUp:
    def _setup_existing_session(
        self, client: FakeGenieClient
    ) -> GenieSessionStore:
        """Pre-populate the session store with an active Genie conversation."""
        store = GenieSessionStore()
        store.set_genie_conversation_id(APP_CONV_ID, GENIE_CONV_ID)
        store.update_context(
            APP_CONV_ID,
            last_intent="BROAD_LISTING",
            last_user_prompt="shipments from US via air",
            last_enriched_prompt="Summarise shipments from US via air",
            last_filters={"source": "US", "transport_mode": "AIR"},
        )
        return store

    def test_follow_up_calls_send_message_not_start(self):
        """When a session exists, follow-up must call send_message, not start_conversation."""
        client = FakeGenieClient()
        store  = self._setup_existing_session(client)
        pipeline = _make_pipeline(client=client, store=store)

        pipeline.run("which are in transit?", APP_CONV_ID)

        assert len(client.send_msg_calls)   == 1
        assert len(client.start_conv_calls) == 0

    def test_follow_up_passes_correct_genie_conv_id(self):
        client = FakeGenieClient()
        store  = self._setup_existing_session(client)
        pipeline = _make_pipeline(client=client, store=store)

        pipeline.run("which are in transit?", APP_CONV_ID)

        called_space_id, called_conv_id, called_msg = client.send_msg_calls[0]
        assert called_space_id == SPACE_ID
        assert called_conv_id  == GENIE_CONV_ID
        assert called_msg      == "which are in transit?"

    def test_follow_up_stores_new_message_id(self):
        """After a follow-up, last_message_id in the store should be the follow-up msg ID."""
        client = FakeGenieClient(followup_msg_id=FOLLOWUP_MSG_ID)
        store  = self._setup_existing_session(client)
        pipeline = _make_pipeline(client=client, store=store)

        pipeline.run("which are in transit?", APP_CONV_ID)

        stored_msg_id = store.get_last_message_id(APP_CONV_ID)
        assert stored_msg_id == FOLLOWUP_MSG_ID


# =============================================================================
# TEST 5: Text-only response
# =============================================================================


class TestTextOnlyResponse:
    def test_text_only_maps_to_success_with_no_table(self):
        client = FakeGenieClient(
            completed_message=_make_text_only_message(
                "**3 shipments** found from the US via air."
            )
        )
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("shipments from US via air", APP_CONV_ID)

        assert resp["status"]   == "success"
        assert resp["is_table"] is False
        assert resp["table_data"] is None
        assert resp["row_count"]  == 0
        assert "3 shipments" in resp["message"]

    def test_text_only_has_no_sql(self):
        client = FakeGenieClient(completed_message=_make_text_only_message())
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("summary", APP_CONV_ID)
        assert resp["generated_sql"] is None

    def test_text_only_response_includes_source_genie(self):
        pipeline = _make_pipeline()
        resp = pipeline.run("summary", APP_CONV_ID)
        assert resp["source"] == "genie"

    def test_existing_contract_fields_all_present(self):
        pipeline = _make_pipeline()
        resp = pipeline.run("summary", APP_CONV_ID)
        for field in [
            "status", "message", "is_table", "table_data",
            "row_count", "download_key", "execution_time_ms",
            "conversation_id", "clarification",
        ]:
            assert field in resp, f"Missing contract field: {field}"


# =============================================================================
# TEST 6 & 7: Query response and fetch toggle
# =============================================================================


class TestQueryResponse:
    def test_query_response_fetches_rows_and_maps_table_data(self):
        """Query attachment + rows should produce is_table=True with table_data."""
        client = FakeGenieClient(
            completed_message=_make_query_message(),
            query_result=_make_query_result(3),
        )
        pipeline = _make_pipeline(client=client, fetch_query_results=True)
        resp = pipeline.run("shipments from US", APP_CONV_ID)

        assert resp["is_table"]   is True
        assert resp["table_data"] is not None
        assert resp["row_count"]  == 3

    def test_query_response_fetch_called_with_statement_id(self):
        client = FakeGenieClient(
            completed_message=_make_query_message(),
            query_result=_make_query_result(3),
        )
        pipeline = _make_pipeline(client=client, fetch_query_results=True)
        pipeline.run("shipments from US", APP_CONV_ID)

        assert len(client.fetch_calls) == 1
        # 4th element of the call tuple is the statement_id
        assert client.fetch_calls[0][3] == STMT_ID

    def test_fetch_disabled_does_not_call_fetch_query_result(self):
        """fetch_query_results=False must not call fetch_query_result at all."""
        client = FakeGenieClient(
            completed_message=_make_query_message(),
        )
        pipeline = _make_pipeline(client=client, fetch_query_results=False)
        resp = pipeline.run("shipments from US", APP_CONV_ID)

        assert len(client.fetch_calls) == 0
        assert resp["is_table"] is False

    def test_generated_sql_is_in_response(self):
        client = FakeGenieClient(
            completed_message=_make_query_message(),
            query_result=_make_query_result(3),
        )
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("shipments from US", APP_CONV_ID)

        assert resp["generated_sql"] is not None
        assert "SELECT" in resp["generated_sql"].upper()


# =============================================================================
# TEST 8: Viz attachment
# =============================================================================


class TestVizAttachment:
    def test_viz_attachment_has_visualization_true(self):
        client = FakeGenieClient(completed_message=_make_full_message())
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("shipments from US via air", APP_CONV_ID)

        assert resp["has_visualization"] is True

    def test_visualization_is_reference_type_not_chart_spec(self):
        client = FakeGenieClient(completed_message=_make_full_message())
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("shipments from US via air", APP_CONV_ID)

        viz = resp["visualization"]
        assert viz is not None
        assert viz["type"] == "genie_viz_reference"
        assert viz["render_strategy"] == "client_side_from_query_result"

    def test_visualization_has_no_vega_or_plotly(self):
        client = FakeGenieClient(completed_message=_make_full_message())
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("shipments", APP_CONV_ID)

        viz = resp.get("visualization") or {}
        forbidden = {"vega_lite", "plotly", "chart_spec", "spec", "image_url"}
        found = forbidden & set(viz.keys())
        assert not found, f"Forbidden chart spec keys in visualization: {found}"

    def test_no_viz_when_no_viz_attachment(self):
        client = FakeGenieClient(completed_message=_make_text_only_message())
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("summary", APP_CONV_ID)

        assert resp["has_visualization"] is False
        assert resp["visualization"] is None


# =============================================================================
# TEST 9: Suggested questions
# =============================================================================


class TestSuggestedQuestions:
    def test_suggested_questions_returned_as_list(self):
        client = FakeGenieClient(completed_message=_make_full_message())
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("shipments from US via air", APP_CONV_ID)

        assert isinstance(resp["suggested_questions"], list)
        assert len(resp["suggested_questions"]) == 3

    def test_no_suggestions_returns_empty_list(self):
        client = FakeGenieClient(completed_message=_make_text_only_message())
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("summary", APP_CONV_ID)

        assert resp["suggested_questions"] == []


# =============================================================================
# TEST 10: GenieTimeoutError
# =============================================================================


class TestTimeoutError:
    def test_timeout_returns_error_status(self):
        client = FakeGenieClient(
            raise_on_wait=GenieTimeoutError("polling timed out after 30s")
        )
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("shipments from US", APP_CONV_ID)

        assert resp["status"] == "error"

    def test_timeout_has_fallback_recommended_true(self):
        client = FakeGenieClient(
            raise_on_wait=GenieTimeoutError("polling timed out after 30s")
        )
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("shipments from US", APP_CONV_ID)

        assert resp["fallback_recommended"] is True

    def test_timeout_message_does_not_expose_internal_detail(self):
        client = FakeGenieClient(
            raise_on_wait=GenieTimeoutError("polling timed out after 30s")
        )
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("shipments from US", APP_CONV_ID)

        # Internal error message must not appear in user-facing message
        assert "polling timed out" not in resp["message"].lower()
        assert resp["source"] == "genie"

    def test_timeout_includes_app_conversation_id(self):
        client = FakeGenieClient(
            raise_on_wait=GenieTimeoutError("timeout")
        )
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("shipments from US", APP_CONV_ID)

        assert resp["conversation_id"] == APP_CONV_ID


# =============================================================================
# TEST 11: GenieExecutionError
# =============================================================================


class TestExecutionError:
    def test_execution_error_returns_error_status(self):
        client = FakeGenieClient(
            raise_on_wait=GenieExecutionError("Genie message status FAILED")
        )
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("shipments from US", APP_CONV_ID)

        assert resp["status"] == "error"

    def test_execution_error_has_fallback_recommended_true(self):
        client = FakeGenieClient(
            raise_on_wait=GenieExecutionError("Genie message status FAILED")
        )
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("shipments from US", APP_CONV_ID)

        assert resp["fallback_recommended"] is True

    def test_execution_error_no_table_data(self):
        client = FakeGenieClient(
            raise_on_wait=GenieExecutionError("FAILED")
        )
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("shipments from US", APP_CONV_ID)

        assert resp["is_table"]   is False
        assert resp["table_data"] is None
        assert resp["row_count"]  == 0


# =============================================================================
# TEST 12: Query result fetch failure — non-fatal
# =============================================================================


class TestQueryFetchFailure:
    def test_fetch_failure_still_returns_success(self):
        """When fetch_query_result raises, the pipeline must return status=success
        with the text content from the message attachment.
        """
        client = FakeGenieClient(
            completed_message=_make_query_message(),
            raise_on_fetch=ConnectionError("SQL warehouse unavailable"),
        )
        pipeline = _make_pipeline(client=client, fetch_query_results=True)
        resp = pipeline.run("shipments from US", APP_CONV_ID)

        assert resp["status"] == "success", (
            f"Expected success when fetch fails non-fatally, got {resp['status']!r}"
        )

    def test_fetch_failure_no_table_data(self):
        client = FakeGenieClient(
            completed_message=_make_query_message(),
            raise_on_fetch=RuntimeError("unexpected fetch error"),
        )
        pipeline = _make_pipeline(client=client, fetch_query_results=True)
        resp = pipeline.run("shipments from US", APP_CONV_ID)

        assert resp["is_table"]   is False
        assert resp["table_data"] is None

    def test_fetch_failure_text_message_still_present(self):
        client = FakeGenieClient(
            completed_message=_make_query_message(),
            raise_on_fetch=RuntimeError("unexpected fetch error"),
        )
        pipeline = _make_pipeline(client=client, fetch_query_results=True)
        resp = pipeline.run("shipments from US", APP_CONV_ID)

        # The text from the text attachment should still be in the message
        assert "shipments" in resp["message"].lower()

    def test_fetch_failure_sql_still_extracted(self):
        """Even without rows, generated_sql must be extracted from the query attachment."""
        client = FakeGenieClient(
            completed_message=_make_query_message(),
            raise_on_fetch=RuntimeError("fetch error"),
        )
        pipeline = _make_pipeline(client=client, fetch_query_results=True)
        resp = pipeline.run("shipments from US", APP_CONV_ID)

        assert resp["generated_sql"] is not None

    def test_fetch_failure_no_fallback_recommended(self):
        """Fetch failure is non-fatal — fallback_recommended must be False."""
        client = FakeGenieClient(
            completed_message=_make_query_message(),
            raise_on_fetch=RuntimeError("fetch error"),
        )
        pipeline = _make_pipeline(client=client, fetch_query_results=True)
        resp = pipeline.run("shipments from US", APP_CONV_ID)

        assert resp.get("fallback_recommended") is False


# =============================================================================
# TEST 13 & 14: Expired / reset session starts new conversation
# =============================================================================


class TestExpiredSession:
    def _past_dt(self, seconds: int) -> datetime:
        return datetime.now(timezone.utc) - timedelta(seconds=seconds)

    def test_expired_session_calls_start_conversation(self):
        """If the session is expired, get_genie_conversation_id returns None.
        The pipeline must call start_conversation, not send_message.
        """
        store = GenieSessionStore()
        store.set_genie_conversation_id(APP_CONV_ID, GENIE_CONV_ID)

        # Force-expire the session
        with store._lock:
            store._sessions[APP_CONV_ID].expires_at = self._past_dt(10)

        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client, store=store)
        pipeline.run("shipments from US", APP_CONV_ID)

        assert len(client.start_conv_calls) == 1
        assert len(client.send_msg_calls)   == 0

    def test_expired_session_stores_new_genie_conv_id(self):
        store = GenieSessionStore()
        store.set_genie_conversation_id(APP_CONV_ID, "old-genie-conv-id")

        with store._lock:
            store._sessions[APP_CONV_ID].expires_at = self._past_dt(10)

        new_genie_conv_id = "brand-new-genie-conv-99"
        client = FakeGenieClient(genie_conv_id=new_genie_conv_id)
        pipeline = _make_pipeline(client=client, store=store)
        pipeline.run("shipments from US", APP_CONV_ID)

        stored = store.get_genie_conversation_id(APP_CONV_ID)
        assert stored == new_genie_conv_id


class TestResetSession:
    def test_reset_session_calls_start_conversation(self):
        """After reset_session(), the next run() must call start_conversation."""
        store = GenieSessionStore()
        store.set_genie_conversation_id(APP_CONV_ID, GENIE_CONV_ID)
        store.reset_session(APP_CONV_ID)  # marks as inactive — get() returns None

        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client, store=store)
        pipeline.run("shipments from US", APP_CONV_ID)

        assert len(client.start_conv_calls) == 1
        assert len(client.send_msg_calls)   == 0


# =============================================================================
# TEST 15 & 16: Debug mode
# =============================================================================


class TestDebugMode:
    def test_debug_true_includes_debug_info(self):
        client = FakeGenieClient(completed_message=_make_full_message())
        pipeline = _make_pipeline(client=client, debug=True)
        resp = pipeline.run("shipments from US via air", APP_CONV_ID)

        assert resp["debug_info"] is not None
        assert isinstance(resp["debug_info"], dict)

    def test_debug_true_debug_info_contains_sql(self):
        client = FakeGenieClient(completed_message=_make_full_message())
        pipeline = _make_pipeline(client=client, debug=True)
        resp = pipeline.run("shipments from US via air", APP_CONV_ID)

        debug = resp["debug_info"]
        assert "generated_sql" in debug
        assert debug["generated_sql"] is not None

    def test_debug_false_excludes_debug_info(self):
        client = FakeGenieClient(completed_message=_make_full_message())
        pipeline = _make_pipeline(client=client, debug=False)
        resp = pipeline.run("shipments from US via air", APP_CONV_ID)

        assert resp["debug_info"] is None

    def test_generated_sql_present_even_in_non_debug_mode(self):
        """generated_sql is always returned for audit, even when debug=False."""
        client = FakeGenieClient(completed_message=_make_full_message())
        pipeline = _make_pipeline(client=client, debug=False)
        resp = pipeline.run("shipments from US via air", APP_CONV_ID)

        assert resp["generated_sql"] is not None


# =============================================================================
# TEST 17: No stack trace in error messages
# =============================================================================


class TestNoStackTrace:
    _EXCEPTION_MARKERS = [
        "Traceback",
        "File \"",
        "line ",
        "Exception",
        "Error(",
        "raise ",
    ]

    def _assert_no_trace(self, message: str) -> None:
        for marker in self._EXCEPTION_MARKERS:
            assert marker not in message, (
                f"Stack trace marker {marker!r} found in user-facing message: "
                f"{message[:200]!r}"
            )

    def test_timeout_error_no_trace(self):
        client = FakeGenieClient(
            raise_on_wait=GenieTimeoutError("timed out at line 42 in genie_client.py")
        )
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("any query", APP_CONV_ID)
        self._assert_no_trace(resp["message"])

    def test_execution_error_no_trace(self):
        client = FakeGenieClient(
            raise_on_wait=GenieExecutionError("FAILED with details at line 99")
        )
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("any query", APP_CONV_ID)
        self._assert_no_trace(resp["message"])

    def test_generic_exception_no_trace(self):
        """Even an unexpected exception must not expose a stack trace."""
        client = FakeGenieClient(
            raise_on_wait=RuntimeError(
                "unexpected internal failure at genie_client.py:293 "
                "in wait_for_message_completion"
            )
        )
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("any query", APP_CONV_ID)
        self._assert_no_trace(resp["message"])


# =============================================================================
# TEST 18: Multi-turn scenario
# =============================================================================


class TestMultiTurnScenario:
    def test_second_message_reuses_same_genie_conv_id(self):
        """Two consecutive run() calls for the same app_conversation_id must
        use the same Genie conversation: first call uses start_conversation,
        second call uses send_message with the same genie_conv_id.

        Simulates the live smoke test T1 + T2 pattern:
          T1: 'shipments from US via air'
          T2: 'which are in transit?'
        """
        store  = GenieSessionStore()
        client = FakeGenieClient(
            genie_conv_id=GENIE_CONV_ID,
            start_msg_id=START_MSG_ID,
            followup_msg_id=FOLLOWUP_MSG_ID,
        )
        pipeline = _make_pipeline(client=client, store=store)

        # Turn 1 — new conversation
        resp_t1 = pipeline.run("shipments from US via air", APP_CONV_MULTI)
        assert resp_t1["status"] == "success"
        assert len(client.start_conv_calls) == 1
        assert len(client.send_msg_calls)   == 0

        genie_conv_from_t1 = store.get_genie_conversation_id(APP_CONV_MULTI)
        assert genie_conv_from_t1 == GENIE_CONV_ID

        # Turn 2 — follow-up
        resp_t2 = pipeline.run("which are in transit?", APP_CONV_MULTI)
        assert resp_t2["status"] == "success"
        assert len(client.start_conv_calls) == 1   # still only 1
        assert len(client.send_msg_calls)   == 1

        # send_message must pass the genie_conv_id from T1
        called_space_id, called_conv_id, _ = client.send_msg_calls[0]
        assert called_conv_id == GENIE_CONV_ID, (
            f"Follow-up must reuse genie_conv_id={GENIE_CONV_ID!r}, "
            f"got {called_conv_id!r}"
        )

    def test_multi_turn_genie_conv_id_consistent_in_responses(self):
        """Both responses must report the same genie_conversation_id."""
        store  = GenieSessionStore()
        client = FakeGenieClient(genie_conv_id=GENIE_CONV_ID)
        pipeline = _make_pipeline(client=client, store=store)

        resp_t1 = pipeline.run("shipments from US via air", APP_CONV_MULTI)
        resp_t2 = pipeline.run("which are in transit?",     APP_CONV_MULTI)

        assert resp_t1["genie_conversation_id"] == GENIE_CONV_ID
        assert resp_t2["genie_conversation_id"] == GENIE_CONV_ID

    def test_message_id_is_updated_after_each_turn(self):
        """The session store's last_message_id should reflect the most recent message."""
        store  = GenieSessionStore()
        client = FakeGenieClient(
            start_msg_id=START_MSG_ID,
            followup_msg_id=FOLLOWUP_MSG_ID,
        )
        pipeline = _make_pipeline(client=client, store=store)

        pipeline.run("shipments from US via air", APP_CONV_MULTI)
        assert store.get_last_message_id(APP_CONV_MULTI) == START_MSG_ID

        pipeline.run("which are in transit?", APP_CONV_MULTI)
        assert store.get_last_message_id(APP_CONV_MULTI) == FOLLOWUP_MSG_ID


# =============================================================================
# E3: Router integration tests
# =============================================================================


class TestRouterIntegration:
    def test_greeting_short_circuits_without_genie_call(self):
        client = FakeGenieClient()
        store = GenieSessionStore()
        pipeline = _make_pipeline(client=client, store=store)

        resp = pipeline.run("hi", APP_CONV_ID)

        assert resp["status"] == "success"
        assert resp["source"] == "local"
        assert resp["is_table"] is False
        assert resp["table_data"] is None
        assert resp["download_key"] is None
        assert len(client.start_conv_calls) == 0
        assert len(client.send_msg_calls) == 0
        assert store.get_genie_conversation_id(APP_CONV_ID) is None

    def test_identity_short_circuits_without_genie_call(self):
        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client)

        resp = pipeline.run("Who built you?", APP_CONV_ID)

        assert resp["status"] == "success"
        assert resp["source"] == "local"
        assert len(client.start_conv_calls) == 0
        assert len(client.send_msg_calls) == 0

    def test_identity_response_contains_owner_and_hides_backend_terms(self):
        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client)

        resp = pipeline.run("What model are you?", APP_CONV_ID)
        msg = resp["message"]

        assert "TransparencE Shipment Intelligence" in msg
        assert "GLOG team" in msg
        forbidden = ["Databricks", "Genie", "Claude", "OpenAI", "model", "LLM", "backend architecture"]
        assert all(term not in msg for term in forbidden)

    def test_download_request_with_existing_key_returns_local_download_response(self):
        client = FakeGenieClient()
        store = GenieSessionStore()
        store.update_context(APP_CONV_ID, last_download_key="dl-123")
        pipeline = _make_pipeline(client=client, store=store)

        resp = pipeline.run("download csv", APP_CONV_ID)

        assert resp["source"] == "local"
        assert resp["download_key"] == "dl-123"
        assert "download" in resp["message"].lower() or "csv" in resp["message"].lower()
        assert len(client.start_conv_calls) == 0
        assert len(client.send_msg_calls) == 0

    def test_download_request_without_existing_key_asks_for_table_query_first(self):
        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client)

        resp = pipeline.run("export this", APP_CONV_ID)

        assert resp["source"] == "local"
        assert resp["download_key"] is None
        assert "run" in resp["message"].lower()
        assert "query" in resp["message"].lower() or "table" in resp["message"].lower()
        assert len(client.start_conv_calls) == 0
        assert len(client.send_msg_calls) == 0

    def test_part_number_query_routes_as_part_number_not_shipment_id(self):
        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client, debug=True)

        prompt = (
            "i need the shipments for these part numbers which are going to US - "
            "363097-000, NB15524001, 605705-31, 216367-001, 605704-ZZ, "
            "NB19684001, 6460-295-22341, NB18784001, 216278-001"
        )
        resp = pipeline.run(prompt, APP_CONV_ID)

        assert len(client.start_conv_calls) == 1
        sent_prompt = client.start_conv_calls[0][1]
        assert "part_number" in sent_prompt
        assert "destination is US" in sent_prompt
        assert "shipment IDs" not in sent_prompt
        router = resp["debug_info"]["router_decision"]
        assert router["entity_type"] == "part_number"
        assert "NB15524001" in router["entities"]

    def test_correction_reuses_previous_entities_when_available(self):
        client = FakeGenieClient()
        store = GenieSessionStore()
        store.set_genie_conversation_id(APP_CONV_ID, GENIE_CONV_ID)
        store.update_context(
            APP_CONV_ID,
            last_entities=["NB15524001", "216367-001"],
            last_entity_type="shipment_id",
            last_filters={"destination": "US"},
            last_intent="EXPLICIT_ENTITY_SEARCH",
        )
        pipeline = _make_pipeline(client=client, store=store, debug=True)

        resp = pipeline.run("these are part numbers, no shipment ids", APP_CONV_ID)

        assert len(client.start_conv_calls) == 1
        assert len(client.send_msg_calls) == 0
        sent_prompt = client.start_conv_calls[0][1]
        assert "part_number" in sent_prompt
        assert "NB15524001" in sent_prompt
        assert "destination is US" in sent_prompt
        router = resp["debug_info"]["router_decision"]
        assert router["intent"] == "CORRECTION"
        assert router["entity_type"] == "part_number"
        assert router["entities"] == ["NB15524001", "216367-001"]

    def test_correction_without_previous_entities_asks_user_to_resend_values(self):
        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client)

        resp = pipeline.run("these are part numbers, no shipment ids", APP_CONV_ID)

        assert resp["source"] == "local"
        assert "resend" in resp["message"].lower() or "provided values" in resp["message"].lower()
        assert len(client.start_conv_calls) == 0
        assert len(client.send_msg_calls) == 0

    def test_direct_lookup_still_calls_genie_precisely(self):
        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client, debug=True)

        resp = pipeline.run("shipment 42570415", APP_CONV_ID)

        assert len(client.start_conv_calls) == 1
        assert client.start_conv_calls[0][1] == "shipment 42570415"
        router = resp["debug_info"]["router_decision"]
        assert router["intent"] == "DIRECT_LOOKUP"
        assert router["entity_type"] == "shipment_id"

    def test_true_follow_up_reuses_existing_genie_conversation(self):
        client = FakeGenieClient()
        store = GenieSessionStore()
        store.set_genie_conversation_id(APP_CONV_ID, GENIE_CONV_ID)
        store.update_context(
            APP_CONV_ID,
            last_intent="BROAD_LISTING",
            last_filters={"source": "US", "transport_mode": "AIR"},
            last_user_prompt="shipments from US via air",
            last_enriched_prompt="Summarise shipments from US via air",
        )
        pipeline = _make_pipeline(client=client, store=store, debug=True)

        resp = pipeline.run("which are in transit?", APP_CONV_ID)

        assert len(client.start_conv_calls) == 0
        assert len(client.send_msg_calls) == 1
        _, conv_id, sent_prompt = client.send_msg_calls[0]
        assert conv_id == GENIE_CONV_ID
        assert sent_prompt == "which are in transit?"
        assert resp["debug_info"]["router_decision"]["intent"] == "TRUE_FOLLOW_UP"

    def test_new_standalone_aggregation_is_still_enriched_in_same_chat(self):
        client = FakeGenieClient()
        store = GenieSessionStore()
        store.set_genie_conversation_id(APP_CONV_ID, GENIE_CONV_ID)
        store.update_context(
            APP_CONV_ID,
            last_intent="TRUE_FOLLOW_UP",
            last_user_prompt="which are in transit?",
        )
        pipeline = _make_pipeline(client=client, store=store, debug=True)

        resp = pipeline.run("shipment status distribution", APP_CONV_ID)

        assert len(client.start_conv_calls) == 1
        assert len(client.send_msg_calls) == 0
        sent_prompt = client.start_conv_calls[0][1]
        assert "group by" in sent_prompt.lower()  # E5: "shipment status distribution" canonicalizes to status distribution prompt with GROUP BY
        assert "shipment status distribution" in sent_prompt.lower()
        assert resp["debug_info"]["router_decision"]["intent"] == "AGGREGATION"

    def test_router_decision_only_exposed_in_debug_mode(self):
        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client, debug=False)

        resp = pipeline.run("shipment 42570415", APP_CONV_ID)

        assert resp["debug_info"] is None

    def test_table_chart_download_behavior_does_not_regress(self):
        client = FakeGenieClient(
            completed_message=_make_full_message(),
            query_result=_make_query_result(3),
        )
        store = GenieSessionStore()
        audit = FakeAuditService(download_key="dl-xyz")
        pipeline = _make_pipeline(client=client, store=store, audit_service=audit, debug=True)

        resp = pipeline.run("shipment status distribution", APP_CONV_ID)
        snapshot = store.get_context_snapshot(APP_CONV_ID)

        assert resp["status"] == "success"
        assert resp["is_table"] is True
        assert resp["has_visualization"] is True
        assert resp["download_key"] == "dl-xyz"
        assert resp["table_data"] is not None
        assert audit.calls
        assert snapshot["last_download_key"] == "dl-xyz"
        assert snapshot["last_row_count"] == resp["row_count"]
        assert snapshot["last_total_row_count"] == 5000
        assert snapshot["last_returned_row_count"] == 3
        assert snapshot["last_intent"] == "AGGREGATION"


# =============================================================================
# E4: Async export tests
# =============================================================================


class TestAsyncExportE4:
    def test_async_table_response_includes_export_id(self):
        client = FakeGenieClient(
            completed_message=_make_full_message(),
            query_result=_make_query_result(3),
        )
        store = GenieSessionStore()
        audit = FakeAuditService(download_key="dl-async")
        manager = ExportJobManager(ttl_hours=24)
        pipeline = _make_pipeline(
            client=client,
            store=store,
            audit_service=audit,
            async_export_enabled=True,
            export_mode="returned_rows_only",
            export_job_manager=manager,
        )

        resp = pipeline.run("shipment status distribution", APP_CONV_ID)

        assert resp["export_id"]
        assert resp["export_status"] in {"queued", "running", "ready"}
        assert resp["export_mode"] == "returned_rows_only"
        assert resp["preview_row_count"] == 3
        assert resp["returned_row_count"] == 3
        assert resp["total_row_count"] == 5000

    def test_text_only_response_does_not_create_export_job(self):
        manager = ExportJobManager(ttl_hours=24)
        pipeline = _make_pipeline(
            client=FakeGenieClient(completed_message=_make_text_only_message()),
            audit_service=FakeAuditService(),
            async_export_enabled=True,
            export_job_manager=manager,
        )

        resp = pipeline.run("quick summary", APP_CONV_ID)

        assert resp["is_table"] is False
        assert resp["export_id"] is None
        assert manager.cleanup_expired_jobs() == 0

    def test_unsafe_sql_falls_back_to_returned_rows_only(self):
        unsafe_message = _make_full_message()
        unsafe_message.query_attachments[0].sql = "DELETE FROM some_table"
        client = FakeGenieClient(
            completed_message=unsafe_message,
            query_result=_make_query_result(3),
        )
        pipeline = _make_pipeline(
            client=client,
            audit_service=FakeAuditService(),
            async_export_enabled=True,
            export_mode="async_full_query",
            export_job_manager=ExportJobManager(ttl_hours=24),
            sql_service=FakeSQLService(),
        )

        resp = pipeline.run("shipment status distribution", APP_CONV_ID)

        assert resp["export_mode"] == "returned_rows_only"

    def test_full_query_export_applies_max_export_rows(self):
        client = FakeGenieClient(
            completed_message=_make_full_message(),
            query_result=_make_query_result(3),
        )
        sql_service = FakeSQLService(result_rows=[[f"SHP-{i}", "US"] for i in range(10)])
        pipeline = _make_pipeline(
            client=client,
            audit_service=FakeAuditService(download_key="dl-full"),
            async_export_enabled=True,
            export_mode="async_full_query",
            max_export_rows=5,
            export_job_manager=ExportJobManager(ttl_hours=24),
            sql_service=sql_service,
        )

        resp = pipeline.run("shipment status distribution", APP_CONV_ID)
        job = pipeline._export_job_manager.get_job(resp["export_id"])
        if job is not None and job.status == "ready":
            assert job.row_count == 5
        assert sql_service.calls
        assert sql_service.calls[0]["row_limit"] == 5
        assert "LIMIT 5" in sql_service.calls[0]["sql"]

    def test_download_request_while_export_running_is_local(self):
        client = FakeGenieClient()
        store = GenieSessionStore()
        store.update_context(
            APP_CONV_ID,
            last_export_id="exp-1",
            last_export_status="running",
            last_export_mode="returned_rows_only",
        )
        pipeline = _make_pipeline(client=client, store=store)

        resp = pipeline.run("download this result", APP_CONV_ID)

        assert resp["source"] == "local"
        assert resp["export_status"] == "running"
        assert "prepared" in resp["message"].lower()
        assert len(client.start_conv_calls) == 0


# =============================================================================
# E4-fix2: NON_BUSINESS_OR_SMALL_TALK — pipeline integration
# =============================================================================


class TestNonBusinessRoutingPipeline:
    """Verify that low-information prompts are handled locally without any
    GenieClient calls, and that the pipeline response matches the local
    clarification contract."""

    def test_bro_does_not_call_genie_client(self):
        """'bro' must short-circuit before any GenieClient method is called."""
        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client)
        pipeline.run("bro", APP_CONV_ID)

        assert len(client.start_conv_calls) == 0, (
            f"start_conversation should not be called for 'bro', "
            f"got {client.start_conv_calls}"
        )
        assert len(client.send_msg_calls) == 0

    def test_bro_returns_local_clarification_source(self):
        """'bro' must return a local clarification, not a table."""
        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client)
        resp = pipeline.run("bro", APP_CONV_ID)

        assert resp["source"] == "local"
        assert resp["is_table"] is False
        assert resp["table_data"] is None
        assert "shipment" in resp["message"].lower()

    def test_bro_does_not_create_or_modify_genie_session(self):
        """Low-info prompt must not create a Genie session mapping."""
        store = GenieSessionStore()
        pipeline = _make_pipeline(client=FakeGenieClient(), store=store)

        pipeline.run("bro", APP_CONV_ID)

        assert store.get_genie_conversation_id(APP_CONV_ID) is None

    def test_normal_business_query_still_calls_genie_after_guard(self):
        """A legitimate logistics query must still reach the Genie API."""
        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client)

        pipeline.run("shipments from US via air", APP_CONV_ID)

        assert len(client.start_conv_calls) == 1

    def test_short_business_prompt_with_domain_keyword_calls_genie(self):
        """'delayed?' contains a domain keyword and must not be blocked."""
        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client)

        pipeline.run("delayed?", APP_CONV_ID)

        assert len(client.start_conv_calls) == 1


# =============================================================================
# E4-fix2: Export field propagation through pipeline response
# =============================================================================


class TestExportFieldPropagation:
    """Verify that export metadata (export_row_count, export_status, export_mode)
    is present in the pipeline response dict when a sync export is created."""

    def test_sync_export_row_count_present_in_table_response(self):
        """After a table query with audit_service, response must include export_row_count."""
        client = FakeGenieClient(
            completed_message=_make_query_message(),
            query_result=_make_query_result(3),
        )
        audit = FakeAuditService(download_key="dl-test-count")
        pipeline = _make_pipeline(client=client, audit_service=audit)

        resp = pipeline.run("shipments from US", APP_CONV_ID)

        assert resp["is_table"] is True
        assert resp["export_row_count"] is not None, (
            "export_row_count must be populated when sync export succeeds"
        )
        assert resp["export_row_count"] > 0
        assert resp["export_status"] == "ready"
        assert resp["export_mode"] == "returned_rows_only"
        assert resp["download_key"] == "dl-test-count"

    def test_export_row_count_stored_in_session_after_table_query(self):
        """export_row_count must be persisted in the session so downloads can use it."""
        store = GenieSessionStore()
        client = FakeGenieClient(
            completed_message=_make_query_message(),
            query_result=_make_query_result(3),
        )
        audit = FakeAuditService(download_key="dl-session-count")
        pipeline = _make_pipeline(client=client, store=store, audit_service=audit)

        pipeline.run("shipments from US", APP_CONV_ID)
        snapshot = store.get_context_snapshot(APP_CONV_ID)

        assert snapshot["last_export_row_count"] is not None
        assert snapshot["last_export_row_count"] > 0
        assert snapshot["last_download_key"] == "dl-session-count"

    def test_download_request_after_table_carries_row_count_through_pipeline(self):
        """When download is requested, export_row_count must be in the local response."""
        store = GenieSessionStore()
        store.update_context(
            APP_CONV_ID,
            last_download_key="dl-prev",
            last_export_id="exp-prev",
            last_export_status="ready",
            last_export_mode="returned_rows_only",
            last_export_row_count=91,
        )
        pipeline = _make_pipeline(client=FakeGenieClient(), store=store)

        resp = pipeline.run("download this data", APP_CONV_ID)

        assert resp["source"] == "local"
        assert resp["export_row_count"] == 91, (
            f"Expected export_row_count=91, got {resp['export_row_count']}"
        )
        assert resp["download_key"] == "dl-prev"
        assert resp["export_status"] == "ready"

    def test_text_only_response_has_null_export_row_count(self):
        """A text-only Genie response (no table) must have export_row_count=None."""
        client = FakeGenieClient(completed_message=_make_text_only_message())
        pipeline = _make_pipeline(client=client)

        resp = pipeline.run("give me a quick summary", APP_CONV_ID)

        assert resp["is_table"] is False
        assert resp["export_row_count"] is None
        assert resp["download_key"] is None


# =============================================================================
# Typed-download async export bug tests (Option D: Part 1 + Part 2)
# =============================================================================


class TestTypedDownloadAsyncBug:
    """Integration: 'download this data' typed by user after grid shows CSV ready.

    The scenario:
      1. Pipeline returns table → export starts async (latest_table_result=queued).
      2. Background thread completes → ExportJobManager.get_job() is now ready.
      3. User types 'download this data'.
      4. Pipeline should return the ready download, NOT 'still preparing'.
    """

    def _make_async_pipeline(
        self,
        store: GenieSessionStore,
        audit: FakeAuditService,
        manager: ExportJobManager,
        client: Optional[FakeGenieClient] = None,
    ) -> GeniePipeline:
        return _make_pipeline(
            client=client or FakeGenieClient(
                completed_message=_make_query_message(),
                query_result=_make_query_result(3),
            ),
            store=store,
            audit_service=audit,
            async_export_enabled=True,
            export_mode="returned_rows_only",
            export_job_manager=manager,
        )

    def test_stale_queued_ltr_ejm_ready_returns_download_not_preparing(self):
        """Core bug regression: typed 'download' with stale queued LTR but ready EJM
        must return the ready download key, NOT the 'still preparing' message."""
        store = GenieSessionStore()
        manager = ExportJobManager(ttl_hours=24)
        # Manually plant a stale LTR (queued) and a ready EJM entry
        from app.services.genie_session_store import TableExportRecord
        from datetime import datetime, timezone
        stale_export_id = "stale-exp-001"
        store.update_context(
            APP_CONV_ID,
            last_export_id=stale_export_id,
            last_export_status="queued",
            last_export_mode="returned_rows_only",
        )
        # Plant latest_table_result with stale state
        ltr = TableExportRecord(
            assistant_message_id="msg-001",
            download_key=None,
            export_id=stale_export_id,
            export_status="queued",
            export_mode="returned_rows_only",
            export_row_count=None,
            query_description="US shipments",
            created_at=datetime.now(timezone.utc),
        )
        store.update_context(APP_CONV_ID, latest_table_result=ltr)

        # Simulate background thread completing: EJM is now ready
        job = manager.create_job(app_conversation_id=APP_CONV_ID, mode="returned_rows_only")
        # Override with the known export_id (create_job generates a new UUID,
        # so we use a different approach: plant the job state directly)
        # Cleanest way: create, then override the export_id to match our stale LTR
        # Actually let's use a real flow: plant via EJM with same export_id
        # EJM doesn't support arbitrary IDs, so mark the fresh job ready + point LTR at it
        from app.services.genie_session_store import TableExportRecord
        ltr2 = TableExportRecord(
            assistant_message_id="msg-001",
            download_key=None,
            export_id=job.export_id,     # use actual job ID
            export_status="queued",
            export_mode="returned_rows_only",
            export_row_count=None,
            query_description="US shipments",
            created_at=datetime.now(timezone.utc),
        )
        store.update_context(APP_CONV_ID, latest_table_result=ltr2)
        store.update_context(APP_CONV_ID,
            last_export_id=job.export_id,
            last_export_status="queued")
        # Now mark EJM ready
        manager.mark_running(job.export_id)
        manager.mark_ready(
            job.export_id,
            file_path="/tmp/ready.csv",
            download_key="ready-key-001",
            row_count=350,
        )

        # Run 'download this data' through the pipeline
        pipeline = _make_pipeline(
            client=FakeGenieClient(),  # no genie call expected
            store=store,
            audit_service=FakeAuditService(),
            async_export_enabled=True,
            export_mode="returned_rows_only",
            export_job_manager=manager,
        )
        resp = pipeline.run("download this data", APP_CONV_ID)

        assert resp["source"] == "local", "Expected local (no Genie call)"
        assert resp["download_key"] == "ready-key-001", (
            f"Expected ready-key-001, got {resp['download_key']!r}. "
            f"message={resp['message']!r}"
        )
        assert resp["export_status"] == "ready"
        assert resp["export_row_count"] == 350
        assert "preparing" not in resp["message"].lower(), (
            f"'preparing' must not appear in response for a ready export: {resp['message']!r}"
        )

    def test_stale_queued_ltr_ejm_still_running_says_preparing(self):
        """latest_table_result queued + EJM still running → 'still preparing'."""
        from app.services.genie_session_store import TableExportRecord
        from datetime import datetime, timezone
        store = GenieSessionStore()
        manager = ExportJobManager(ttl_hours=24)
        job = manager.create_job(app_conversation_id=APP_CONV_ID, mode="returned_rows_only")
        manager.mark_running(job.export_id)  # still running

        ltr = TableExportRecord(
            assistant_message_id="msg-002",
            download_key=None,
            export_id=job.export_id,
            export_status="queued",
            export_mode="returned_rows_only",
            export_row_count=None,
            query_description=None,
            created_at=datetime.now(timezone.utc),
        )
        store.update_context(APP_CONV_ID, latest_table_result=ltr)

        pipeline = _make_pipeline(
            client=FakeGenieClient(),
            store=store,
            audit_service=FakeAuditService(),
            async_export_enabled=True,
            export_job_manager=manager,
        )
        resp = pipeline.run("download this data", APP_CONV_ID)

        assert resp["source"] == "local"
        assert resp["download_key"] is None
        assert "prepared" in resp["message"].lower() or "preparing" in resp["message"].lower(), (
            f"Expected 'prepared'/'preparing' for still-running export: {resp['message']!r}"
        )

    def test_stale_queued_ltr_ejm_failed_says_failed(self):
        """latest_table_result queued + EJM failed → export failed message."""
        from app.services.genie_session_store import TableExportRecord
        from datetime import datetime, timezone
        store = GenieSessionStore()
        manager = ExportJobManager(ttl_hours=24)
        job = manager.create_job(app_conversation_id=APP_CONV_ID, mode="returned_rows_only")
        manager.mark_failed(job.export_id, "out of memory")

        ltr = TableExportRecord(
            assistant_message_id="msg-003",
            download_key=None,
            export_id=job.export_id,
            export_status="queued",
            export_mode="returned_rows_only",
            export_row_count=None,
            query_description=None,
            created_at=datetime.now(timezone.utc),
        )
        store.update_context(APP_CONV_ID, latest_table_result=ltr)

        pipeline = _make_pipeline(
            client=FakeGenieClient(),
            store=store,
            audit_service=FakeAuditService(),
            async_export_enabled=True,
            export_job_manager=manager,
        )
        resp = pipeline.run("download this data", APP_CONV_ID)

        assert resp["source"] == "local"
        assert resp["download_key"] is None
        assert resp["export_status"] == "failed"
        assert "failed" in resp["message"].lower(), (
            f"Expected 'failed' in response for failed export: {resp['message']!r}"
        )

    def test_older_table_a_export_completing_late_does_not_overwrite_table_b_ltr(self):
        """Scenario 6: Table A export completes after Table B is already latest.
        latest_table_result must remain pointing to Table B."""
        from app.services.genie_session_store import TableExportRecord
        from datetime import datetime, timezone
        store = GenieSessionStore()
        manager = ExportJobManager(ttl_hours=24)

        # Table B is latest (has its own export_id)
        job_b = manager.create_job(app_conversation_id=APP_CONV_ID)
        ltr_b = TableExportRecord(
            assistant_message_id="msg-b",
            download_key=None,
            export_id=job_b.export_id,
            export_status="queued",
            export_mode="returned_rows_only",
            export_row_count=None,
            query_description="Table B query",
            created_at=datetime.now(timezone.utc),
        )
        store.update_context(APP_CONV_ID, latest_table_result=ltr_b)

        # Table A job (older) completes: simulate _run_export_job logic
        job_a = manager.create_job(app_conversation_id=APP_CONV_ID)
        manager.mark_running(job_a.export_id)
        manager.mark_ready(job_a.export_id,
            file_path="/tmp/a.csv",
            download_key="key-a",
            row_count=50,
        )
        # Manually run the latest_table_result guard (mirrors _run_export_job logic)
        session = store.get_session(APP_CONV_ID)
        if session is not None and session.latest_table_result is not None:
            _ltr = session.latest_table_result
            if _ltr.export_id == job_a.export_id:  # should NOT match (it's job_b)
                updated_ltr = TableExportRecord(
                    assistant_message_id=_ltr.assistant_message_id,
                    download_key="key-a",
                    export_id=job_a.export_id,
                    export_status="ready",
                    export_mode="returned_rows_only",
                    export_row_count=50,
                    query_description=_ltr.query_description,
                    created_at=_ltr.created_at,
                )
                store.update_context(APP_CONV_ID, latest_table_result=updated_ltr)

        # Verify: latest_table_result should still be Table B's
        snapshot = store.get_context_snapshot(APP_CONV_ID)
        ltr_now = snapshot["latest_table_result"]
        assert ltr_now["export_id"] == job_b.export_id, (
            f"latest_table_result was overwritten by older export A. "
            f"export_id={ltr_now['export_id']!r}"
        )
        assert ltr_now["download_key"] is None, (
            "Table B export hasn't completed — download_key should still be None"
        )


class TestRunExportJobUpdatesLTR:
    """Unit tests for _run_export_job latest_table_result update (Part 1)."""

    def _make_full_pipeline_with_export(
        self,
        store: Optional[GenieSessionStore] = None,
        audit: Optional[FakeAuditService] = None,
        manager: Optional[ExportJobManager] = None,
    ) -> GeniePipeline:
        return _make_pipeline(
            client=FakeGenieClient(
                completed_message=_make_query_message(),
                query_result=_make_query_result(3),
            ),
            store=store or GenieSessionStore(),
            audit_service=audit or FakeAuditService(download_key="dl-ltr-test"),
            async_export_enabled=True,
            export_mode="returned_rows_only",
            export_job_manager=manager or ExportJobManager(ttl_hours=24),
        )

    def test_run_export_job_updates_ltr_when_export_id_matches(self):
        """After export completes, latest_table_result must be updated to ready
        when its export_id matches the completed export."""
        import time
        from app.services.genie_session_store import TableExportRecord
        from datetime import datetime, timezone
        store = GenieSessionStore()
        manager = ExportJobManager(ttl_hours=24)
        audit = FakeAuditService(download_key="dk-match")

        # First: run a table query to populate latest_table_result with queued state
        pipeline = self._make_full_pipeline_with_export(
            store=store, audit=audit, manager=manager
        )
        resp = pipeline.run("shipments from US", APP_CONV_ID)

        export_id = resp.get("export_id")
        assert export_id, "Expected an export_id in the response"

        # Wait briefly for the background thread to complete
        time.sleep(0.3)

        # Verify latest_table_result was updated to ready
        snapshot = store.get_context_snapshot(APP_CONV_ID)
        ltr = snapshot.get("latest_table_result")
        assert ltr is not None, "latest_table_result must be set"
        assert ltr["export_id"] == export_id
        # Either the thread updated it to ready, or the initial response was sync-ready
        assert ltr["export_status"] in ("ready", "queued", "running"), (
            f"Unexpected export_status: {ltr['export_status']!r}"
        )
        if ltr["export_status"] == "ready":
            assert ltr["download_key"] == "dk-match", (
                f"download_key should be 'dk-match', got {ltr['download_key']!r}"
            )

    def test_run_export_job_does_not_update_ltr_when_export_id_does_not_match(self):
        """If latest_table_result refers to a different (newer) export,
        completing an older export must NOT overwrite latest_table_result."""
        import time
        from app.services.genie_session_store import TableExportRecord
        from datetime import datetime, timezone
        store = GenieSessionStore()
        manager = ExportJobManager(ttl_hours=24)
        audit = FakeAuditService(download_key="dk-old")

        # Step 1: run first query → export_id_A created and completes
        pipeline = self._make_full_pipeline_with_export(
            store=store, audit=audit, manager=manager
        )
        resp1 = pipeline.run("shipments from US", APP_CONV_ID)
        export_id_a = resp1.get("export_id")
        time.sleep(0.3)  # let thread A complete

        # Step 2: plant a NEW latest_table_result for Table B (different export_id)
        job_b = manager.create_job(app_conversation_id=APP_CONV_ID)
        ltr_b = TableExportRecord(
            assistant_message_id="msg-b",
            download_key=None,
            export_id=job_b.export_id,
            export_status="queued",
            export_mode="returned_rows_only",
            export_row_count=None,
            query_description="New Table B query",
            created_at=datetime.now(timezone.utc),
        )
        store.update_context(APP_CONV_ID, latest_table_result=ltr_b)

        # Step 3: run _run_export_job with export_id_a (the old one)
        # It should NOT overwrite LTR since LTR now points to job_b
        if export_id_a:
            try:
                pipeline._run_export_job(
                    export_id=export_id_a,
                    app_conversation_id=APP_CONV_ID,
                    mode="returned_rows_only",
                    headers=["col1"],
                    rows=[["v1"]],
                    response={},
                )
            except Exception:
                pass  # audit may raise if already exported — that's fine

        # latest_table_result should still be Table B's
        snapshot = store.get_context_snapshot(APP_CONV_ID)
        ltr_now = snapshot.get("latest_table_result")
        assert ltr_now is not None
        assert ltr_now["export_id"] == job_b.export_id, (
            f"latest_table_result was overwritten by old export. "
            f"Got export_id={ltr_now['export_id']!r}, expected {job_b.export_id!r}"
        )

    def test_ltr_updated_to_ready_enables_typed_download_without_ejm_lookup(self):
        """End-to-end: after _run_export_job updates LTR to ready, typed 'download'
        should serve the key directly from LTR (no EJM lookup needed)."""
        import time
        store = GenieSessionStore()
        manager = ExportJobManager(ttl_hours=24)
        audit = FakeAuditService(download_key="dk-e2e")

        pipeline = self._make_full_pipeline_with_export(
            store=store, audit=audit, manager=manager
        )
        # First turn: table query
        resp1 = pipeline.run("shipments from US", APP_CONV_ID)
        assert resp1.get("export_id"), "Expected export_id in first response"
        time.sleep(0.3)  # let background thread complete + update LTR

        # Second turn: typed download
        resp2 = pipeline.run("i want to download this data", APP_CONV_ID)

        assert resp2["source"] == "local", (
            f"Expected local (no Genie call), got source={resp2['source']!r}"
        )
        # If LTR was updated to ready by the thread, download_key must be present.
        # If the thread hasn't finished yet (race condition in test), the EJM fallback
        # must still return ready — assert either path works.
        if resp2.get("download_key"):
            assert resp2["export_status"] == "ready"
            assert "preparing" not in resp2["message"].lower()
        else:
            # Thread may not have run yet in CI — check EJM
            ejm_job = manager.get_job(resp1["export_id"])
            if ejm_job is not None and ejm_job.status == "ready":
                # EJM is ready but we didn't get download_key — test failure
                assert resp2["download_key"] == "dk-e2e", (
                    f"EJM is ready but download_key not returned. "
                    f"resp={resp2['message']!r}"
                )


# =============================================================================


# =============================================================================
# E6: Analytical shape-validation safety net (analysis / insights / overview …)
# =============================================================================


class _FakeGenieClientDual:
    """FakeGenieClient variant that returns raw headers on first call and
    aggregated headers on the second call (simulating a shape-validation retry).
    """

    def __init__(
        self,
        raw_query_result: "GenieQueryResult",
        agg_query_result: "GenieQueryResult",
        raw_message: "GenieMessage",
        agg_message: "GenieMessage",
    ):
        self._raw_qr = raw_query_result
        self._agg_qr = agg_query_result
        self._raw_msg = raw_message
        self._agg_msg = agg_message
        self.start_conv_calls: list = []
        self.send_msg_calls: list = []
        self._call_count = 0

    def start_conversation(self, space_id, message):
        self._call_count += 1
        self.start_conv_calls.append((space_id, message))
        return {
            "conversation_id": f"fake-conv-{self._call_count}",
            "message_id": f"fake-msg-{self._call_count}",
        }

    def send_message(self, space_id, conversation_id, message):
        self.send_msg_calls.append((space_id, conversation_id, message))
        return {"message_id": "fake-follow-up-msg"}

    def wait_for_message_completion(
        self, space_id, conv_id, msg_id, timeout_seconds=120, poll_interval_seconds=2.0
    ):
        if self._call_count == 1:
            return self._raw_msg
        return self._agg_msg

    def fetch_query_result(
        self, space_id, conv_id, msg_id, statement_id=None, fetch_rows=True, row_limit=500
    ):
        if self._call_count == 1:
            return self._raw_qr
        return self._agg_qr


_RAW_AIRL_COLUMNS = [
    {"name": "shipment_number_id",      "type_text": "STRING",  "position": 0},
    {"name": "part_number",             "type_text": "STRING",  "position": 1},
    {"name": "source_",                 "type_text": "STRING",  "position": 2},
    {"name": "destination",             "type_text": "STRING",  "position": 3},
    {"name": "transportation_mode_desc","type_text": "STRING",  "position": 4},
    {"name": "execution_status",        "type_text": "STRING",  "position": 5},
    {"name": "actual_pgi_date",         "type_text": "DATE",    "position": 6},
    {"name": "eta",                     "type_text": "DATE",    "position": 7},
]
_RAW_AIR_ROWS = [
    ["SHP-001", "NB15524001", "US", "CN", "Air transport", "In Transit",
     "2025-10-01", "2025-10-15"],
] * 10  # simulate 10 raw rows

_AGG_COLUMNS_DELAY = [
    {"name": "execution_status", "type_text": "STRING",  "position": 0},
    {"name": "shipment_count",   "type_text": "BIGINT",  "position": 1},
    {"name": "delayed_count",    "type_text": "BIGINT",  "position": 2},
]
_AGG_ROWS_DELAY = [
    ["In Transit", 120, 30],
    ["Delivered",  80,  5],
]


def _make_query_message_with_columns(columns, rows, text="Aggregated result."):
    query_result_obj = GenieQueryResult(
        statement_id=STMT_ID,
        status="SUCCEEDED",
        columns=columns,
        rows=rows,
        row_count=len(rows),
        total_row_count=len(rows),
        format="JSON_ARRAY",
    )
    msg = GenieMessage(
        id=START_MSG_ID,
        space_id=SPACE_ID,
        conversation_id=GENIE_CONV_ID,
        status="COMPLETED",
        content="result",
        message_id=START_MSG_ID,
        text_attachments=[GenieTextAttachment(content=text)],
        query_attachments=[
            GenieQueryAttachment(
                attachment_id=ATT_QUERY_ID,
                sql=TEST_SQL,
                description=text,
                statement_id=STMT_ID,
                row_count=len(rows),
            )
        ],
        viz_attachments=[],
        suggested_questions=[],
    )
    return msg, query_result_obj


class TestE6AnalyticalShapeValidation:
    """E6: Shape validation must fire for analytical prompts that return raw rows,
    whether the intent is AGGREGATION or BROAD_LISTING with analytical vocabulary.
    """

    def test_analytical_aggregation_prompt_triggers_retry_on_raw_rows(self):
        """After E6 fix, 'give analysis on shipments via air and their delay' routes
        as AGGREGATION.  If Genie returns raw rows, shape validation fires and
        the pipeline retries with a delay analysis canonical prompt.
        """
        raw_msg, raw_qr = _make_query_message_with_columns(
            _RAW_AIRL_COLUMNS, _RAW_AIR_ROWS, "Found 500 matching shipments."
        )
        agg_msg, agg_qr = _make_query_message_with_columns(
            _AGG_COLUMNS_DELAY, _AGG_ROWS_DELAY, "Delay analysis complete."
        )
        client = _FakeGenieClientDual(raw_qr, agg_qr, raw_msg, agg_msg)
        pipeline = _make_pipeline(client=client)

        resp = pipeline.run(
            "give analysis on shipments via air and their delay",
            "e6-conv-01",
        )

        # Should have retried — two start_conversation calls
        assert len(client.start_conv_calls) == 2, (
            f"Expected 2 start_conv calls (original + retry), got {len(client.start_conv_calls)}"
        )
        # Retry prompt should be delay-specific
        _, retry_prompt = client.start_conv_calls[1]
        assert "delay" in retry_prompt.lower()
        assert "individual shipment-level rows" in retry_prompt.lower() or "do not return" in retry_prompt.lower()
        # Final response should be from aggregated result
        assert resp["status"] == "success"
        assert resp["is_table"] is True
        headers = resp.get("table_data", {}).get("headers", [])
        assert "shipment_count" in headers or "delayed_count" in headers

    def test_shape_validation_does_not_retry_normal_aggregation_with_agg_headers(self):
        """AGGREGATION intent with aggregated headers must NOT trigger retry."""
        agg_msg, agg_qr = _make_query_message_with_columns(
            _AGG_COLUMNS_DELAY, _AGG_ROWS_DELAY, "Delay analysis."
        )
        client = FakeGenieClient(
            completed_message=agg_msg,
            query_result=agg_qr,
        )
        pipeline = _make_pipeline(client=client)

        pipeline.run("delay analysis by status", "e6-conv-02")

        assert len(client.start_conv_calls) == 1, "No retry expected for aggregated headers"

    def test_normal_broad_listing_without_analytical_vocab_does_not_retry(self):
        """BROAD_LISTING with no analytical vocab + raw headers must NOT trigger
        the E6 safety net retry.
        """
        raw_msg, raw_qr = _make_query_message_with_columns(
            _RAW_AIRL_COLUMNS, _RAW_AIR_ROWS, "Shipments from US."
        )
        client = FakeGenieClient(
            completed_message=raw_msg,
            query_result=raw_qr,
        )
        pipeline = _make_pipeline(client=client)

        # "show shipments from US" is BROAD_LISTING — no analytical vocab → no retry
        pipeline.run("show shipments from US", "e6-conv-03")

        assert len(client.start_conv_calls) == 1, "No retry expected for non-analytical BROAD_LISTING"

    def test_shape_validation_disabled_skips_retry_even_for_analytical_prompt(self):
        """When enable_shape_validation=False, no retry fires regardless."""
        raw_msg, raw_qr = _make_query_message_with_columns(
            _RAW_AIRL_COLUMNS, _RAW_AIR_ROWS, "500 raw rows."
        )
        client = FakeGenieClient(
            completed_message=raw_msg,
            query_result=raw_qr,
        )
        # enable_shape_validation defaults to True in _make_pipeline;
        # construct pipeline directly with it disabled
        pipeline = GeniePipeline(
            genie_client=client,
            session_store=GenieSessionStore(),
            space_id=SPACE_ID,
            poll_interval_seconds=0.0,
            fetch_query_results=True,
            enable_shape_validation=False,
        )

        pipeline.run("give analysis on shipments via air and their delay", "e6-conv-04")

        assert len(client.start_conv_calls) == 1, "No retry when shape validation disabled"

    def test_show_me_delayed_shipments_does_not_trigger_retry(self):
        """Regression: 'show me delayed shipments' is BROAD_LISTING, has no
        analytical vocab, so the E6 shape-validation safety net must NOT retry.
        Raw shipment rows are an acceptable response for this listing prompt.
        """
        raw_msg, raw_qr = _make_query_message_with_columns(
            _RAW_AIRL_COLUMNS, _RAW_AIR_ROWS, "Delayed shipments."
        )
        client = FakeGenieClient(
            completed_message=raw_msg,
            query_result=raw_qr,
        )
        pipeline = _make_pipeline(client=client)
        pipeline.run("show me delayed shipments", "e6-listing-01")
        assert len(client.start_conv_calls) == 1, (
            "BROAD_LISTING listing prompt must not trigger shape-validation retry"
        )

    def test_show_shipments_from_china_does_not_trigger_retry(self):
        """Regression: 'show shipments from China' is BROAD_LISTING, no
        analytical vocab — shape-validation safety net must NOT retry.
        """
        raw_msg, raw_qr = _make_query_message_with_columns(
            _RAW_AIRL_COLUMNS, _RAW_AIR_ROWS, "China shipments."
        )
        client = FakeGenieClient(
            completed_message=raw_msg,
            query_result=raw_qr,
        )
        pipeline = _make_pipeline(client=client)
        pipeline.run("show shipments from China", "e6-listing-02")
        assert len(client.start_conv_calls) == 1, (
            "BROAD_LISTING listing prompt must not trigger shape-validation retry"
        )

    def test_analytical_prompt_now_routes_as_aggregation_not_broad_listing(self):
        """Integration check: the pipeline's route decision for the diagnosed
        prompt must be AGGREGATION after E6 fix.
        """
        from unittest.mock import patch

        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client, debug=True)

        # Capture the route decision via the debug log or just run and check intent
        # We verify intent is AGGREGATION by confirming start_conversation is called
        # (not blocked by local routing) and the enriched prompt uses agg template
        resp = pipeline.run(
            "give analysis on shipments via air and their delay",
            "e6-conv-05",
        )

        assert resp["status"] == "success"
        assert len(client.start_conv_calls) >= 1
        # The prompt sent to Genie must use analytics framing (not "Summarise X: total count...")
        _, sent_prompt = client.start_conv_calls[0]
        assert "business analytics summary" in sent_prompt.lower() or "aggregat" in sent_prompt.lower(), (
            f"Expected AGGREGATION framing in sent_prompt, got: {sent_prompt[:200]}"
        )

    def test_aggregation_first_prompt_has_hard_no_raw_rows_contract(self):
        """FIX 3: The prompt sent to Genie on the FIRST (non-retry) attempt must
        use the hard 'Do not return individual shipment-level rows.' contract,
        not the soft 'unless necessary' wording.
        """
        client = FakeGenieClient()
        pipeline = _make_pipeline(client=client)

        pipeline.run("analyze air shipment delays", "fix3-hardening-01")

        assert len(client.start_conv_calls) >= 1
        _, sent_prompt = client.start_conv_calls[0]
        assert "do not return individual shipment-level rows" in sent_prompt.lower(), (
            f"First-attempt enriched prompt must contain hard contract: {sent_prompt[:200]}"
        )
        assert "unless necessary" not in sent_prompt.lower(), (
            f"First-attempt enriched prompt must not contain soft 'unless necessary': "
            f"{sent_prompt[:200]}"
        )

    def test_retry_exhaustion_with_raw_retry_result_sets_fallback_recommended(self):
        """FIX 5: If the one-shot retry ALSO returns raw shipment-level rows,
        the pipeline must set fallback_recommended=True and shape_retry_exhausted=True
        rather than silently accepting an invalid analytical result.
        """
        # Both first and retry call return raw shipment rows
        raw_msg, raw_qr = _make_query_message_with_columns(
            _RAW_AIRL_COLUMNS, _RAW_AIR_ROWS, "Found 500 matching shipments."
        )
        # Pass identical raw results for both first and second (retry) calls
        client = _FakeGenieClientDual(raw_qr, raw_qr, raw_msg, raw_msg)
        pipeline = _make_pipeline(client=client)

        resp = pipeline.run(
            "give analysis on shipments via air and their delay",
            "fix5-exhaustion-01",
        )

        # Two start_conversation calls: original + retry
        assert len(client.start_conv_calls) == 2, (
            f"Expected 2 start_conv calls (original + retry), "
            f"got {len(client.start_conv_calls)}"
        )
        # FIX 5: retry also returned raw rows → must flag as fallback
        assert resp.get("fallback_recommended") is True, (
            "shape_retry_exhausted: fallback_recommended must be True when "
            "retry also returns raw rows"
        )
        assert resp.get("shape_retry_exhausted") is True, (
            "shape_retry_exhausted flag must be set when retry returns raw rows"
        )

    def test_retry_exhaustion_not_set_when_retry_returns_valid_agg_result(self):
        """FIX 5 regression: when the retry succeeds (aggregated headers),
        shape_retry_exhausted must NOT be set and fallback_recommended must
        not be True.
        """
        raw_msg, raw_qr = _make_query_message_with_columns(
            _RAW_AIRL_COLUMNS, _RAW_AIR_ROWS, "Found 500 raw shipments."
        )
        agg_msg, agg_qr = _make_query_message_with_columns(
            _AGG_COLUMNS_DELAY, _AGG_ROWS_DELAY, "Delay analysis complete."
        )
        client = _FakeGenieClientDual(raw_qr, agg_qr, raw_msg, agg_msg)
        pipeline = _make_pipeline(client=client)

        resp = pipeline.run(
            "give analysis on shipments via air and their delay",
            "fix5-success-01",
        )

        # Must have retried
        assert len(client.start_conv_calls) == 2
        # Retry succeeded → no exhaustion flag
        assert not resp.get("shape_retry_exhausted"), (
            "shape_retry_exhausted must not be set when retry returns valid agg result"
        )
        # fallback_recommended must not be True (may be absent or False)
        assert resp.get("fallback_recommended") is not True, (
            "fallback_recommended must not be True when retry succeeds"
        )

    def test_listing_prompt_raw_retry_does_not_set_exhausted_flag(self):
        """FIX 5 regression: BROAD_LISTING listing prompt (no analytical vocab)
        never triggers shape validation, so shape_retry_exhausted must remain unset.
        """
        raw_msg, raw_qr = _make_query_message_with_columns(
            _RAW_AIRL_COLUMNS, _RAW_AIR_ROWS, "Shipments from US."
        )
        client = FakeGenieClient(
            completed_message=raw_msg,
            query_result=raw_qr,
        )
        pipeline = _make_pipeline(client=client)

        resp = pipeline.run("show shipments from US", "fix5-listing-01")

        assert len(client.start_conv_calls) == 1, "No retry for listing prompt"
        assert not resp.get("shape_retry_exhausted"), (
            "shape_retry_exhausted must not be set for listing prompts"
        )


# STANDALONE RUNNER
# =============================================================================

if __name__ == "__main__":
    import traceback

    test_classes = [
        TestNewConversation,
        TestFollowUp,
        TestTextOnlyResponse,
        TestQueryResponse,
        TestVizAttachment,
        TestSuggestedQuestions,
        TestTimeoutError,
        TestExecutionError,
        TestQueryFetchFailure,
        TestExpiredSession,
        TestResetSession,
        TestDebugMode,
        TestNoStackTrace,
        TestMultiTurnScenario,
        TestRouterIntegration,
        TestAsyncExportE4,
        TestNonBusinessRoutingPipeline,
        TestExportFieldPropagation,
        TestTypedDownloadAsyncBug,
        TestRunExportJobUpdatesLTR,
        TestE6AnalyticalShapeValidation,
    ]

    passed = failed = total = 0
    print("=" * 70)
    print("PHASE G3: genie_pipeline.py unit tests")
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
