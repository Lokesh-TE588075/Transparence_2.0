"""Unit tests for app.services.conversation_repository.

Covers:
  - ConversationRecord domain model validation
  - InMemoryConversationRepository: creation, lookup, mutations,
    listing, deletion
  - Thread-safety guarantees (concurrent creates and CAS conflicts)
  - Interface / dependency-isolation guarantees

All tests are self-contained.  No network, database, or file I/O occurs.
Live smoke suites are separate files excluded from this run.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from app.services.conversation_repository import (
    ConversationAlreadyExistsError,
    ConversationNotFoundError,
    ConversationOwnershipError,
    ConversationRecord,
    ConversationRepository,
    ConversationRepositoryError,
    ConversationStatus,
    ConversationVersionConflictError,
    InMemoryConversationRepository,
)

# ---------------------------------------------------------------------------
# Shared deterministic timestamps and owner identifiers
# ---------------------------------------------------------------------------

TS_BASE = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
TS_LATER = TS_BASE + timedelta(seconds=60)
TS_EVEN_LATER = TS_BASE + timedelta(seconds=120)

OWNER_A = "sha256_hash_owner_a"
OWNER_B = "sha256_hash_owner_b"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(**overrides) -> ConversationRecord:
    """Return a valid ConversationRecord with sensible defaults."""
    defaults: dict = dict(
        conversation_id="conv-001",
        owner_user_id_hash=OWNER_A,
        frontend_conversation_id="fe-001",
        genie_conversation_id=None,
        last_genie_message_id=None,
        status=ConversationStatus.ACTIVE,
        version=1,
        created_at=TS_BASE,
        updated_at=TS_BASE,
        last_active_at=TS_BASE,
    )
    defaults.update(overrides)
    return ConversationRecord(**defaults)


@pytest.fixture()
def repo() -> InMemoryConversationRepository:
    """Fresh in-memory repository for each test."""
    return InMemoryConversationRepository()


# ===========================================================================
# 1. Domain model
# ===========================================================================


class TestDomainModel:

    def test_valid_record_constructs(self):
        r = _make_record()
        assert r.conversation_id == "conv-001"
        assert r.owner_user_id_hash == OWNER_A
        assert r.frontend_conversation_id == "fe-001"
        assert r.genie_conversation_id is None
        assert r.last_genie_message_id is None
        assert r.status == ConversationStatus.ACTIVE
        assert r.version == 1
        assert r.created_at == TS_BASE
        assert r.updated_at == TS_BASE
        assert r.last_active_at == TS_BASE

    def test_empty_conversation_id_rejected(self):
        with pytest.raises(ValueError, match="conversation_id"):
            _make_record(conversation_id="")

    def test_empty_owner_rejected(self):
        with pytest.raises(ValueError, match="owner_user_id_hash"):
            _make_record(owner_user_id_hash="")

    def test_empty_frontend_id_rejected(self):
        with pytest.raises(ValueError, match="frontend_conversation_id"):
            _make_record(frontend_conversation_id="")

    def test_version_zero_rejected(self):
        with pytest.raises(ValueError, match="version"):
            _make_record(version=0)

    def test_version_negative_rejected(self):
        with pytest.raises(ValueError, match="version"):
            _make_record(version=-1)

    def test_naive_created_at_rejected(self):
        with pytest.raises(ValueError, match="created_at"):
            _make_record(created_at=datetime(2024, 6, 1, 12, 0, 0))

    def test_naive_updated_at_rejected(self):
        with pytest.raises(ValueError, match="updated_at"):
            _make_record(updated_at=datetime(2024, 6, 1, 12, 0, 0))

    def test_naive_last_active_at_rejected(self):
        with pytest.raises(ValueError, match="last_active_at"):
            _make_record(last_active_at=datetime(2024, 6, 1, 12, 0, 0))

    def test_updated_at_before_created_at_rejected(self):
        earlier = TS_BASE - timedelta(seconds=1)
        with pytest.raises(ValueError, match="updated_at"):
            _make_record(updated_at=earlier)

    def test_last_active_at_before_created_at_rejected(self):
        earlier = TS_BASE - timedelta(seconds=1)
        with pytest.raises(ValueError, match="last_active_at"):
            _make_record(last_active_at=earlier)

    def test_record_is_frozen(self):
        r = _make_record()
        with pytest.raises(Exception):
            r.version = 99  # type: ignore[misc]


# ===========================================================================
# 2. Creation
# ===========================================================================


class TestCreation:

    def test_create_generates_uuid_when_no_id_supplied(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-auto")
        assert r.conversation_id
        uuid.UUID(r.conversation_id)  # raises if not a valid UUID

    def test_create_sets_active_status(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        assert r.status == ConversationStatus.ACTIVE

    def test_create_sets_version_one(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        assert r.version == 1

    def test_create_timestamps_are_equal_on_first_create(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001", now=TS_BASE)
        assert r.created_at == TS_BASE
        assert r.updated_at == TS_BASE
        assert r.last_active_at == TS_BASE

    def test_explicit_conversation_id_accepted(self, repo):
        cid = str(uuid.uuid4())
        r = repo.create_conversation(OWNER_A, "fe-explicit", conversation_id=cid)
        assert r.conversation_id == cid

    def test_duplicate_logical_create_returns_same_record(self, repo):
        r1 = repo.create_conversation(OWNER_A, "fe-dup")
        r2 = repo.create_conversation(OWNER_A, "fe-dup")
        assert r1.conversation_id == r2.conversation_id

    def test_duplicate_logical_create_does_not_increment_version(self, repo):
        repo.create_conversation(OWNER_A, "fe-dup")
        r2 = repo.create_conversation(OWNER_A, "fe-dup")
        assert r2.version == 1

    def test_frontend_uuid_collision_across_users_creates_separate_records(self, repo):
        fe_id = "fe-shared"
        r_a = repo.create_conversation(OWNER_A, fe_id)
        r_b = repo.create_conversation(OWNER_B, fe_id)
        assert r_a.conversation_id != r_b.conversation_id
        assert r_a.owner_user_id_hash == OWNER_A
        assert r_b.owner_user_id_hash == OWNER_B

    def test_explicit_global_conversation_id_collision_rejected(self, repo):
        cid = str(uuid.uuid4())
        repo.create_conversation(OWNER_A, "fe-first", conversation_id=cid)
        with pytest.raises(ConversationAlreadyExistsError):
            repo.create_conversation(OWNER_A, "fe-second", conversation_id=cid)

    def test_ownership_collision_raises_ownership_error(self, repo):
        cid = str(uuid.uuid4())
        repo.create_conversation(OWNER_A, "fe-a", conversation_id=cid)
        with pytest.raises(ConversationOwnershipError):
            repo.create_conversation(OWNER_B, "fe-b", conversation_id=cid)


# ===========================================================================
# 3. Lookup
# ===========================================================================


class TestLookup:

    def test_get_by_conversation_id(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        found = repo.get_by_id(OWNER_A, r.conversation_id)
        assert found is not None
        assert found.conversation_id == r.conversation_id

    def test_get_by_frontend_id(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        found = repo.get_by_frontend_id(OWNER_A, "fe-001")
        assert found is not None
        assert found.conversation_id == r.conversation_id

    def test_missing_record_get_by_id_returns_none(self, repo):
        assert repo.get_by_id(OWNER_A, "nonexistent-id") is None

    def test_missing_record_get_by_frontend_id_returns_none(self, repo):
        assert repo.get_by_frontend_id(OWNER_A, "nonexistent-fe") is None

    def test_cross_user_get_by_id_returns_none(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        assert repo.get_by_id(OWNER_B, r.conversation_id) is None

    def test_cross_user_get_by_frontend_id_returns_none(self, repo):
        repo.create_conversation(OWNER_A, "fe-001")
        assert repo.get_by_frontend_id(OWNER_B, "fe-001") is None

    def test_returned_record_is_frozen_immutable(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        found = repo.get_by_id(OWNER_A, r.conversation_id)
        assert found is not None
        with pytest.raises(Exception):
            found.version = 99  # type: ignore[misc]


# ===========================================================================
# 4. Genie binding
# ===========================================================================


class TestGenieBinding:

    def test_bind_genie_conversation_sets_field(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001", now=TS_BASE)
        updated = repo.bind_genie_conversation(
            OWNER_A, r.conversation_id, "genie-abc",
            expected_version=1, now=TS_LATER,
        )
        assert updated.genie_conversation_id == "genie-abc"

    def test_bind_increments_version(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        updated = repo.bind_genie_conversation(
            OWNER_A, r.conversation_id, "genie-abc",
            expected_version=1,
        )
        assert updated.version == 2

    def test_bind_updates_updated_at(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001", now=TS_BASE)
        updated = repo.bind_genie_conversation(
            OWNER_A, r.conversation_id, "genie-abc",
            expected_version=1, now=TS_LATER,
        )
        assert updated.updated_at == TS_LATER

    def test_empty_genie_id_rejected(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        with pytest.raises(ValueError):
            repo.bind_genie_conversation(
                OWNER_A, r.conversation_id, "",
                expected_version=1,
            )

    def test_stale_expected_version_raises_conflict(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        with pytest.raises(ConversationVersionConflictError):
            repo.bind_genie_conversation(
                OWNER_A, r.conversation_id, "genie-abc",
                expected_version=99,
            )

    def test_failed_version_update_leaves_record_unchanged(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        try:
            repo.bind_genie_conversation(
                OWNER_A, r.conversation_id, "genie-abc",
                expected_version=99,
            )
        except ConversationVersionConflictError:
            pass
        found = repo.get_by_id(OWNER_A, r.conversation_id)
        assert found is not None
        assert found.genie_conversation_id is None
        assert found.version == 1

    def test_bind_not_found_for_missing_conversation(self, repo):
        with pytest.raises(ConversationNotFoundError):
            repo.bind_genie_conversation(
                OWNER_A, "no-such-id", "genie-abc",
                expected_version=1,
            )


# ===========================================================================
# 5. Message update
# ===========================================================================


class TestMessageUpdate:

    def test_update_last_genie_message_sets_field(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        updated = repo.update_last_genie_message(
            OWNER_A, r.conversation_id, "msg-001",
            expected_version=1,
        )
        assert updated.last_genie_message_id == "msg-001"

    def test_empty_message_id_rejected(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        with pytest.raises(ValueError):
            repo.update_last_genie_message(
                OWNER_A, r.conversation_id, "",
                expected_version=1,
            )

    def test_update_increments_version(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        updated = repo.update_last_genie_message(
            OWNER_A, r.conversation_id, "msg-001",
            expected_version=1,
        )
        assert updated.version == 2

    def test_cross_user_update_raises_not_found(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        with pytest.raises(ConversationNotFoundError):
            repo.update_last_genie_message(
                OWNER_B, r.conversation_id, "msg-001",
                expected_version=1,
            )


# ===========================================================================
# 6. Touch
# ===========================================================================


class TestTouch:

    def test_touch_updates_last_active_at(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001", now=TS_BASE)
        updated = repo.touch(
            OWNER_A, r.conversation_id,
            expected_version=1, now=TS_LATER,
        )
        assert updated.last_active_at == TS_LATER

    def test_touch_updates_updated_at(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001", now=TS_BASE)
        updated = repo.touch(
            OWNER_A, r.conversation_id,
            expected_version=1, now=TS_LATER,
        )
        assert updated.updated_at == TS_LATER

    def test_touch_increments_version(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        updated = repo.touch(OWNER_A, r.conversation_id, expected_version=1)
        assert updated.version == 2

    def test_touch_preserves_genie_conversation_id(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        r2 = repo.bind_genie_conversation(
            OWNER_A, r.conversation_id, "genie-xyz",
            expected_version=1,
        )
        r3 = repo.touch(OWNER_A, r.conversation_id, expected_version=2)
        assert r3.genie_conversation_id == "genie-xyz"

    def test_touch_preserves_last_genie_message_id(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        r2 = repo.update_last_genie_message(
            OWNER_A, r.conversation_id, "msg-preserve",
            expected_version=1,
        )
        r3 = repo.touch(OWNER_A, r.conversation_id, expected_version=2)
        assert r3.last_genie_message_id == "msg-preserve"

    def test_touch_preserves_created_at(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001", now=TS_BASE)
        updated = repo.touch(
            OWNER_A, r.conversation_id,
            expected_version=1, now=TS_LATER,
        )
        assert updated.created_at == TS_BASE


# ===========================================================================
# 7. Status
# ===========================================================================


class TestStatus:

    def test_set_stale(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        updated = repo.set_status(
            OWNER_A, r.conversation_id,
            ConversationStatus.STALE, expected_version=1,
        )
        assert updated.status == ConversationStatus.STALE

    def test_set_reset(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        updated = repo.set_status(
            OWNER_A, r.conversation_id,
            ConversationStatus.RESET, expected_version=1,
        )
        assert updated.status == ConversationStatus.RESET

    def test_set_expired(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        updated = repo.set_status(
            OWNER_A, r.conversation_id,
            ConversationStatus.EXPIRED, expected_version=1,
        )
        assert updated.status == ConversationStatus.EXPIRED

    def test_status_update_increments_version(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        updated = repo.set_status(
            OWNER_A, r.conversation_id,
            ConversationStatus.STALE, expected_version=1,
        )
        assert updated.version == 2

    def test_status_update_preserves_conversation_id(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        updated = repo.set_status(
            OWNER_A, r.conversation_id,
            ConversationStatus.STALE, expected_version=1,
        )
        assert updated.conversation_id == r.conversation_id

    def test_status_update_preserves_genie_ids(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        r2 = repo.bind_genie_conversation(
            OWNER_A, r.conversation_id, "genie-preserve",
            expected_version=1,
        )
        r3 = repo.set_status(
            OWNER_A, r.conversation_id,
            ConversationStatus.STALE, expected_version=2,
        )
        assert r3.genie_conversation_id == "genie-preserve"
        assert r3.frontend_conversation_id == "fe-001"

    def test_status_update_version_conflict_leaves_status_unchanged(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        try:
            repo.set_status(
                OWNER_A, r.conversation_id,
                ConversationStatus.EXPIRED, expected_version=99,
            )
        except ConversationVersionConflictError:
            pass
        found = repo.get_by_id(OWNER_A, r.conversation_id)
        assert found is not None
        assert found.status == ConversationStatus.ACTIVE


# ===========================================================================
# 8. Compare and update
# ===========================================================================


class TestCompareAndUpdate:

    def test_update_multiple_fields_atomically(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        updated = repo.compare_and_update(
            OWNER_A, r.conversation_id,
            expected_version=1,
            genie_conversation_id="genie-multi",
            last_genie_message_id="msg-multi",
            status=ConversationStatus.STALE,
        )
        assert updated.genie_conversation_id == "genie-multi"
        assert updated.last_genie_message_id == "msg-multi"
        assert updated.status == ConversationStatus.STALE

    def test_version_increments_exactly_once(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        updated = repo.compare_and_update(
            OWNER_A, r.conversation_id,
            expected_version=1,
            genie_conversation_id="g",
            last_genie_message_id="m",
            status=ConversationStatus.STALE,
        )
        assert updated.version == 2

    def test_stale_version_changes_nothing(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        try:
            repo.compare_and_update(
                OWNER_A, r.conversation_id,
                expected_version=99,
                genie_conversation_id="genie-fail",
            )
        except ConversationVersionConflictError:
            pass
        found = repo.get_by_id(OWNER_A, r.conversation_id)
        assert found is not None
        assert found.genie_conversation_id is None
        assert found.version == 1

    def test_touch_last_active_false_preserves_last_active_at(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001", now=TS_BASE)
        updated = repo.compare_and_update(
            OWNER_A, r.conversation_id,
            expected_version=1,
            touch_last_active=False,
            now=TS_LATER,
        )
        assert updated.last_active_at == TS_BASE

    def test_touch_last_active_true_updates_last_active_at(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001", now=TS_BASE)
        updated = repo.compare_and_update(
            OWNER_A, r.conversation_id,
            expected_version=1,
            touch_last_active=True,
            now=TS_LATER,
        )
        assert updated.last_active_at == TS_LATER

    def test_explicit_field_updates_persist_in_store(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        repo.compare_and_update(
            OWNER_A, r.conversation_id,
            expected_version=1,
            genie_conversation_id="genie-persist",
        )
        found = repo.get_by_id(OWNER_A, r.conversation_id)
        assert found is not None
        assert found.genie_conversation_id == "genie-persist"

    def test_no_op_update_still_increments_version(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        updated = repo.compare_and_update(
            OWNER_A, r.conversation_id,
            expected_version=1,
        )
        assert updated.version == 2


# ===========================================================================
# 9. Listing
# ===========================================================================


class TestListing:

    def test_list_only_owner_conversations(self, repo):
        repo.create_conversation(OWNER_A, "fe-a1")
        repo.create_conversation(OWNER_A, "fe-a2")
        repo.create_conversation(OWNER_B, "fe-b1")
        results = repo.list_for_owner(OWNER_A)
        assert len(results) == 2
        assert all(r.owner_user_id_hash == OWNER_A for r in results)

    def test_list_sorted_by_updated_at_descending(self, repo):
        repo.create_conversation(OWNER_A, "fe-oldest", now=TS_BASE)
        repo.create_conversation(OWNER_A, "fe-newest", now=TS_LATER)
        results = repo.list_for_owner(OWNER_A)
        assert results[0].frontend_conversation_id == "fe-newest"
        assert results[1].frontend_conversation_id == "fe-oldest"

    def test_filter_by_status_active(self, repo):
        r1 = repo.create_conversation(OWNER_A, "fe-active")
        r2 = repo.create_conversation(OWNER_A, "fe-stale")
        repo.set_status(
            OWNER_A, r2.conversation_id,
            ConversationStatus.STALE, expected_version=1,
        )
        results = repo.list_for_owner(
            OWNER_A, statuses={ConversationStatus.ACTIVE}
        )
        assert len(results) == 1
        assert results[0].frontend_conversation_id == "fe-active"

    def test_filter_by_multiple_statuses(self, repo):
        r1 = repo.create_conversation(OWNER_A, "fe-a")
        r2 = repo.create_conversation(OWNER_A, "fe-b")
        r3 = repo.create_conversation(OWNER_A, "fe-c")
        repo.set_status(OWNER_A, r2.conversation_id, ConversationStatus.STALE, expected_version=1)
        repo.set_status(OWNER_A, r3.conversation_id, ConversationStatus.EXPIRED, expected_version=1)
        results = repo.list_for_owner(
            OWNER_A,
            statuses={ConversationStatus.ACTIVE, ConversationStatus.STALE},
        )
        statuses_found = {r.status for r in results}
        assert ConversationStatus.ACTIVE in statuses_found
        assert ConversationStatus.STALE in statuses_found
        assert ConversationStatus.EXPIRED not in statuses_found

    def test_limit_enforced(self, repo):
        for i in range(5):
            repo.create_conversation(OWNER_A, f"fe-{i}")
        results = repo.list_for_owner(OWNER_A, limit=3)
        assert len(results) <= 3

    def test_invalid_limit_zero_rejected(self, repo):
        with pytest.raises(ValueError):
            repo.list_for_owner(OWNER_A, limit=0)

    def test_invalid_limit_negative_rejected(self, repo):
        with pytest.raises(ValueError):
            repo.list_for_owner(OWNER_A, limit=-1)

    def test_two_owners_remain_isolated(self, repo):
        shared_fe = "fe-same"
        repo.create_conversation(OWNER_A, shared_fe)
        repo.create_conversation(OWNER_B, shared_fe)
        a_results = repo.list_for_owner(OWNER_A)
        b_results = repo.list_for_owner(OWNER_B)
        assert len(a_results) == 1
        assert len(b_results) == 1
        assert a_results[0].owner_user_id_hash == OWNER_A
        assert b_results[0].owner_user_id_hash == OWNER_B

    def test_empty_list_when_no_conversations(self, repo):
        results = repo.list_for_owner(OWNER_A)
        assert results == []


# ===========================================================================
# 10. Deletion
# ===========================================================================


class TestDeletion:

    def test_owner_can_delete_own_record(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        result = repo.delete_conversation(OWNER_A, r.conversation_id)
        assert result is True

    def test_delete_removes_from_id_index(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        repo.delete_conversation(OWNER_A, r.conversation_id)
        assert repo.get_by_id(OWNER_A, r.conversation_id) is None

    def test_delete_removes_from_frontend_index(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        repo.delete_conversation(OWNER_A, r.conversation_id)
        assert repo.get_by_frontend_id(OWNER_A, "fe-001") is None

    def test_repeat_delete_returns_false(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        repo.delete_conversation(OWNER_A, r.conversation_id)
        result = repo.delete_conversation(OWNER_A, r.conversation_id)
        assert result is False

    def test_other_user_cannot_delete_record(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        result = repo.delete_conversation(OWNER_B, r.conversation_id)
        assert result is False

    def test_other_user_delete_leaves_record_intact(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        repo.delete_conversation(OWNER_B, r.conversation_id)
        assert repo.get_by_id(OWNER_A, r.conversation_id) is not None

    def test_delete_nonexistent_returns_false(self, repo):
        result = repo.delete_conversation(OWNER_A, "no-such-id")
        assert result is False

    def test_after_delete_create_same_logical_key_works(self, repo):
        r = repo.create_conversation(OWNER_A, "fe-001")
        repo.delete_conversation(OWNER_A, r.conversation_id)
        r2 = repo.create_conversation(OWNER_A, "fe-001")
        assert r2.version == 1
        assert r2.status == ConversationStatus.ACTIVE


# ===========================================================================
# 11. Thread safety
# ===========================================================================


class TestThreadSafety:

    def test_concurrent_duplicate_creates_result_in_one_record(self, repo):
        """Multiple threads creating the same logical conversation yields
        exactly one unique conversation_id (idempotency under contention)."""
        results: List[str] = []
        errors: List[Exception] = []
        barrier = threading.Barrier(10)

        def _create() -> None:
            barrier.wait()
            try:
                r = repo.create_conversation(OWNER_A, "fe-concurrent")
                results.append(r.conversation_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_create) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected errors: {errors}"
        assert len(results) == 10
        assert len(set(results)) == 1, "All threads must get the same conversation_id"

    def test_concurrent_updates_same_version_yields_exactly_one_success(self, repo):
        """Concurrent CAS bind_genie_conversation with expected_version=1:
        exactly one succeeds and the rest raise ConversationVersionConflictError."""
        r = repo.create_conversation(OWNER_A, "fe-cas")
        successes: List[str] = []
        conflicts: List[Exception] = []
        barrier = threading.Barrier(5)

        def _bind(genie_id: str) -> None:
            barrier.wait()
            try:
                repo.bind_genie_conversation(
                    OWNER_A, r.conversation_id, genie_id,
                    expected_version=1,
                )
                successes.append(genie_id)
            except ConversationVersionConflictError as exc:
                conflicts.append(exc)

        threads = [
            threading.Thread(target=_bind, args=(f"genie-{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(successes) == 1, f"Expected 1 success, got {len(successes)}"
        assert len(conflicts) == 4, f"Expected 4 conflicts, got {len(conflicts)}"

    def test_indexes_remain_consistent_after_concurrent_creates(self, repo):
        """After concurrent creates for distinct frontend IDs, every created
        conversation_id is retrievable from both indexes."""
        created: List[ConversationRecord] = []
        barrier = threading.Barrier(8)

        def _create(fe_id: str) -> None:
            barrier.wait()
            r = repo.create_conversation(OWNER_A, fe_id)
            created.append(r)

        threads = [
            threading.Thread(target=_create, args=(f"fe-idx-{i}",))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(created) == 8
        for record in created:
            by_id = repo.get_by_id(OWNER_A, record.conversation_id)
            by_fe = repo.get_by_frontend_id(OWNER_A, record.frontend_conversation_id)
            assert by_id is not None
            assert by_fe is not None
            assert by_id.conversation_id == record.conversation_id


# ===========================================================================
# 12. Interface / dependency isolation
# ===========================================================================


class TestInterface:

    def test_inmemory_satisfies_repository_protocol(self, repo):
        assert isinstance(repo, ConversationRepository)

    def test_no_psycopg_dependency_in_source(self):
        import inspect
        import app.services.conversation_repository as mod
        src = inspect.getsource(mod)
        assert "psycopg" not in src
        assert "sqlalchemy" not in src.lower()

    def test_no_raw_email_field_on_record(self):
        fields = set(ConversationRecord.__dataclass_fields__.keys())
        assert "email" not in fields
        assert "user_id" not in fields
        assert "databricks_user_id" not in fields
        assert "raw_email" not in fields

    def test_no_database_or_env_access_at_import(self):
        # If importing the module raises or accesses the network/DB, this
        # test will fail.  Reaching this line means the import was clean.
        import importlib
        mod = importlib.import_module("app.services.conversation_repository")
        assert mod is not None

    def test_all_exceptions_subclass_base_error(self):
        for exc_cls in (
            ConversationNotFoundError,
            ConversationAlreadyExistsError,
            ConversationOwnershipError,
            ConversationVersionConflictError,
            ConversationRepositoryError,
        ):
            assert issubclass(exc_cls, ConversationRepositoryError)

    def test_conversation_status_values_are_strings(self):
        for status in ConversationStatus:
            assert isinstance(status.value, str)

    def test_all_four_statuses_exist(self):
        assert ConversationStatus.ACTIVE
        assert ConversationStatus.STALE
        assert ConversationStatus.RESET
        assert ConversationStatus.EXPIRED
