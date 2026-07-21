"""Tests for MessageHistoryService (app/services/message_history_service.py).

H1 -- TransparencE Conversation History Persistence
"""
import json
import pytest
from unittest.mock import MagicMock, patch
from app.services.message_history_service import (
    build_response_payload,
    parse_response_payload,
    deactivate_conversation_messages,
    HISTORY_MAX_TABLE_ROWS,
    HISTORY_MAX_SUGGESTED_QUESTIONS,
    _FORBIDDEN_PAYLOAD_FIELDS,
)
from app.services.message_repository import (
    InMemoryMessageRepository,
    MESSAGE_TEXT_MAX_LEN,
    PAYLOAD_JSON_MAX_BYTES,
)


OWNER = "owner_hash_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CONV = "conv-id-0001"


# ---------------------------------------------------------------------------
# build_response_payload
# ---------------------------------------------------------------------------

class TestBuildResponsePayload:
    def _genie_result(self, **overrides):
        base = {
            "status": "success",
            "message": "Here are the results.",
            "is_table": True,
            "table_data": {"headers": ["col1", "col2"], "rows": [["a", "b"]]},
            "row_count": 1,
            "suggested_questions": ["q1", "q2"],
            "computed_chart_data": {"data": [], "x_key": "col1", "y_key": "col2"},
            "computed_metrics": {"avg": 1},
            "source": "genie",
            "query_description": "Shipments by country",
            "has_visualization": True,
            # Forbidden fields that must be stripped:
            "genie_conversation_id": "GC-SECRET",
            "genie_message_id": "GM-SECRET",
            "generated_sql": "SELECT * FROM table",
            "genie_thought_description": "internal thought",
            "fallback_recommended": False,
            "debug_info": {"trace": "sensitive"},
        }
        base.update(overrides)
        return base

    def test_returns_json_string(self):
        result = build_response_payload(self._genie_result())
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert isinstance(parsed, dict)

    def test_includes_status(self):
        parsed = json.loads(build_response_payload(self._genie_result()))
        assert parsed["status"] == "success"

    def test_includes_safe_fields(self):
        parsed = json.loads(build_response_payload(self._genie_result()))
        assert "is_table" in parsed
        assert "row_count" in parsed
        assert "source" in parsed
        assert "query_description" in parsed
        assert "has_visualization" in parsed

    def test_excludes_all_forbidden_fields(self):
        parsed = json.loads(build_response_payload(self._genie_result()))
        for field in _FORBIDDEN_PAYLOAD_FIELDS:
            assert field not in parsed, f"Forbidden field present: {field}"

    def test_caps_table_rows_at_max(self):
        large_table = {
            "headers": ["a"],
            "rows": [[str(i)] for i in range(HISTORY_MAX_TABLE_ROWS + 50)],
        }
        parsed = json.loads(build_response_payload(self._genie_result(table_data=large_table)))
        assert len(parsed["table_data"]["rows"]) == HISTORY_MAX_TABLE_ROWS

    def test_caps_suggested_questions(self):
        many_questions = [f"q{i}" for i in range(HISTORY_MAX_SUGGESTED_QUESTIONS + 5)]
        parsed = json.loads(build_response_payload(self._genie_result(suggested_questions=many_questions)))
        assert len(parsed["suggested_questions"]) == HISTORY_MAX_SUGGESTED_QUESTIONS

    def test_none_table_data_handled(self):
        parsed = json.loads(build_response_payload(self._genie_result(table_data=None)))
        assert parsed.get("table_data") is None

    def test_drops_table_data_when_payload_too_large(self):
        # Build a table that makes the payload too large
        huge_rows = [["x" * 500] for _ in range(HISTORY_MAX_TABLE_ROWS)]
        large_table = {"headers": ["col"], "rows": huge_rows}
        # Should not raise; table_data should be dropped or stripped
        result = build_response_payload(self._genie_result(table_data=large_table))
        assert isinstance(result, str)
        assert len(result.encode()) <= PAYLOAD_JSON_MAX_BYTES


# ---------------------------------------------------------------------------
# parse_response_payload
# ---------------------------------------------------------------------------

class TestParseResponsePayload:
    def test_returns_dict_for_valid_json(self):
        result = parse_response_payload('{"status": "success", "is_table": true}')
        assert result == {"status": "success", "is_table": True}

    def test_returns_empty_dict_for_none(self):
        assert parse_response_payload(None) == {}

    def test_returns_empty_dict_for_empty_string(self):
        assert parse_response_payload("") == {}

    def test_returns_empty_dict_for_invalid_json(self):
        assert parse_response_payload("not json") == {}

    def test_returns_empty_dict_for_json_array(self):
        # Arrays are not dicts; should return empty
        assert parse_response_payload("[1, 2, 3]") == {}

    def test_preserves_all_safe_fields(self):
        payload = json.dumps({"status": "success", "row_count": 5, "source": "genie"})
        result = parse_response_payload(payload)
        assert result["row_count"] == 5


# ---------------------------------------------------------------------------
# deactivate_conversation_messages
# ---------------------------------------------------------------------------

class TestDeactivateConversationMessages:
    def test_deactivates_messages(self):
        repo = InMemoryMessageRepository()
        repo.append_message(OWNER, CONV, "user", "hello")
        deactivate_conversation_messages(repo, OWNER, CONV)
        all_recs = repo._all_for(OWNER, CONV)
        assert all(not r.is_active for r in all_recs)

    def test_does_not_raise_on_empty_conversation(self):
        repo = InMemoryMessageRepository()
        # Should not raise when no messages exist
        deactivate_conversation_messages(repo, OWNER, CONV)

    def test_does_not_raise_on_repository_error(self):
        mock_repo = MagicMock()
        mock_repo.deactivate_messages.side_effect = Exception("DB down")
        # deactivate_conversation_messages must swallow errors (non-blocking)
        deactivate_conversation_messages(mock_repo, OWNER, CONV)

    def test_logs_error_on_repository_failure(self, caplog):
        import logging
        mock_repo = MagicMock()
        mock_repo.deactivate_messages.side_effect = Exception("DB down")
        with caplog.at_level(logging.ERROR):
            deactivate_conversation_messages(mock_repo, OWNER, CONV)
        # Should log something about the failure
        assert any("DB down" in r.message or "deactivat" in r.message.lower() for r in caplog.records)
