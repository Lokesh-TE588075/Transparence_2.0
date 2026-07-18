from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest

from app.services.conversation_repository import (
    ConversationNotFoundError,
    ConversationRecord,
    ConversationRepositoryError,
    ConversationRepositoryUnavailableError,
    ConversationStatus,
    ConversationVersionConflictError,
)
from app.services.conversation_repository_factory import (
    ConversationRepositoryBackend,
    ConversationRepositoryBundle,
)
from app.services.durable_genie_session_adapter import (
    DurableGenieSessionAdapter,
    DurableGenieSessionAdapterError,
    DurableGenieSessionClosedError,
    DurableGenieSessionKey,
    DurableGenieSessionNotFoundError,
    DurableGenieSessionUnavailableError,
    DurableGenieSessionVersionConflictError,
    GenieSessionLookupResult,
    GenieSessionLookupSource,
)


UTC = timezone.utc
SENSITIVE_OWNER = "ownerhash-secret-123"
SENSITIVE_FRONTEND = "frontend-secret-456"
SENSITIVE_GENIE = "genie-secret-789"
SENSITIVE_MSG = "msg-secret-999"
SENSITIVE_HOST = "prod.internal"
SENSITIVE_ENDPOINT = "projects/p/branches/b/endpoints/e"
SENSITIVE_SQL = "SELECT * FROM app_conversation"


class GenericRepoFailure(ConversationRepositoryError):
    pass


@dataclass
class FakeInfrastructure:
    workspace_client_calls: int = 0
    pool_open_calls: int = 0
    credential_calls: int = 0
    sql_calls: int = 0
    close_calls: int = 0

    def close(self) -> None:
        self.close_calls += 1


class FakeCacheStore:
    def __init__(self) -> None:
        self.genie_ids: Dict[str, str] = {}
        self.message_ids: Dict[str, str] = {}
        self.inactive: set[str] = set()
        self.fail_on: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    def _maybe_fail(self, operation: str) -> None:
        if operation in self.fail_on:
            raise RuntimeError(
                f"cache failed host={SENSITIVE_HOST} endpoint={SENSITIVE_ENDPOINT}"
            )

    def get_genie_conversation_id(self, app_conversation_id: str) -> Optional[str]:
        self.calls.append(("get_genie_conversation_id", app_conversation_id))
        self._maybe_fail("get_genie_conversation_id")
        if app_conversation_id in self.inactive:
            return None
        return self.genie_ids.get(app_conversation_id)

    def set_genie_conversation_id(self, app_conversation_id: str, genie_conversation_id: str) -> None:
        self.calls.append(("set_genie_conversation_id", app_conversation_id))
        self._maybe_fail("set_genie_conversation_id")
        self.inactive.discard(app_conversation_id)
        self.genie_ids[app_conversation_id] = genie_conversation_id

    def get_last_message_id(self, app_conversation_id: str) -> Optional[str]:
        self.calls.append(("get_last_message_id", app_conversation_id))
        self._maybe_fail("get_last_message_id")
        if app_conversation_id in self.inactive:
            return None
        return self.message_ids.get(app_conversation_id)

    def set_last_message_id(self, app_conversation_id: str, message_id: str) -> None:
        self.calls.append(("set_last_message_id", app_conversation_id))
        self._maybe_fail("set_last_message_id")
        self.inactive.discard(app_conversation_id)
        self.message_ids[app_conversation_id] = message_id

    def reset_genie_mapping(self, app_conversation_id: str) -> None:
        self.calls.append(("reset_genie_mapping", app_conversation_id))
        self._maybe_fail("reset_genie_mapping")
        self.genie_ids.pop(app_conversation_id, None)
        self.message_ids.pop(app_conversation_id, None)
        self.inactive.discard(app_conversation_id)

    def reset_session(self, app_conversation_id: str) -> None:
        self.calls.append(("reset_session", app_conversation_id))
        self._maybe_fail("reset_session")
        self.genie_ids.pop(app_conversation_id, None)
        self.message_ids.pop(app_conversation_id, None)
        self.inactive.add(app_conversation_id)


class FakeConversationRepository:
    def __init__(self) -> None:
        self._by_id: Dict[str, ConversationRecord] = {}
        self._by_owner_frontend: Dict[tuple[str, str], str] = {}
        self.fail_on: dict[str, Exception] = {}
        self.calls: list[str] = []
        self.sequence = 0

    def _now(self, now: Optional[datetime]) -> datetime:
        return now or datetime.now(UTC)

    def _make_id(self) -> str:
        self.sequence += 1
        return f"conv-{self.sequence}"

    def _key(self, owner_user_id_hash: str, frontend_conversation_id: str) -> tuple[str, str]:
        return owner_user_id_hash, frontend_conversation_id

    def _raise_if_needed(self, operation: str) -> None:
        exc = self.fail_on.get(operation)
        if exc is not None:
            raise exc

    def _record(self, owner: str, frontend: str) -> Optional[ConversationRecord]:
        cid = self._by_owner_frontend.get((owner, frontend))
        return None if cid is None else self._by_id.get(cid)

    def _replace(self, record: ConversationRecord, **changes: Any) -> ConversationRecord:
        values = {
            "conversation_id": record.conversation_id,
            "owner_user_id_hash": record.owner_user_id_hash,
            "frontend_conversation_id": record.frontend_conversation_id,
            "genie_conversation_id": record.genie_conversation_id,
            "last_genie_message_id": record.last_genie_message_id,
            "status": record.status,
            "version": record.version,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "last_active_at": record.last_active_at,
        }
        values.update(changes)
        return ConversationRecord(**values)

    def get_by_id(self, owner_user_id_hash: str, conversation_id: str) -> Optional[ConversationRecord]:
        self.calls.append("get_by_id")
        self._raise_if_needed("get_by_id")
        record = self._by_id.get(conversation_id)
        if record is None or record.owner_user_id_hash != owner_user_id_hash:
            return None
        return record

    def get_by_frontend_id(self, owner_user_id_hash: str, frontend_conversation_id: str) -> Optional[ConversationRecord]:
        self.calls.append("get_by_frontend_id")
        self._raise_if_needed("get_by_frontend_id")
        return self._record(owner_user_id_hash, frontend_conversation_id)

    def create_conversation(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
        *,
        conversation_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        self.calls.append("create_conversation")
        self._raise_if_needed("create_conversation")
        existing = self._record(owner_user_id_hash, frontend_conversation_id)
        if existing is not None:
            return existing
        ts = self._now(now)
        record = ConversationRecord(
            conversation_id=conversation_id or self._make_id(),
            owner_user_id_hash=owner_user_id_hash,
            frontend_conversation_id=frontend_conversation_id,
            genie_conversation_id=None,
            last_genie_message_id=None,
            status=ConversationStatus.ACTIVE,
            version=1,
            created_at=ts,
            updated_at=ts,
            last_active_at=ts,
        )
        self._by_id[record.conversation_id] = record
        self._by_owner_frontend[self._key(owner_user_id_hash, frontend_conversation_id)] = record.conversation_id
        return record

    def bind_genie_conversation(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        genie_conversation_id: str,
        *,
        expected_version: int,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        self.calls.append("bind_genie_conversation")
        self._raise_if_needed("bind_genie_conversation")
        record = self.get_by_id(owner_user_id_hash, conversation_id)
        if record is None:
            raise ConversationNotFoundError(
                f"missing host={SENSITIVE_HOST} endpoint={SENSITIVE_ENDPOINT}"
            )
        if record.version != expected_version:
            raise ConversationVersionConflictError(
                f"conflict genie={SENSITIVE_GENIE} msg={SENSITIVE_MSG}"
            )
        ts = self._now(now)
        updated = self._replace(
            record,
            genie_conversation_id=genie_conversation_id,
            version=record.version + 1,
            updated_at=ts,
        )
        self._by_id[conversation_id] = updated
        return updated

    def update_last_genie_message(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        last_genie_message_id: str,
        *,
        expected_version: int,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        self.calls.append("update_last_genie_message")
        self._raise_if_needed("update_last_genie_message")
        record = self.get_by_id(owner_user_id_hash, conversation_id)
        if record is None:
            raise ConversationNotFoundError(
                f"missing sql={SENSITIVE_SQL} host={SENSITIVE_HOST}"
            )
        if record.version != expected_version:
            raise ConversationVersionConflictError(
                f"conflict message={SENSITIVE_MSG}"
            )
        ts = self._now(now)
        updated = self._replace(
            record,
            last_genie_message_id=last_genie_message_id,
            version=record.version + 1,
            updated_at=ts,
        )
        self._by_id[conversation_id] = updated
        return updated

    def touch(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        *,
        expected_version: int,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        self.calls.append("touch")
        self._raise_if_needed("touch")
        record = self.get_by_id(owner_user_id_hash, conversation_id)
        if record is None:
            raise ConversationNotFoundError("not found")
        if record.version != expected_version:
            raise ConversationVersionConflictError("conflict")
        ts = self._now(now)
        updated = self._replace(
            record,
            version=record.version + 1,
            updated_at=ts,
            last_active_at=ts,
        )
        self._by_id[conversation_id] = updated
        return updated

    def set_status(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        status: ConversationStatus,
        *,
        expected_version: int,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        self.calls.append("set_status")
        self._raise_if_needed("set_status")
        record = self.get_by_id(owner_user_id_hash, conversation_id)
        if record is None:
            raise ConversationNotFoundError("not found")
        if record.version != expected_version:
            raise ConversationVersionConflictError("conflict")
        ts = self._now(now)
        updated = self._replace(
            record,
            status=status,
            version=record.version + 1,
            updated_at=ts,
        )
        self._by_id[conversation_id] = updated
        return updated

    def compare_and_update(self, *args: Any, **kwargs: Any) -> ConversationRecord:
        self.calls.append("compare_and_update")
        self._raise_if_needed("compare_and_update")
        raise NotImplementedError

    def list_for_owner(self, owner_user_id_hash: str, *, statuses=None, limit: int = 50):
        self.calls.append("list_for_owner")
        self._raise_if_needed("list_for_owner")
        records = [
            record
            for record in self._by_id.values()
            if record.owner_user_id_hash == owner_user_id_hash
        ]
        return records[:limit]

    def delete_conversation(self, owner_user_id_hash: str, conversation_id: str) -> bool:
        self.calls.append("delete_conversation")
        self._raise_if_needed("delete_conversation")
        record = self.get_by_id(owner_user_id_hash, conversation_id)
        if record is None:
            return False
        self._by_id.pop(conversation_id, None)
        self._by_owner_frontend.pop(
            (record.owner_user_id_hash, record.frontend_conversation_id),
            None,
        )
        return True


def make_bundle(
    repo: FakeConversationRepository,
    infra: Optional[FakeInfrastructure] = None,
    *,
    backend: ConversationRepositoryBackend = ConversationRepositoryBackend.MEMORY,
    durable: bool = False,
) -> ConversationRepositoryBundle:
    infra = infra or FakeInfrastructure()
    return ConversationRepositoryBundle(
        repository=repo,
        backend=backend,
        durable=durable,
        _resource_closer=infra.close,
    )


def make_key(
    owner: str = SENSITIVE_OWNER,
    frontend: str = SENSITIVE_FRONTEND,
) -> DurableGenieSessionKey:
    return DurableGenieSessionKey(owner_user_id_hash=owner, frontend_conversation_id=frontend)


def make_adapter(
    repo: Optional[FakeConversationRepository] = None,
    cache: Optional[FakeCacheStore] = None,
    infra: Optional[FakeInfrastructure] = None,
    *,
    backend: ConversationRepositoryBackend = ConversationRepositoryBackend.MEMORY,
    durable: bool = False,
    cache_enabled: bool = True,
) -> tuple[DurableGenieSessionAdapter, FakeConversationRepository, FakeCacheStore | None, FakeInfrastructure]:
    repo = repo or FakeConversationRepository()
    infra = infra or FakeInfrastructure()
    bundle = make_bundle(repo, infra, backend=backend, durable=durable)
    adapter = DurableGenieSessionAdapter(bundle, cache_store=cache, cache_enabled=cache_enabled)
    return adapter, repo, cache, infra


def test_construction_performs_no_repository_access() -> None:
    adapter, repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    assert repo.calls == []
    assert adapter is not None


def test_construction_opens_no_pool() -> None:
    _adapter, _repo, _cache, infra = make_adapter(cache=FakeCacheStore())
    assert infra.pool_open_calls == 0


def test_construction_generates_no_credential() -> None:
    _adapter, _repo, _cache, infra = make_adapter(cache=FakeCacheStore())
    assert infra.credential_calls == 0


def test_construction_performs_no_sql() -> None:
    _adapter, _repo, _cache, infra = make_adapter(cache=FakeCacheStore())
    assert infra.sql_calls == 0


def test_close_closes_bundle_once() -> None:
    adapter, _repo, _cache, infra = make_adapter(cache=FakeCacheStore())
    adapter.close()
    assert infra.close_calls == 1


def test_repeated_close_is_safe() -> None:
    adapter, _repo, _cache, infra = make_adapter(cache=FakeCacheStore())
    adapter.close()
    adapter.close()
    assert infra.close_calls == 1


def test_use_after_close_rejected() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    adapter.close()
    with pytest.raises(DurableGenieSessionClosedError):
        adapter.load(make_key())


def test_context_manager_closes_bundle() -> None:
    repo = FakeConversationRepository()
    infra = FakeInfrastructure()
    cache = FakeCacheStore()
    with DurableGenieSessionAdapter(make_bundle(repo, infra), cache_store=cache) as adapter:
        assert isinstance(adapter, DurableGenieSessionAdapter)
    assert infra.close_calls == 1


def test_repr_hides_sensitive_identifiers() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    text = repr(adapter)
    assert SENSITIVE_OWNER not in text
    assert SENSITIVE_FRONTEND not in text
    assert SENSITIVE_GENIE not in text


def test_valid_key() -> None:
    key = make_key("  abc123  ", "  conv-1  ")
    assert key.owner_user_id_hash == "abc123"
    assert key.frontend_conversation_id == "conv-1"


def test_empty_owner_rejected() -> None:
    with pytest.raises(ValueError):
        DurableGenieSessionKey(owner_user_id_hash="", frontend_conversation_id="x")


def test_empty_frontend_id_rejected() -> None:
    with pytest.raises(ValueError):
        DurableGenieSessionKey(owner_user_id_hash="abc", frontend_conversation_id="")


def test_whitespace_rejected() -> None:
    with pytest.raises(ValueError):
        DurableGenieSessionKey(owner_user_id_hash="   ", frontend_conversation_id="x")
    with pytest.raises(ValueError):
        DurableGenieSessionKey(owner_user_id_hash="abc", frontend_conversation_id="   ")


def test_cache_key_deterministic() -> None:
    key_a = make_key()
    key_b = make_key()
    assert key_a.to_internal_cache_key() == key_b.to_internal_cache_key()


def test_key_repr_hides_values() -> None:
    text = repr(make_key())
    assert SENSITIVE_OWNER not in text
    assert SENSITIVE_FRONTEND not in text


def test_get_or_create_returns_new_durable_record() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    result = adapter.get_or_create(make_key())
    assert result.record.frontend_conversation_id == SENSITIVE_FRONTEND
    assert result.record.owner_user_id_hash == SENSITIVE_OWNER


def test_get_or_create_returns_existing_durable_record_idempotently() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    first = adapter.get_or_create(make_key())
    second = adapter.get_or_create(make_key())
    assert second.record.conversation_id == first.record.conversation_id
    assert second.record.version == 1


def test_get_or_create_source_repository() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    result = adapter.get_or_create(make_key())
    assert result.source is GenieSessionLookupSource.REPOSITORY


def test_get_or_create_degraded_false() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    result = adapter.get_or_create(make_key())
    assert result.degraded is False


def test_get_or_create_refreshes_cache_after_success() -> None:
    adapter, _repo, cache, _infra = make_adapter(cache=FakeCacheStore())
    result = adapter.get_or_create(make_key())
    cache_key = make_key().to_internal_cache_key()
    assert cache is not None
    assert ("reset_genie_mapping", cache_key) in cache.calls
    assert result.record.conversation_id.startswith("conv-")


def test_get_or_create_cache_failure_does_not_fail_durable_success() -> None:
    cache = FakeCacheStore()
    cache.fail_on.add("reset_genie_mapping")
    adapter, repo, _cache, _infra = make_adapter(cache=cache)
    result = adapter.get_or_create(make_key())
    repo.fail_on["create_conversation"] = ConversationRepositoryUnavailableError("down")
    fallback = adapter.get_or_create(make_key())
    assert result.record.conversation_id == fallback.record.conversation_id
    assert fallback.degraded is True


def test_get_or_create_repository_unavailable_with_confirmed_snapshot_returns_degraded_result() -> None:
    adapter, repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    success = adapter.get_or_create(make_key())
    repo.fail_on["create_conversation"] = ConversationRepositoryUnavailableError("down")
    fallback = adapter.get_or_create(make_key())
    assert fallback.source is GenieSessionLookupSource.CACHE
    assert fallback.degraded is True
    assert fallback.record == success.record


def test_get_or_create_repository_unavailable_without_snapshot_raises() -> None:
    adapter, repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    repo.fail_on["create_conversation"] = ConversationRepositoryUnavailableError("down")
    with pytest.raises(DurableGenieSessionUnavailableError):
        adapter.get_or_create(make_key())


def test_get_or_create_no_authoritative_cache_only_create() -> None:
    cache = FakeCacheStore()
    cache.genie_ids[make_key().to_internal_cache_key()] = SENSITIVE_GENIE
    adapter, repo, _cache, _infra = make_adapter(cache=cache)
    repo.fail_on["create_conversation"] = ConversationRepositoryUnavailableError("down")
    with pytest.raises(DurableGenieSessionUnavailableError):
        adapter.get_or_create(make_key())


def test_get_or_create_owner_isolation_preserved() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    result_a = adapter.get_or_create(make_key(owner="owner-a", frontend="same"))
    result_b = adapter.get_or_create(make_key(owner="owner-b", frontend="same"))
    assert result_a.record.conversation_id != result_b.record.conversation_id


def test_load_returns_repository_record() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    created = adapter.get_or_create(make_key())
    loaded = adapter.load(make_key())
    assert loaded is not None
    assert loaded.record == created.record


def test_load_missing_returns_none() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    assert adapter.load(make_key()) is None


def test_load_repository_result_refreshes_snapshot() -> None:
    adapter, repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    created = adapter.get_or_create(make_key())
    repo.fail_on["get_by_frontend_id"] = ConversationRepositoryUnavailableError("down")
    fallback = adapter.load(make_key())
    assert fallback is not None
    assert fallback.record == created.record


def test_load_unavailable_with_snapshot_falls_back() -> None:
    adapter, repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    adapter.get_or_create(make_key())
    repo.fail_on["get_by_frontend_id"] = ConversationRepositoryUnavailableError("down")
    result = adapter.load(make_key())
    assert result is not None
    assert result.source is GenieSessionLookupSource.CACHE


def test_load_unavailable_without_snapshot_raises() -> None:
    adapter, repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    repo.fail_on["get_by_frontend_id"] = ConversationRepositoryUnavailableError("down")
    with pytest.raises(DurableGenieSessionUnavailableError):
        adapter.load(make_key())


def test_load_cross_owner_result_not_leaked() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    adapter.get_or_create(make_key(owner="owner-a", frontend="same"))
    assert adapter.load(make_key(owner="owner-b", frontend="same")) is None


def test_bind_genie_conversation_durable_bind_succeeds() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    created = adapter.get_or_create(make_key())
    updated = adapter.bind_genie_conversation(
        make_key(),
        SENSITIVE_GENIE,
        expected_version=created.record.version,
    )
    assert updated.genie_conversation_id == SENSITIVE_GENIE


def test_bind_genie_conversation_cache_updated_after_durable_success() -> None:
    cache = FakeCacheStore()
    adapter, _repo, _cache, _infra = make_adapter(cache=cache)
    created = adapter.get_or_create(make_key())
    updated = adapter.bind_genie_conversation(make_key(), SENSITIVE_GENIE, expected_version=created.record.version)
    cache_key = make_key().to_internal_cache_key()
    assert cache.genie_ids[cache_key] == SENSITIVE_GENIE
    assert updated.version == 2


def test_bind_genie_conversation_empty_id_rejected_before_repository_call() -> None:
    adapter, repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    adapter.get_or_create(make_key())
    with pytest.raises(ValueError):
        adapter.bind_genie_conversation(make_key(), "   ", expected_version=1)
    assert "bind_genie_conversation" not in repo.calls


def test_bind_genie_conversation_version_conflict_translated() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    created = adapter.get_or_create(make_key())
    with pytest.raises(DurableGenieSessionVersionConflictError):
        adapter.bind_genie_conversation(make_key(), SENSITIVE_GENIE, expected_version=created.record.version + 1)


def test_bind_genie_conversation_not_found_translated() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    with pytest.raises(DurableGenieSessionNotFoundError):
        adapter.bind_genie_conversation(make_key(), SENSITIVE_GENIE, expected_version=1)


def test_bind_genie_conversation_repository_unavailable_translated() -> None:
    adapter, repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    adapter.get_or_create(make_key())
    repo.fail_on["get_by_frontend_id"] = ConversationRepositoryUnavailableError("down")
    with pytest.raises(DurableGenieSessionUnavailableError):
        adapter.bind_genie_conversation(make_key(), SENSITIVE_GENIE, expected_version=1)


def test_bind_genie_conversation_cache_unchanged_when_durable_write_fails() -> None:
    cache = FakeCacheStore()
    adapter, _repo, _cache, _infra = make_adapter(cache=cache)
    created = adapter.get_or_create(make_key())
    with pytest.raises(DurableGenieSessionVersionConflictError):
        adapter.bind_genie_conversation(make_key(), SENSITIVE_GENIE, expected_version=created.record.version + 1)
    cache_key = make_key().to_internal_cache_key()
    assert cache.genie_ids.get(cache_key) is None


def test_bind_genie_conversation_version_increments_once() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    created = adapter.get_or_create(make_key())
    updated = adapter.bind_genie_conversation(make_key(), SENSITIVE_GENIE, expected_version=created.record.version)
    assert updated.version == created.record.version + 1


def test_update_last_genie_message_success() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    created = adapter.get_or_create(make_key())
    updated = adapter.update_last_genie_message(make_key(), SENSITIVE_MSG, expected_version=created.record.version)
    assert updated.last_genie_message_id == SENSITIVE_MSG


def test_update_last_genie_message_empty_message_rejected() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    adapter.get_or_create(make_key())
    with pytest.raises(ValueError):
        adapter.update_last_genie_message(make_key(), "", expected_version=1)


def test_update_last_genie_message_conflict_translated() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    created = adapter.get_or_create(make_key())
    with pytest.raises(DurableGenieSessionVersionConflictError):
        adapter.update_last_genie_message(make_key(), SENSITIVE_MSG, expected_version=created.record.version + 1)


def test_update_last_genie_message_cache_after_durable_order_enforced() -> None:
    cache = FakeCacheStore()
    cache.fail_on.add("reset_genie_mapping")
    adapter, repo, _cache, _infra = make_adapter(cache=cache)
    created = adapter.get_or_create(make_key())
    updated = adapter.update_last_genie_message(make_key(), SENSITIVE_MSG, expected_version=created.record.version)
    repo.fail_on["get_by_frontend_id"] = ConversationRepositoryUnavailableError("down")
    fallback = adapter.load(make_key())
    assert updated.last_genie_message_id == SENSITIVE_MSG
    assert fallback is not None
    assert fallback.record.last_genie_message_id == SENSITIVE_MSG


def test_touch_succeeds() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    base = datetime.now(UTC)
    created = adapter.get_or_create(make_key(), now=base)
    touched = adapter.touch(make_key(), expected_version=created.record.version, now=base + timedelta(seconds=1))
    assert touched.last_active_at > created.record.last_active_at


def test_touch_conflict_surfaced() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    created = adapter.get_or_create(make_key())
    with pytest.raises(DurableGenieSessionVersionConflictError):
        adapter.touch(make_key(), expected_version=created.record.version + 1)


@pytest.mark.parametrize(
    "status",
    [
        ConversationStatus.ACTIVE,
        ConversationStatus.STALE,
        ConversationStatus.RESET,
        ConversationStatus.EXPIRED,
    ],
)
def test_each_status_supported(status: ConversationStatus) -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    created = adapter.get_or_create(make_key())
    updated = adapter.set_status(make_key(), status, expected_version=created.record.version)
    assert updated.status is status


def test_cache_refreshed_after_status_success() -> None:
    cache = FakeCacheStore()
    adapter, _repo, _cache, _infra = make_adapter(cache=cache)
    created = adapter.get_or_create(make_key())
    bound = adapter.bind_genie_conversation(make_key(), SENSITIVE_GENIE, expected_version=created.record.version)
    cache.calls.clear()
    adapter.set_status(make_key(), ConversationStatus.STALE, expected_version=bound.version)
    assert cache.calls[0][0] == "reset_genie_mapping"


def test_delete_durable_delete_true() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    adapter.get_or_create(make_key())
    assert adapter.delete(make_key()) is True


def test_delete_missing_returns_false() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    assert adapter.delete(make_key()) is False


def test_delete_clears_cache_after_durable_delete() -> None:
    cache = FakeCacheStore()
    adapter, _repo, _cache, _infra = make_adapter(cache=cache)
    created = adapter.get_or_create(make_key())
    adapter.bind_genie_conversation(make_key(), SENSITIVE_GENIE, expected_version=created.record.version)
    adapter.delete(make_key())
    cache_key = make_key().to_internal_cache_key()
    assert cache_key in cache.inactive
    assert cache.genie_ids.get(cache_key) is None


def test_delete_durable_unavailable_raises() -> None:
    adapter, repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    adapter.get_or_create(make_key())
    repo.fail_on["get_by_frontend_id"] = ConversationRepositoryUnavailableError("down")
    with pytest.raises(DurableGenieSessionUnavailableError):
        adapter.delete(make_key())


def test_delete_cache_clear_failure_does_not_reverse_durable_delete() -> None:
    cache = FakeCacheStore()
    adapter, repo, _cache, _infra = make_adapter(cache=cache)
    adapter.get_or_create(make_key())
    cache.fail_on.add("reset_session")
    assert adapter.delete(make_key()) is True
    assert repo.get_by_frontend_id(SENSITIVE_OWNER, SENSITIVE_FRONTEND) is None


def test_deleted_snapshot_cannot_be_used_for_fallback() -> None:
    adapter, repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    adapter.get_or_create(make_key())
    assert adapter.delete(make_key()) is True
    repo.fail_on["get_by_frontend_id"] = ConversationRepositoryUnavailableError("down")
    with pytest.raises(DurableGenieSessionUnavailableError):
        adapter.load(make_key())


def test_only_repository_confirmed_snapshots_eligible() -> None:
    cache = FakeCacheStore()
    cache.genie_ids[make_key().to_internal_cache_key()] = SENSITIVE_GENIE
    adapter, repo, _cache, _infra = make_adapter(cache=cache)
    repo.fail_on["get_by_frontend_id"] = ConversationRepositoryUnavailableError("down")
    with pytest.raises(DurableGenieSessionUnavailableError):
        adapter.load(make_key())


def test_fallback_marked_degraded() -> None:
    adapter, repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    adapter.get_or_create(make_key())
    repo.fail_on["get_by_frontend_id"] = ConversationRepositoryUnavailableError("down")
    result = adapter.load(make_key())
    assert result is not None
    assert result.degraded is True


def test_fallback_source_cache() -> None:
    adapter, repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    adapter.get_or_create(make_key())
    repo.fail_on["get_by_frontend_id"] = ConversationRepositoryUnavailableError("down")
    result = adapter.load(make_key())
    assert result is not None
    assert result.source is GenieSessionLookupSource.CACHE


def test_no_fallback_across_owners() -> None:
    adapter, repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    adapter.get_or_create(make_key(owner="owner-a", frontend="same"))
    repo.fail_on["get_by_frontend_id"] = ConversationRepositoryUnavailableError("down")
    with pytest.raises(DurableGenieSessionUnavailableError):
        adapter.load(make_key(owner="owner-b", frontend="same"))


def test_fallback_does_not_mutate_version() -> None:
    adapter, repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    created = adapter.get_or_create(make_key())
    repo.fail_on["get_by_frontend_id"] = ConversationRepositoryUnavailableError("down")
    fallback = adapter.load(make_key())
    assert fallback is not None
    assert fallback.record.version == created.record.version


def test_fallback_unavailable_after_close() -> None:
    adapter, repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    adapter.get_or_create(make_key())
    adapter.close()
    repo.fail_on["get_by_frontend_id"] = ConversationRepositoryUnavailableError("down")
    with pytest.raises(DurableGenieSessionClosedError):
        adapter.load(make_key())


def test_errors_contain_no_owner_hash() -> None:
    adapter, repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    repo.fail_on["create_conversation"] = ConversationRepositoryUnavailableError(
        f"owner={SENSITIVE_OWNER}"
    )
    with pytest.raises(DurableGenieSessionUnavailableError) as excinfo:
        adapter.get_or_create(make_key())
    assert SENSITIVE_OWNER not in str(excinfo.value)


def test_errors_contain_no_genie_id() -> None:
    adapter, _repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    created = adapter.get_or_create(make_key())
    with pytest.raises(DurableGenieSessionVersionConflictError) as excinfo:
        adapter.bind_genie_conversation(make_key(), SENSITIVE_GENIE, expected_version=created.record.version + 1)
    assert SENSITIVE_GENIE not in str(excinfo.value)


def test_errors_contain_no_sql_host_or_endpoint() -> None:
    adapter, repo, _cache, _infra = make_adapter(cache=FakeCacheStore())
    repo.fail_on["create_conversation"] = GenericRepoFailure(
        f"host={SENSITIVE_HOST} endpoint={SENSITIVE_ENDPOINT} sql={SENSITIVE_SQL}"
    )
    with pytest.raises(DurableGenieSessionAdapterError) as excinfo:
        adapter.get_or_create(make_key())
    text = str(excinfo.value)
    assert SENSITIVE_HOST not in text
    assert SENSITIVE_ENDPOINT not in text
    assert SENSITIVE_SQL not in text


def test_no_real_workspace_client() -> None:
    _adapter, _repo, _cache, infra = make_adapter(cache=FakeCacheStore())
    assert infra.workspace_client_calls == 0


def test_no_real_pool() -> None:
    _adapter, _repo, _cache, infra = make_adapter(cache=FakeCacheStore())
    assert infra.pool_open_calls == 0


def test_no_sql_execution() -> None:
    _adapter, _repo, _cache, infra = make_adapter(cache=FakeCacheStore())
    assert infra.sql_calls == 0


def test_adapter_not_imported_by_existing_runtime_modules() -> None:
    # Only this one approved Phase 3C module is permitted to import the adapter.
    # All other application modules must not import durable_genie_session_adapter.
    _APPROVED_IMPORTER = "app/services/durable_genie_session_runtime_factory.py"
    app_root = pathlib.Path("app")
    offenders = []
    for path in app_root.rglob("*.py"):
        if path.as_posix() == "app/services/durable_genie_session_adapter.py":
            continue
        if path.as_posix() == _APPROVED_IMPORTER:
            continue
        text = path.read_text(encoding="utf-8")
        if "durable_genie_session_adapter" in text:
            offenders.append(path.as_posix())
    assert offenders == []


def test_genie_session_store_source_unchanged() -> None:
    text = pathlib.Path("app/services/genie_session_store.py").read_text(encoding="utf-8")
    assert "DurableGenieSessionAdapter" not in text
    assert "durable_genie_session_adapter" not in text


def test_adapter_works_with_memory_repository_bundle() -> None:
    adapter, _repo, _cache, _infra = make_adapter(
        cache=FakeCacheStore(),
        backend=ConversationRepositoryBackend.MEMORY,
        durable=False,
    )
    result = adapter.get_or_create(make_key())
    assert result.source is GenieSessionLookupSource.REPOSITORY


def test_adapter_works_with_mocked_durable_repository_bundle() -> None:
    adapter, _repo, _cache, _infra = make_adapter(
        cache=FakeCacheStore(),
        backend=ConversationRepositoryBackend.LAKEBASE,
        durable=True,
    )
    result = adapter.get_or_create(make_key())
    assert result.record.status is ConversationStatus.ACTIVE


def test_lookup_result_repr_hides_sensitive_fields() -> None:
    record = ConversationRecord(
        conversation_id="conv-1",
        owner_user_id_hash=SENSITIVE_OWNER,
        frontend_conversation_id=SENSITIVE_FRONTEND,
        genie_conversation_id=SENSITIVE_GENIE,
        last_genie_message_id=SENSITIVE_MSG,
        status=ConversationStatus.ACTIVE,
        version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        last_active_at=datetime.now(UTC),
    )
    result = GenieSessionLookupResult(record=record, source=GenieSessionLookupSource.REPOSITORY, degraded=False)
    text = repr(result)
    assert SENSITIVE_OWNER not in text
    assert SENSITIVE_FRONTEND not in text
    assert SENSITIVE_GENIE not in text
    assert SENSITIVE_MSG not in text


def test_adapter_source_parses_and_has_no_merge_markers() -> None:
    path = pathlib.Path("app/services/durable_genie_session_adapter.py")
    source = path.read_text(encoding="utf-8")
    assert "<<<<<<<" not in source
    assert ">>>>>>>" not in source
    ast.parse(source)


def test_adapter_test_source_parses() -> None:
    path = pathlib.Path("tests/test_durable_genie_session_adapter.py")
    source = path.read_text(encoding="utf-8")
    ast.parse(source)
