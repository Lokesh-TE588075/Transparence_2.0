"""Durable adapter between conversation repository and GenieSessionStore.

This module introduces a standalone adapter that keeps the conversation
repository as the single authority for conversation existence, ownership,
versioning, status, timestamps, Genie conversation binding, and last-message
tracking.

The optional injected GenieSessionStore is treated only as a best-effort
compatibility cache.  Degraded fallback reads are served exclusively from an
adapter-local cache of previously repository-confirmed ConversationRecord
snapshots.  No runtime wiring is performed here.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from app.services.conversation_repository import (
    ConversationNotFoundError,
    ConversationRecord,
    ConversationRepositoryError,
    ConversationRepositoryUnavailableError,
    ConversationStatus,
    ConversationVersionConflictError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, repr=False)
class DurableGenieSessionKey:
    """Owner-scoped logical key for a durable Genie session."""

    owner_user_id_hash: str
    frontend_conversation_id: str

    def __post_init__(self) -> None:
        owner = self.owner_user_id_hash.strip()
        frontend = self.frontend_conversation_id.strip()
        if not owner:
            raise ValueError("owner_user_id_hash must not be empty")
        if not frontend:
            raise ValueError("frontend_conversation_id must not be empty")
        if "@" in owner:
            raise ValueError("owner_user_id_hash must be a derived identifier")
        if "@" in frontend:
            raise ValueError("frontend_conversation_id must not be a raw identity value")
        object.__setattr__(self, "owner_user_id_hash", owner)
        object.__setattr__(self, "frontend_conversation_id", frontend)

    def to_internal_cache_key(self) -> str:
        payload = (
            f"{self.owner_user_id_hash}\x1f{self.frontend_conversation_id}"
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        return f"durable-genie:{digest}"

    def __repr__(self) -> str:
        return "DurableGenieSessionKey(owner_user_id_hash=<redacted>, frontend_conversation_id=<redacted>)"


class GenieSessionLookupSource(str, Enum):
    REPOSITORY = "REPOSITORY"
    CACHE = "CACHE"


@dataclass(frozen=True, repr=False)
class GenieSessionLookupResult:
    record: ConversationRecord
    source: GenieSessionLookupSource
    degraded: bool

    def __repr__(self) -> str:
        return (
            "GenieSessionLookupResult("
            f"conversation_id={self.record.conversation_id!r}, "
            f"status={self.record.status.value!r}, "
            f"version={self.record.version!r}, "
            f"source={self.source.value!r}, "
            f"degraded={self.degraded!r}"
            ")"
        )


class DurableGenieSessionAdapterError(Exception):
    """Base exception for adapter failures."""


class DurableGenieSessionUnavailableError(DurableGenieSessionAdapterError):
    """Raised when the authoritative repository is unavailable."""


class DurableGenieSessionVersionConflictError(DurableGenieSessionAdapterError):
    """Raised on optimistic-concurrency conflicts."""


class DurableGenieSessionNotFoundError(DurableGenieSessionAdapterError):
    """Raised when the owner-scoped durable conversation record is absent."""


class DurableGenieSessionCacheError(DurableGenieSessionAdapterError):
    """Raised for compatibility-cache bridge failures."""


class DurableGenieSessionClosedError(DurableGenieSessionAdapterError):
    """Raised when the adapter is used after close()."""


class _GenieSessionCompatibilityCache:
    """Narrow bridge over the confirmed public GenieSessionStore API."""

    def __init__(self, cache_store: Any) -> None:
        self._cache_store = cache_store

    def get_genie_conversation_id(self, cache_key: str) -> Optional[str]:
        try:
            return self._cache_store.get_genie_conversation_id(cache_key)
        except Exception as exc:  # pragma: no cover - exercised via adapter behavior
            raise DurableGenieSessionCacheError(
                "Genie session compatibility cache is unavailable."
            ) from exc

    def get_last_message_id(self, cache_key: str) -> Optional[str]:
        try:
            return self._cache_store.get_last_message_id(cache_key)
        except Exception as exc:  # pragma: no cover - exercised via adapter behavior
            raise DurableGenieSessionCacheError(
                "Genie session compatibility cache is unavailable."
            ) from exc

    def set_genie_conversation_id(self, cache_key: str, genie_conversation_id: str) -> None:
        try:
            self._cache_store.set_genie_conversation_id(cache_key, genie_conversation_id)
        except Exception as exc:
            raise DurableGenieSessionCacheError(
                "Genie session compatibility cache write failed."
            ) from exc

    def set_last_message_id(self, cache_key: str, last_genie_message_id: str) -> None:
        try:
            self._cache_store.set_last_message_id(cache_key, last_genie_message_id)
        except Exception as exc:
            raise DurableGenieSessionCacheError(
                "Genie session compatibility cache write failed."
            ) from exc

    def reset_genie_mapping(self, cache_key: str) -> None:
        try:
            self._cache_store.reset_genie_mapping(cache_key)
        except Exception as exc:
            raise DurableGenieSessionCacheError(
                "Genie session compatibility cache write failed."
            ) from exc

    def reset_session(self, cache_key: str) -> None:
        try:
            self._cache_store.reset_session(cache_key)
        except Exception as exc:
            raise DurableGenieSessionCacheError(
                "Genie session compatibility cache write failed."
            ) from exc


class DurableGenieSessionAdapter:
    """Repository-authoritative durable session adapter."""

    def __init__(
        self,
        repository_bundle: Any,
        cache_store: Any | None = None,
        cache_enabled: bool = True,
    ) -> None:
        self._repository_bundle = repository_bundle
        self._repository = repository_bundle.repository
        self._cache_enabled = bool(cache_enabled)
        self._cache = (
            _GenieSessionCompatibilityCache(cache_store)
            if self._cache_enabled and cache_store is not None
            else None
        )
        self._lock = threading.RLock()
        self._confirmed_snapshots: dict[str, ConversationRecord] = {}
        self._closed = False

    def __repr__(self) -> str:
        return (
            "DurableGenieSessionAdapter("
            f"backend={self._repository_bundle.backend.value!r}, "
            f"durable={self._repository_bundle.durable!r}, "
            f"cache_enabled={self._cache_enabled!r}, "
            f"has_cache_store={self._cache is not None!r}, "
            f"closed={self._closed!r}"
            ")"
        )

    def __enter__(self) -> "DurableGenieSessionAdapter":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._repository_bundle.close()

    def get_or_create(
        self,
        key: DurableGenieSessionKey,
        *,
        now: Optional[datetime] = None,
    ) -> GenieSessionLookupResult:
        self._ensure_open()
        try:
            record = self._repository.create_conversation(
                key.owner_user_id_hash,
                key.frontend_conversation_id,
                now=now,
            )
        except ConversationRepositoryUnavailableError as exc:
            snapshot = self._get_confirmed_snapshot(key)
            if snapshot is not None:
                return GenieSessionLookupResult(
                    record=snapshot,
                    source=GenieSessionLookupSource.CACHE,
                    degraded=True,
                )
            raise DurableGenieSessionUnavailableError(
                "Durable Genie session state is temporarily unavailable."
            ) from exc
        except ConversationRepositoryError as exc:
            self._raise_translated_repository_error(exc)

        self._store_confirmed_snapshot(key, record)
        self._refresh_compatibility_cache(key, record)
        return GenieSessionLookupResult(
            record=record,
            source=GenieSessionLookupSource.REPOSITORY,
            degraded=False,
        )

    def load(
        self,
        key: DurableGenieSessionKey,
    ) -> Optional[GenieSessionLookupResult]:
        self._ensure_open()
        try:
            record = self._repository.get_by_frontend_id(
                key.owner_user_id_hash,
                key.frontend_conversation_id,
            )
        except ConversationRepositoryUnavailableError as exc:
            snapshot = self._get_confirmed_snapshot(key)
            if snapshot is not None:
                return GenieSessionLookupResult(
                    record=snapshot,
                    source=GenieSessionLookupSource.CACHE,
                    degraded=True,
                )
            raise DurableGenieSessionUnavailableError(
                "Durable Genie session state is temporarily unavailable."
            ) from exc
        except ConversationRepositoryError as exc:
            self._raise_translated_repository_error(exc)

        if record is None:
            self._remove_confirmed_snapshot(key)
            return None

        self._store_confirmed_snapshot(key, record)
        self._refresh_compatibility_cache(key, record)
        return GenieSessionLookupResult(
            record=record,
            source=GenieSessionLookupSource.REPOSITORY,
            degraded=False,
        )

    def bind_genie_conversation(
        self,
        key: DurableGenieSessionKey,
        genie_conversation_id: str,
        *,
        expected_version: int,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        genie_conversation_id = self._require_non_empty_value(
            genie_conversation_id,
            "genie_conversation_id",
        )
        current = self._require_existing_record(key)
        try:
            record = self._repository.bind_genie_conversation(
                key.owner_user_id_hash,
                current.conversation_id,
                genie_conversation_id,
                expected_version=expected_version,
                now=now,
            )
        except ConversationRepositoryError as exc:
            self._raise_translated_repository_error(exc)

        self._store_confirmed_snapshot(key, record)
        self._refresh_compatibility_cache(key, record)
        return record

    def update_last_genie_message(
        self,
        key: DurableGenieSessionKey,
        last_genie_message_id: str,
        *,
        expected_version: int,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        last_genie_message_id = self._require_non_empty_value(
            last_genie_message_id,
            "last_genie_message_id",
        )
        current = self._require_existing_record(key)
        try:
            record = self._repository.update_last_genie_message(
                key.owner_user_id_hash,
                current.conversation_id,
                last_genie_message_id,
                expected_version=expected_version,
                now=now,
            )
        except ConversationRepositoryError as exc:
            self._raise_translated_repository_error(exc)

        self._store_confirmed_snapshot(key, record)
        self._refresh_compatibility_cache(key, record)
        return record

    def touch(
        self,
        key: DurableGenieSessionKey,
        *,
        expected_version: int,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        current = self._require_existing_record(key)
        try:
            record = self._repository.touch(
                key.owner_user_id_hash,
                current.conversation_id,
                expected_version=expected_version,
                now=now,
            )
        except ConversationRepositoryError as exc:
            self._raise_translated_repository_error(exc)

        self._store_confirmed_snapshot(key, record)
        self._refresh_compatibility_cache(key, record)
        return record

    def set_status(
        self,
        key: DurableGenieSessionKey,
        status: ConversationStatus,
        *,
        expected_version: int,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        current = self._require_existing_record(key)
        try:
            record = self._repository.set_status(
                key.owner_user_id_hash,
                current.conversation_id,
                status,
                expected_version=expected_version,
                now=now,
            )
        except ConversationRepositoryError as exc:
            self._raise_translated_repository_error(exc)

        self._store_confirmed_snapshot(key, record)
        self._refresh_compatibility_cache(key, record)
        return record

    def delete(self, key: DurableGenieSessionKey) -> bool:
        self._ensure_open()
        try:
            current = self._repository.get_by_frontend_id(
                key.owner_user_id_hash,
                key.frontend_conversation_id,
            )
        except ConversationRepositoryUnavailableError as exc:
            raise DurableGenieSessionUnavailableError(
                "Durable Genie session state is temporarily unavailable."
            ) from exc
        except ConversationRepositoryError as exc:
            self._raise_translated_repository_error(exc)

        if current is None:
            self._remove_confirmed_snapshot(key)
            self._clear_compatibility_cache_after_delete(key)
            return False

        try:
            deleted = self._repository.delete_conversation(
                key.owner_user_id_hash,
                current.conversation_id,
            )
        except ConversationRepositoryUnavailableError as exc:
            raise DurableGenieSessionUnavailableError(
                "Durable Genie session state is temporarily unavailable."
            ) from exc
        except ConversationRepositoryError as exc:
            self._raise_translated_repository_error(exc)

        if deleted:
            self._remove_confirmed_snapshot(key)
            self._clear_compatibility_cache_after_delete(key)
            return True

        self._remove_confirmed_snapshot(key)
        self._clear_compatibility_cache_after_delete(key)
        return False

    def _ensure_open(self) -> None:
        with self._lock:
            if self._closed:
                raise DurableGenieSessionClosedError(
                    "Durable Genie session adapter is closed."
                )

    def _require_non_empty_value(self, value: str, name: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{name} must not be empty")
        return normalized

    def _require_existing_record(self, key: DurableGenieSessionKey) -> ConversationRecord:
        self._ensure_open()
        try:
            record = self._repository.get_by_frontend_id(
                key.owner_user_id_hash,
                key.frontend_conversation_id,
            )
        except ConversationRepositoryUnavailableError as exc:
            raise DurableGenieSessionUnavailableError(
                "Durable Genie session state is temporarily unavailable."
            ) from exc
        except ConversationRepositoryError as exc:
            self._raise_translated_repository_error(exc)

        if record is None:
            self._remove_confirmed_snapshot(key)
            raise DurableGenieSessionNotFoundError(
                "Durable Genie session was not found."
            )
        return record

    def _store_confirmed_snapshot(
        self,
        key: DurableGenieSessionKey,
        record: ConversationRecord,
    ) -> None:
        with self._lock:
            self._confirmed_snapshots[key.to_internal_cache_key()] = record

    def _get_confirmed_snapshot(
        self,
        key: DurableGenieSessionKey,
    ) -> Optional[ConversationRecord]:
        with self._lock:
            if self._closed:
                return None
            return self._confirmed_snapshots.get(key.to_internal_cache_key())

    def _remove_confirmed_snapshot(self, key: DurableGenieSessionKey) -> None:
        with self._lock:
            self._confirmed_snapshots.pop(key.to_internal_cache_key(), None)

    def _refresh_compatibility_cache(
        self,
        key: DurableGenieSessionKey,
        record: ConversationRecord,
    ) -> None:
        if self._cache is None:
            return
        cache_key = key.to_internal_cache_key()
        try:
            self._cache.reset_genie_mapping(cache_key)
            if record.genie_conversation_id is not None:
                self._cache.set_genie_conversation_id(
                    cache_key,
                    record.genie_conversation_id,
                )
            if record.last_genie_message_id is not None:
                self._cache.set_last_message_id(
                    cache_key,
                    record.last_genie_message_id,
                )
        except DurableGenieSessionCacheError:
            logger.warning(
                "Durable Genie session compatibility cache refresh failed."
            )

    def _clear_compatibility_cache_after_delete(
        self,
        key: DurableGenieSessionKey,
    ) -> None:
        if self._cache is None:
            return
        try:
            self._cache.reset_session(key.to_internal_cache_key())
        except DurableGenieSessionCacheError:
            logger.warning(
                "Durable Genie session compatibility cache clear failed."
            )

    def _raise_translated_repository_error(self, exc: ConversationRepositoryError) -> None:
        if isinstance(exc, ConversationRepositoryUnavailableError):
            raise DurableGenieSessionUnavailableError(
                "Durable Genie session state is temporarily unavailable."
            ) from exc
        if isinstance(exc, ConversationVersionConflictError):
            raise DurableGenieSessionVersionConflictError(
                "Durable Genie session version conflict."
            ) from exc
        if isinstance(exc, ConversationNotFoundError):
            raise DurableGenieSessionNotFoundError(
                "Durable Genie session was not found."
            ) from exc
        raise DurableGenieSessionAdapterError(
            "Durable Genie session operation failed."
        ) from exc
