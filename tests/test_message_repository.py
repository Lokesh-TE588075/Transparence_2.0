"""Tests for InMemoryMessageRepository (app/services/message_repository.py).

H1 -- TransparencE Conversation History Persistence
"""
import pytest
from datetime import datetime, timezone
from app.services.message_repository import (
    InMemoryMessageRepository,
    MessageRecord,
    MessageRepositoryError,
    MessageSequenceConflictError,
    MessageValidationError,
    MESSAGE_TEXT_MAX_LEN,
    PAYLOAD_JSON_MAX_BYTES,
    MAX_PAGE_SIZE,
)


OWNER_A = "owner_hash_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OWNER_B = "owner_hash_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
CONV_1 = "conv-id-0001"
CONV_2 = "conv-id-0002"


@pytest.fixture
def repo():
    return InMemoryMessageRepository()


# ---------------------------------------------------------------------------
# Basic append
# ---------------------------------------------------------------------------

class TestAppendMessage:
    def test_first_message_gets_sequence_1(self, repo):
        rec = repo.append_message(
            owner_user_id_hash=OWNER_A,
            frontend_conversation_id=CONV_1,
            role="user",
            message_text="hello",
        )
        assert rec.message_sequence == 1

    def test_second_message_gets_sequence_2(self, repo):
        repo.append_message(OWNER_A, CONV_1, "user", "hello")
        rec = repo.append_message(OWNER_A, CONV_1, "assistant", "hi there")
        assert rec.message_sequence == 2

    def test_fields_are_set_correctly(self, repo):
        rec = repo.append_message(
            owner_user_id_hash=OWNER_A,
            frontend_conversation_id=CONV_1,
            role="assistant",
            message_text="response",
            response_payload_json='{"status": "success"}',
        )
        assert rec.owner_user_id_hash == OWNER_A
        assert rec.frontend_conversation_id == CONV_1
        assert rec.role == "assistant"
        assert rec.message_text == "response"
        assert rec.response_payload_json == '{"status": "success"}'
        assert rec.is_active is True

    def test_message_id_is_set(self, repo):
        rec = repo.append_message(OWNER_A, CONV_1, "user", "hello")
        assert rec.message_id and len(rec.message_id) > 0

    def test_idempotent_on_duplicate_message_id(self, repo):
        import uuid
        mid = str(uuid.uuid4())
        rec1 = repo.append_message(OWNER_A, CONV_1, "user", "hello", message_id=mid)
        rec2 = repo.append_message(OWNER_A, CONV_1, "user", "hello", message_id=mid)
        assert rec1.message_id == rec2.message_id
        assert repo._total_stored() == 1

    def test_text_too_long_raises_validation_error(self, repo):
        long_text = "x" * (MESSAGE_TEXT_MAX_LEN + 1)
        with pytest.raises(MessageValidationError):
            repo.append_message(OWNER_A, CONV_1, "user", long_text)

    def test_payload_too_large_raises_validation_error(self, repo):
        huge_payload = "x" * (PAYLOAD_JSON_MAX_BYTES + 1)
        with pytest.raises(MessageValidationError):
            repo.append_message(OWNER_A, CONV_1, "assistant", "text", response_payload_json=huge_payload)

    def test_empty_text_raises_validation_error(self, repo):
        with pytest.raises(MessageValidationError):
            repo.append_message(OWNER_A, CONV_1, "user", "")

    def test_invalid_role_raises_validation_error(self, repo):
        with pytest.raises(MessageValidationError):
            repo.append_message(OWNER_A, CONV_1, "system", "hello")


# ---------------------------------------------------------------------------
# Owner isolation
# ---------------------------------------------------------------------------

class TestOwnerIsolation:
    def test_different_owners_have_independent_sequences(self, repo):
        rec_a = repo.append_message(OWNER_A, CONV_1, "user", "hello from A")
        rec_b = repo.append_message(OWNER_B, CONV_1, "user", "hello from B")
        assert rec_a.message_sequence == 1
        assert rec_b.message_sequence == 1

    def test_owner_a_cannot_read_owner_b_messages(self, repo):
        repo.append_message(OWNER_B, CONV_1, "user", "secret")
        recs, total = repo.list_messages(OWNER_A, CONV_1, page=1, page_size=10)
        assert total == 0
        assert recs == []


# ---------------------------------------------------------------------------
# list_messages
# ---------------------------------------------------------------------------

class TestListMessages:
    def test_returns_messages_in_sequence_order(self, repo):
        repo.append_message(OWNER_A, CONV_1, "user", "first")
        repo.append_message(OWNER_A, CONV_1, "assistant", "second")
        repo.append_message(OWNER_A, CONV_1, "user", "third")
        recs, total = repo.list_messages(OWNER_A, CONV_1, page=1, page_size=10)
        assert total == 3
        assert [r.message_sequence for r in recs] == [1, 2, 3]

    def test_empty_for_unknown_conversation(self, repo):
        recs, total = repo.list_messages(OWNER_A, "unknown-conv", page=1, page_size=10)
        assert total == 0
        assert recs == []

    def test_pagination_page_1(self, repo):
        for i in range(6):
            repo.append_message(OWNER_A, CONV_1, "user", f"msg {i}")
        recs, total = repo.list_messages(OWNER_A, CONV_1, page=1, page_size=3)
        assert total == 6
        assert len(recs) == 3
        assert recs[0].message_sequence == 1

    def test_pagination_page_2(self, repo):
        for i in range(6):
            repo.append_message(OWNER_A, CONV_1, "user", f"msg {i}")
        recs, total = repo.list_messages(OWNER_A, CONV_1, page=2, page_size=3)
        assert total == 6
        assert len(recs) == 3
        assert recs[0].message_sequence == 4

    def test_page_size_capped_at_max(self, repo):
        for i in range(5):
            repo.append_message(OWNER_A, CONV_1, "user", f"msg {i}")
        recs, total = repo.list_messages(OWNER_A, CONV_1, page=1, page_size=MAX_PAGE_SIZE + 50)
        assert len(recs) == 5  # only 5 messages exist

    def test_deactivated_messages_excluded(self, repo):
        repo.append_message(OWNER_A, CONV_1, "user", "msg 1")
        repo.append_message(OWNER_A, CONV_1, "assistant", "msg 2")
        repo.deactivate_messages(OWNER_A, CONV_1)
        recs, total = repo.list_messages(OWNER_A, CONV_1, page=1, page_size=10)
        assert total == 0
        assert recs == []


# ---------------------------------------------------------------------------
# deactivate_messages
# ---------------------------------------------------------------------------

class TestDeactivateMessages:
    def test_deactivate_sets_is_active_false(self, repo):
        repo.append_message(OWNER_A, CONV_1, "user", "hello")
        repo.deactivate_messages(OWNER_A, CONV_1)
        # Use internal helper to read all (including inactive)
        all_recs = repo._all_for(OWNER_A, CONV_1)
        assert all(not r.is_active for r in all_recs)

    def test_deactivate_only_affects_target_conversation(self, repo):
        repo.append_message(OWNER_A, CONV_1, "user", "in conv 1")
        repo.append_message(OWNER_A, CONV_2, "user", "in conv 2")
        repo.deactivate_messages(OWNER_A, CONV_1)
        recs_2, total_2 = repo.list_messages(OWNER_A, CONV_2, page=1, page_size=10)
        assert total_2 == 1
        assert recs_2[0].is_active is True

    def test_deactivate_idempotent_on_already_inactive(self, repo):
        repo.append_message(OWNER_A, CONV_1, "user", "hello")
        repo.deactivate_messages(OWNER_A, CONV_1)
        # Second call should not raise
        repo.deactivate_messages(OWNER_A, CONV_1)
        all_recs = repo._all_for(OWNER_A, CONV_1)
        assert all(not r.is_active for r in all_recs)

    def test_deactivate_only_affects_owner_a(self, repo):
        repo.append_message(OWNER_A, CONV_1, "user", "A msg")
        repo.append_message(OWNER_B, CONV_1, "user", "B msg")
        repo.deactivate_messages(OWNER_A, CONV_1)
        recs_b, total_b = repo.list_messages(OWNER_B, CONV_1, page=1, page_size=10)
        assert total_b == 1
