"""Message persistence repository contract and in-memory reference implementation.

This module defines:

  - MessageRecord           -- immutable domain model for a single stored message
  - MessageRepository       -- runtime-checkable Protocol / abstract interface
  - Repository exceptions   -- MessageRepositoryError hierarchy
  - InMemoryMessageRepository -- thread-safe reference implementation for tests

Purpose
=======
Persists owner-scoped conversation messages so that browser refresh and tab
reopen can rehydrate the chat UI from the backend rather than from localStorage.

Security contract
=================
  - owner_user_id_hash is a pre-derived HMAC-SHA256 digest of the Databricks
    user email.  Raw email addresses are never stored here.
  - response_payload_json MUST NOT contain: owner hashes, Genie conversation
    IDs, Genie message IDs, process-local keys, SQL credentials, download keys,
    export IDs, or raw exception stack traces.
  - message_text is capped at MESSAGE_TEXT_MAX_LEN characters.
  - response_payload_json is capped at PAYLOAD_JSON_MAX_BYTES bytes.

Durability note
===============
InMemoryMessageRepository is for unit tests and local development ONLY.
All state is lost on process exit / container restart.
The production implementation is LakebaseMessageRepository.

H1 -- TransparencE Conversation History Persistence
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Protocol, Tuple, runtime_checkable


# =============================================================================
# SIZE LIMITS
# =============================================================================

# Maximum length (characters) for message_text.
MESSAGE_TEXT_MAX_LEN: int = 4000

# Maximum size (bytes, UTF-8) for response_payload_json.
PAYLOAD_JSON_MAX_BYTES: int = 524_288  # 512 KiB

# Maximum page size for list_messages pagination.
MAX_PAGE_SIZE: int = 100


# =============================================================================
# UTC HELPERS
# =============================================================================


def _utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


def _require_aware(dt: datetime, name: str) -> None:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{name} must be timezone-aware; got naive {dt!r}")


# =============================================================================
# DOMAIN MODEL
# =============================================================================


@dataclass(frozen=True)
class MessageRecord:
    """Immutable domain model for one persisted conversation message.

    Fields
    ------
    message_id
        Application-generated UUID; stable primary key.
    owner_user_id_hash
        64-character hex HMAC-SHA256 of the user identity.  Never a raw email.
    frontend_conversation_id
        UUID supplied by the React browser client.
    message_sequence
        1-based monotonically increasing sequence number within
        (owner_user_id_hash, frontend_conversation_id).
    role
        'user' or 'assistant'.
    message_text
        Plain-text content.  For user messages: the prompt as typed.
        For assistant messages: the narrative text portion of the response.
        Maximum MESSAGE_TEXT_MAX_LEN characters.
    response_payload_json
        JSON string containing the normalized assistant response payload
        needed to reconstruct the frontend message.  None for user messages.
        Maximum PAYLOAD_JSON_MAX_BYTES bytes (UTF-8 encoded).
    is_active
        False when the parent conversation was reset / expired / deleted.
    created_at
        UTC datetime of row creation.  Immutable.
    updated_at
        UTC datetime of last mutation (created_at on INSERT;
        updated when is_active transitions to False).
    """

    message_id: str
    owner_user_id_hash: str
    frontend_conversation_id: str
    message_sequence: int
    role: str
    message_text: str
    response_payload_json: Optional[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.message_id:
            raise ValueError("message_id must not be empty")
        if not self.owner_user_id_hash:
            raise ValueError("owner_user_id_hash must not be empty")
        if not self.frontend_conversation_id:
            raise ValueError("frontend_conversation_id must not be empty")
        if self.message_sequence < 1:
            raise ValueError(f"message_sequence must be >= 1, got {self.message_sequence!r}")
        if self.role not in ("user", "assistant"):
            raise ValueError(f"role must be 'user' or 'assistant', got {self.role!r}")
        if not self.message_text:
            raise ValueError("message_text must not be empty")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")


# =============================================================================
# REPOSITORY EXCEPTIONS
# =============================================================================


class MessageRepositoryError(Exception):
    """Base exception for all message repository errors."""


class MessageRepositoryUnavailableError(MessageRepositoryError):
    """Raised when the repository backend is unreachable or consistently failing."""


class MessageValidationError(MessageRepositoryError):
    """Raised when the caller supplies invalid field values."""


class MessageSequenceConflictError(MessageRepositoryError):
    """Raised when the (owner, frontend_id, sequence) unique constraint fires.

    This indicates a concurrent write race.  Callers may retry with a
    fresh sequence lookup.
    """


# =============================================================================
# REPOSITORY PROTOCOL
# =============================================================================


@runtime_checkable
class MessageRepository(Protocol):
    """Runtime-checkable protocol for message persistence.

    All implementations must satisfy this interface.  The production
    implementation is LakebaseMessageRepository; the test implementation
    is InMemoryMessageRepository.
    """

    def append_message(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
        role: str,
        message_text: str,
        response_payload_json: Optional[str] = None,
        message_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> MessageRecord:
        """Append a new message and return the stored record.

        Caller is responsible for:
          - owner_user_id_hash derivation
          - message_text truncation to MESSAGE_TEXT_MAX_LEN
          - response_payload_json size validation
          - only passing response_payload_json for 'assistant' messages

        Args:
            owner_user_id_hash: Pre-derived 64-char hex owner digest.
            frontend_conversation_id: Browser-supplied UUID.
            role: 'user' or 'assistant'.
            message_text: Plain-text content.
            response_payload_json: JSON payload for assistant messages; None for user.
            message_id: Optional pre-generated UUID; generated if None.
            now: Optional UTC datetime; uses current UTC if None.

        Returns:
            The stored MessageRecord with the assigned sequence number.

        Raises:
            MessageValidationError: Invalid field values.
            MessageSequenceConflictError: Concurrent write conflict on sequence.
            MessageRepositoryUnavailableError: Backend unavailable.
        """
        ...

    def list_messages(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[MessageRecord], int]:
        """Return a page of active messages in ascending sequence order.

        Only messages with is_active=True are returned.  Deactivated messages
        (belonging to reset/expired conversations) are excluded.

        Args:
            owner_user_id_hash: Pre-derived owner digest (ownership filter).
            frontend_conversation_id: Browser UUID.
            page: 1-based page number.
            page_size: Maximum records per page; capped at MAX_PAGE_SIZE.

        Returns:
            Tuple of (records, total_active_count).
            records: Ordered by message_sequence ASC, page_size entries.
            total_active_count: Total active messages for this conversation.

        Raises:
            MessageRepositoryUnavailableError: Backend unavailable.
        """
        ...

    def deactivate_messages(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
        now: Optional[datetime] = None,
    ) -> int:
        """Deactivate all active messages for a conversation.

        Called when the parent conversation is reset, expired, or deleted.
        Returns the count of rows updated.  0 is a valid result (no messages
        existed yet).

        Args:
            owner_user_id_hash: Pre-derived owner digest.
            frontend_conversation_id: Browser UUID.
            now: Optional UTC datetime; uses current UTC if None.

        Raises:
            MessageRepositoryUnavailableError: Backend unavailable.
        """
        ...


# =============================================================================
# IN-MEMORY REFERENCE IMPLEMENTATION
# =============================================================================


class InMemoryMessageRepository:
    """Thread-safe in-memory implementation of MessageRepository.

    For unit tests and local development ONLY.  All state is lost on
    process restart.  Not for production use.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Key: (owner_user_id_hash, frontend_conversation_id) -> List[MessageRecord]
        self._store: Dict[Tuple[str, str], List[MessageRecord]] = {}

    def _key(self, owner: str, frontend_id: str) -> Tuple[str, str]:
        return (owner, frontend_id)

    def append_message(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
        role: str,
        message_text: str,
        response_payload_json: Optional[str] = None,
        message_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> MessageRecord:
        if not owner_user_id_hash:
            raise MessageValidationError("owner_user_id_hash must not be empty")
        if not frontend_conversation_id:
            raise MessageValidationError("frontend_conversation_id must not be empty")
        if role not in ("user", "assistant"):
            raise MessageValidationError(f"role must be 'user' or 'assistant'")
        if not message_text:
            raise MessageValidationError("message_text must not be empty")
        if len(message_text) > MESSAGE_TEXT_MAX_LEN:
            raise MessageValidationError(
                f"message_text exceeds {MESSAGE_TEXT_MAX_LEN} characters"
            )
        if response_payload_json is not None:
            if len(response_payload_json.encode("utf-8")) > PAYLOAD_JSON_MAX_BYTES:
                raise MessageValidationError(
                    f"response_payload_json exceeds {PAYLOAD_JSON_MAX_BYTES} bytes"
                )

        ts = now if now is not None else _utcnow()
        if ts.tzinfo is None:
            raise MessageValidationError("now must be timezone-aware")

        mid = message_id or str(uuid.uuid4())

        with self._lock:
            k = self._key(owner_user_id_hash, frontend_conversation_id)
            existing = self._store.get(k, [])
            next_seq = len(existing) + 1
            for rec in existing:
                if rec.message_id == mid:
                    # Idempotent: same message_id already stored
                    return rec

            record = MessageRecord(
                message_id=mid,
                owner_user_id_hash=owner_user_id_hash,
                frontend_conversation_id=frontend_conversation_id,
                message_sequence=next_seq,
                role=role,
                message_text=message_text,
                response_payload_json=response_payload_json,
                is_active=True,
                created_at=ts,
                updated_at=ts,
            )
            self._store[k] = existing + [record]
            return record

    def list_messages(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[MessageRecord], int]:
        ps = min(max(1, page_size), MAX_PAGE_SIZE)
        pg = max(1, page)
        with self._lock:
            k = self._key(owner_user_id_hash, frontend_conversation_id)
            all_active = [
                r for r in self._store.get(k, []) if r.is_active
            ]
            # Sort by sequence ascending
            all_active.sort(key=lambda r: r.message_sequence)
            total = len(all_active)
            offset = (pg - 1) * ps
            page_records = all_active[offset: offset + ps]
            return page_records, total

    def deactivate_messages(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
        now: Optional[datetime] = None,
    ) -> int:
        ts = now if now is not None else _utcnow()
        with self._lock:
            k = self._key(owner_user_id_hash, frontend_conversation_id)
            records = self._store.get(k, [])
            count = 0
            updated = []
            for r in records:
                if r.is_active:
                    from dataclasses import replace as _dc_replace
                    updated.append(_dc_replace(r, is_active=False, updated_at=ts))
                    count += 1
                else:
                    updated.append(r)
            self._store[k] = updated
            return count

    # Test helpers
    def _all_for(self, owner: str, frontend_id: str) -> List[MessageRecord]:
        """Test helper: return all records (active + inactive) in sequence order."""
        with self._lock:
            return list(self._store.get(self._key(owner, frontend_id), []))

    def _total_stored(self) -> int:
        """Test helper: total records across all conversations."""
        with self._lock:
            return sum(len(v) for v in self._store.values())
