"""Conversation persistence repository contract and in-memory reference implementation.

This module defines:

  - ConversationStatus      -- lifecycle enum for a tracked conversation
  - ConversationRecord      -- immutable domain model for a single conversation entry
  - Repository exceptions   -- ConversationRepositoryError hierarchy
  - ConversationRepository  -- runtime-checkable Protocol / abstract interface
  - InMemoryConversationRepository -- thread-safe reference implementation

IMPORTANT — durability limitation
==================================
InMemoryConversationRepository is provided as a reference implementation for
unit tests and local development ONLY.  It holds all state in-process memory.
Data is permanently lost when the process exits, restarts, or is suspended
(e.g. on a Databricks Apps container cold-start after inactivity).

It MUST NOT be used as the authoritative enterprise production repository.
The enterprise production implementation is LakebaseConversationRepository
(Phase 2B), which persists records in a Lakebase Postgres table with
compare-and-swap semantics that mirror this contract exactly.

Identity handling
=================
This module never derives, parses, or hashes user identities.  All callers
must supply a pre-derived ``owner_user_id_hash`` (e.g. HMAC-SHA256 of a
Databricks user-email, produced by the identity service in Phase 2C).
Raw email addresses and Databricks user IDs must never be stored here.

Thread safety
=============
InMemoryConversationRepository uses a ``threading.RLock`` so that a single
repository instance may be shared safely across multiple threads or async
workers within one process.

Optimistic concurrency
======================
Every mutating operation (except initial create and physical delete) requires
``expected_version``.  The operation succeeds only when the stored version
equals ``expected_version`` and atomically increments the version by exactly
1.  A mismatch raises ``ConversationVersionConflictError`` and leaves the
record completely unchanged.  This contract matches the future Lakebase
CAS (compare-and-swap) implementation.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from dataclasses import replace as _dc_replace
from datetime import datetime, timezone
from enum import Enum
from typing import Collection, Dict, List, Optional, Protocol, Tuple, runtime_checkable


# =============================================================================
# UTC HELPERS
# =============================================================================


def _utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


def _require_aware(dt: datetime, name: str) -> None:
    """Raise ValueError when *dt* is naive (timezone-unaware)."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(
            f"{name} must be a timezone-aware datetime; got naive {dt!r}"
        )


# =============================================================================
# CONVERSATION STATUS
# =============================================================================


class ConversationStatus(str, Enum):
    """Lifecycle states for a tracked Genie conversation.

    ACTIVE   -- conversation is in normal use.
    STALE    -- conversation has exceeded the inactivity threshold and may be
                refreshed or reset.
    RESET    -- conversation was explicitly reset by the user or system.
    EXPIRED  -- conversation has passed the hard expiry TTL and should not
                be resumed.
    """

    ACTIVE = "ACTIVE"
    STALE = "STALE"
    RESET = "RESET"
    EXPIRED = "EXPIRED"


# =============================================================================
# DOMAIN MODEL
# =============================================================================


@dataclass(frozen=True)
class ConversationRecord:
    """Immutable domain model representing one tracked conversation.

    All instances are frozen (immutable); mutations produce new instances.

    Fields
    ------
    conversation_id
        Stable internal UUID for this conversation record.  Auto-generated
        by the repository on first create when not supplied explicitly.
    owner_user_id_hash
        Pre-derived hash of the user identity.  Never store a raw email
        address or Databricks user ID in this field.
    frontend_conversation_id
        UUID supplied by the browser client to identify its local session.
        Together with ``owner_user_id_hash`` this forms the unique logical
        key that drives idempotent create behaviour.
    genie_conversation_id
        Genie Space conversation ID assigned after the first Genie API call.
        None until the conversation has been bound via ``bind_genie_conversation``.
    last_genie_message_id
        The most recent Genie message ID returned by the Space API.
        None until at least one Genie turn has completed.
    status
        Current lifecycle status (see ``ConversationStatus``).
    version
        Monotonically increasing integer for optimistic concurrency control.
        Starts at 1; incremented by exactly 1 on each successful mutation.
    created_at
        UTC datetime when the record was first created.  Immutable.
    updated_at
        UTC datetime of the last successful mutation.  Changes on every write.
    last_active_at
        UTC datetime of the last user-initiated activity.  Updated by
        ``touch`` and by ``compare_and_update`` when
        ``touch_last_active=True``.
    """

    conversation_id: str
    owner_user_id_hash: str
    frontend_conversation_id: str
    genie_conversation_id: Optional[str]
    last_genie_message_id: Optional[str]
    status: ConversationStatus
    version: int
    created_at: datetime
    updated_at: datetime
    last_active_at: datetime

    def __post_init__(self) -> None:
        # ---- non-empty string checks ----------------------------------------
        if not self.conversation_id:
            raise ValueError("conversation_id must not be empty")
        if not self.owner_user_id_hash:
            raise ValueError("owner_user_id_hash must not be empty")
        if not self.frontend_conversation_id:
            raise ValueError("frontend_conversation_id must not be empty")

        # ---- version bounds --------------------------------------------------
        if self.version < 1:
            raise ValueError(f"version must be >= 1, got {self.version!r}")

        # ---- timezone-aware timestamps ---------------------------------------
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        _require_aware(self.last_active_at, "last_active_at")

        # ---- temporal ordering ----------------------------------------------
        if self.updated_at < self.created_at:
            raise ValueError(
                f"updated_at ({self.updated_at}) must not precede "
                f"created_at ({self.created_at})"
            )
        if self.last_active_at < self.created_at:
            raise ValueError(
                f"last_active_at ({self.last_active_at}) must not precede "
                f"created_at ({self.created_at})"
            )


# =============================================================================
# REPOSITORY EXCEPTIONS
# =============================================================================


class ConversationRepositoryError(Exception):
    """Base exception for all conversation repository errors.

    Callers should catch this base class to handle any repository failure
    without depending on database-specific exception types.  All more
    specific exceptions in this module are subclasses of this base.
    """


class ConversationNotFoundError(ConversationRepositoryError):
    """Raised when a requested conversation does not exist or is not visible
    to the requesting owner (ownership boundary enforced silently)."""


class ConversationAlreadyExistsError(ConversationRepositoryError):
    """Raised when a create request supplies an explicit conversation_id that
    already belongs to a different logical conversation for the same owner."""


class ConversationOwnershipError(ConversationRepositoryError):
    """Raised when a caller attempts to create a conversation using an
    explicit conversation_id that is already owned by a different user."""


class ConversationVersionConflictError(ConversationRepositoryError):
    """Raised when the ``expected_version`` supplied by the caller does not
    match the stored version of the record.

    The record is left completely unchanged.  The caller should re-read the
    record and retry with the current version if appropriate.
    """


class ConversationRepositoryUnavailableError(ConversationRepositoryError):
    """Raised when the underlying storage backend is unavailable or
    unresponsive.  Implementations must wrap database-specific connectivity
    errors in this exception rather than surfacing them directly."""


# =============================================================================
# REPOSITORY INTERFACE
# =============================================================================


@runtime_checkable
class ConversationRepository(Protocol):
    """Runtime-checkable Protocol defining the conversation repository contract.

    All concrete implementations (InMemoryConversationRepository,
    LakebaseConversationRepository, ...) must implement every method listed
    here with identical semantics.

    Ownership scoping
    -----------------
    Every operation is scoped by ``owner_user_id_hash``.  A record is only
    visible to the owner under whom it was created.  Cross-owner access:

    - Lookup methods return None (no ownership leakage).
    - Mutation methods raise ConversationNotFoundError (appears not found).
    - Delete returns False (appears not found).
    - The one exception is create_conversation when an explicitly supplied
      conversation_id belongs to a different owner; that raises
      ConversationOwnershipError because the collision must be surfaced.

    Optimistic concurrency
    ----------------------
    Every mutating operation except ``create_conversation`` and
    ``delete_conversation`` requires ``expected_version``.  If the stored
    version does not match, ``ConversationVersionConflictError`` is raised
    and the record is left completely unchanged.

    Idempotent create
    -----------------
    ``create_conversation`` is idempotent on the logical key
    (owner_user_id_hash + frontend_conversation_id).  A second call with
    the same logical key returns the existing record without modification.
    """

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get_by_id(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
    ) -> Optional[ConversationRecord]:
        """Return the record for *conversation_id* owned by *owner_user_id_hash*.

        Returns None when no matching record exists or when conversation_id
        exists but belongs to a different owner (no cross-owner leakage).
        """
        ...

    def get_by_frontend_id(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
    ) -> Optional[ConversationRecord]:
        """Return the record for *frontend_conversation_id* owned by
        *owner_user_id_hash*, or None when no matching record exists."""
        ...

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create_conversation(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
        *,
        conversation_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        """Create a new conversation or return the existing one idempotently.

        Idempotency: when a record with (owner_user_id_hash,
        frontend_conversation_id) already exists it is returned unchanged.
        No new record is created; version is not incremented.

        Raises
        ------
        ConversationOwnershipError
            When an explicitly supplied *conversation_id* already belongs to
            a different owner.
        ConversationAlreadyExistsError
            When an explicitly supplied *conversation_id* is already
            associated with a different logical conversation for this owner.
        """
        ...

    # ------------------------------------------------------------------
    # Mutations (all require expected_version)
    # ------------------------------------------------------------------

    def bind_genie_conversation(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        genie_conversation_id: str,
        *,
        expected_version: int,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        """Attach a Genie conversation ID to an existing record.

        Raises
        ------
        ConversationNotFoundError
            When the conversation does not exist or is not owned by caller.
        ConversationVersionConflictError
            When *expected_version* does not match the stored version.
        ValueError
            When *genie_conversation_id* is empty.
        """
        ...

    def update_last_genie_message(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        last_genie_message_id: str,
        *,
        expected_version: int,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        """Update the most recent Genie message ID for a conversation.

        Raises
        ------
        ConversationNotFoundError
            When the conversation does not exist or is not owned by caller.
        ConversationVersionConflictError
            When *expected_version* does not match the stored version.
        ValueError
            When *last_genie_message_id* is empty.
        """
        ...

    def touch(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        *,
        expected_version: int,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        """Update *updated_at* and *last_active_at* without changing other fields.

        Used to record recent activity without altering the logical state.

        Raises
        ------
        ConversationNotFoundError
            When the conversation does not exist or is not owned by caller.
        ConversationVersionConflictError
            When *expected_version* does not match the stored version.
        """
        ...

    def set_status(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        status: ConversationStatus,
        *,
        expected_version: int,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        """Set the lifecycle status of a conversation.

        Raises
        ------
        ConversationNotFoundError
            When the conversation does not exist or is not owned by caller.
        ConversationVersionConflictError
            When *expected_version* does not match the stored version.
        """
        ...

    def compare_and_update(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        *,
        expected_version: int,
        genie_conversation_id: Optional[str] = None,
        last_genie_message_id: Optional[str] = None,
        status: Optional[ConversationStatus] = None,
        touch_last_active: bool = True,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        """Atomically update multiple fields in a single compare-and-swap.

        Only fields that are explicitly provided (non-None) are updated.
        The version is incremented exactly once regardless of how many
        fields are changed.

        Parameters
        ----------
        touch_last_active
            When True (default), ``last_active_at`` is updated to *now*.
            When False, ``last_active_at`` is preserved unchanged.

        Raises
        ------
        ConversationNotFoundError
            When the conversation does not exist or is not owned by caller.
        ConversationVersionConflictError
            When *expected_version* does not match the stored version.
        """
        ...

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_for_owner(
        self,
        owner_user_id_hash: str,
        *,
        statuses: Optional[Collection[ConversationStatus]] = None,
        limit: int = 50,
    ) -> List[ConversationRecord]:
        """Return conversations owned by *owner_user_id_hash*.

        Results are sorted by ``updated_at`` descending (most recently
        updated first).  Only records whose ``owner_user_id_hash`` matches
        are included; other owners' records are never returned.

        Parameters
        ----------
        statuses
            Optional filter: only records whose status is in this set.
            When None, all statuses are returned.
        limit
            Maximum number of records to return.  Must be >= 1.

        Raises
        ------
        ValueError
            When *limit* is less than 1.
        """
        ...

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------

    def delete_conversation(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
    ) -> bool:
        """Permanently remove a conversation record.

        Returns True when the record was found and deleted, False when no
        matching record existed for this owner.  A record owned by a
        different owner is treated as not found (False); no ownership error
        is raised to avoid information leakage.
        """
        ...


# =============================================================================
# MODULE-LEVEL HELPERS  (used by InMemoryConversationRepository)
# =============================================================================


def _validate_now(now: Optional[datetime]) -> datetime:
    """Return *now* if provided (must be timezone-aware), else current UTC."""
    if now is None:
        return _utcnow()
    _require_aware(now, "now")
    return now


def _check_version(record: ConversationRecord, expected_version: int) -> None:
    """Raise ConversationVersionConflictError when versions do not match."""
    if record.version != expected_version:
        raise ConversationVersionConflictError(
            f"Version conflict on conversation {record.conversation_id!r}: "
            f"expected {expected_version}, found {record.version}"
        )


# =============================================================================
# IN-MEMORY REFERENCE IMPLEMENTATION
# =============================================================================


class InMemoryConversationRepository(ConversationRepository):
    """Thread-safe in-process conversation repository for tests and local dev.

    .. warning::

        NOT DURABLE — all state is held in Python dicts and is permanently
        lost when the process exits, is restarted, or is suspended by the
        container scheduler (e.g. Databricks Apps cold-start after inactivity).
        This class MUST NOT be used as the authoritative enterprise production
        repository.  Use LakebaseConversationRepository (Phase 2B) for
        production.

    Thread safety
    -------------
    A single ``threading.RLock`` protects both internal indexes.  RLock
    (re-entrant) is used so that the same thread can re-acquire the lock
    without deadlocking, which simplifies future refactoring where one
    method calls another guarded method.

    Internal indexes
    ----------------
    ``_by_id``
        Maps ``conversation_id`` → ``ConversationRecord``.

    ``_by_owner_frontend``
        Maps ``(owner_user_id_hash, frontend_conversation_id)`` →
        ``conversation_id``.

    Both indexes are updated atomically inside the lock.  Neither is ever
    exposed to callers.

    Returned records are instances of the frozen ``ConversationRecord``
    dataclass and are therefore inherently immutable.
    """

    def __init__(self) -> None:
        self._lock: threading.RLock = threading.RLock()
        self._by_id: Dict[str, ConversationRecord] = {}
        self._by_owner_frontend: Dict[Tuple[str, str], str] = {}

    # -------------------------------------------------------------------------
    # Internal helpers  (caller must hold self._lock)
    # -------------------------------------------------------------------------

    def _get_owned(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
    ) -> Optional[ConversationRecord]:
        """Return record only when it exists AND is owned by the caller.

        Returns None both when absent and when owned by a different user,
        preventing cross-owner information leakage.
        """
        record = self._by_id.get(conversation_id)
        if record is None:
            return None
        if record.owner_user_id_hash != owner_user_id_hash:
            return None
        return record

    def _evolve(self, old: ConversationRecord, **changes) -> ConversationRecord:
        """Return a new ConversationRecord derived from *old* with *changes*.

        Uses ``dataclasses.replace``; validation in ``__post_init__`` runs
        on the new instance, preventing storage of invalid records.
        """
        return _dc_replace(old, **changes)

    def _store(self, record: ConversationRecord) -> None:
        """Write *record* into both indexes atomically."""
        self._by_id[record.conversation_id] = record
        key: Tuple[str, str] = (
            record.owner_user_id_hash,
            record.frontend_conversation_id,
        )
        self._by_owner_frontend[key] = record.conversation_id

    def _remove(self, record: ConversationRecord) -> None:
        """Remove *record* from both indexes atomically."""
        self._by_id.pop(record.conversation_id, None)
        key: Tuple[str, str] = (
            record.owner_user_id_hash,
            record.frontend_conversation_id,
        )
        self._by_owner_frontend.pop(key, None)

    # -------------------------------------------------------------------------
    # ConversationRepository interface
    # -------------------------------------------------------------------------

    def get_by_id(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
    ) -> Optional[ConversationRecord]:
        with self._lock:
            return self._get_owned(owner_user_id_hash, conversation_id)

    def get_by_frontend_id(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
    ) -> Optional[ConversationRecord]:
        with self._lock:
            key: Tuple[str, str] = (owner_user_id_hash, frontend_conversation_id)
            cid = self._by_owner_frontend.get(key)
            if cid is None:
                return None
            return self._by_id.get(cid)

    def create_conversation(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
        *,
        conversation_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        with self._lock:
            # Idempotency: return existing record when the logical key exists
            key: Tuple[str, str] = (owner_user_id_hash, frontend_conversation_id)
            existing_cid = self._by_owner_frontend.get(key)
            if existing_cid is not None:
                return self._by_id[existing_cid]

            # Validate explicitly supplied conversation_id for conflicts
            if conversation_id is not None:
                existing = self._by_id.get(conversation_id)
                if existing is not None:
                    if existing.owner_user_id_hash != owner_user_id_hash:
                        raise ConversationOwnershipError(
                            f"conversation_id {conversation_id!r} already belongs "
                            f"to a different owner"
                        )
                    # Same owner, different frontend_id: logical conflict
                    raise ConversationAlreadyExistsError(
                        f"conversation_id {conversation_id!r} is already associated "
                        f"with a different logical conversation for this owner"
                    )

            ts = _validate_now(now)
            cid = conversation_id if conversation_id is not None else str(uuid.uuid4())

            record = ConversationRecord(
                conversation_id=cid,
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
            self._store(record)
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
        if not genie_conversation_id:
            raise ValueError("genie_conversation_id must not be empty")
        with self._lock:
            record = self._get_owned(owner_user_id_hash, conversation_id)
            if record is None:
                raise ConversationNotFoundError(
                    f"Conversation {conversation_id!r} not found for this owner"
                )
            _check_version(record, expected_version)
            ts = _validate_now(now)
            updated = self._evolve(
                record,
                genie_conversation_id=genie_conversation_id,
                version=record.version + 1,
                updated_at=ts,
            )
            self._store(updated)
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
        if not last_genie_message_id:
            raise ValueError("last_genie_message_id must not be empty")
        with self._lock:
            record = self._get_owned(owner_user_id_hash, conversation_id)
            if record is None:
                raise ConversationNotFoundError(
                    f"Conversation {conversation_id!r} not found for this owner"
                )
            _check_version(record, expected_version)
            ts = _validate_now(now)
            updated = self._evolve(
                record,
                last_genie_message_id=last_genie_message_id,
                version=record.version + 1,
                updated_at=ts,
            )
            self._store(updated)
            return updated

    def touch(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        *,
        expected_version: int,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        with self._lock:
            record = self._get_owned(owner_user_id_hash, conversation_id)
            if record is None:
                raise ConversationNotFoundError(
                    f"Conversation {conversation_id!r} not found for this owner"
                )
            _check_version(record, expected_version)
            ts = _validate_now(now)
            updated = self._evolve(
                record,
                version=record.version + 1,
                updated_at=ts,
                last_active_at=ts,
            )
            self._store(updated)
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
        with self._lock:
            record = self._get_owned(owner_user_id_hash, conversation_id)
            if record is None:
                raise ConversationNotFoundError(
                    f"Conversation {conversation_id!r} not found for this owner"
                )
            _check_version(record, expected_version)
            ts = _validate_now(now)
            updated = self._evolve(
                record,
                status=status,
                version=record.version + 1,
                updated_at=ts,
            )
            self._store(updated)
            return updated

    def compare_and_update(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
        *,
        expected_version: int,
        genie_conversation_id: Optional[str] = None,
        last_genie_message_id: Optional[str] = None,
        status: Optional[ConversationStatus] = None,
        touch_last_active: bool = True,
        now: Optional[datetime] = None,
    ) -> ConversationRecord:
        with self._lock:
            record = self._get_owned(owner_user_id_hash, conversation_id)
            if record is None:
                raise ConversationNotFoundError(
                    f"Conversation {conversation_id!r} not found for this owner"
                )
            _check_version(record, expected_version)
            ts = _validate_now(now)

            changes: Dict[str, object] = {
                "version": record.version + 1,
                "updated_at": ts,
            }
            if genie_conversation_id is not None:
                changes["genie_conversation_id"] = genie_conversation_id
            if last_genie_message_id is not None:
                changes["last_genie_message_id"] = last_genie_message_id
            if status is not None:
                changes["status"] = status
            if touch_last_active:
                changes["last_active_at"] = ts

            updated = self._evolve(record, **changes)
            self._store(updated)
            return updated

    def list_for_owner(
        self,
        owner_user_id_hash: str,
        *,
        statuses: Optional[Collection[ConversationStatus]] = None,
        limit: int = 50,
    ) -> List[ConversationRecord]:
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit!r}")
        with self._lock:
            records = [
                r
                for r in self._by_id.values()
                if r.owner_user_id_hash == owner_user_id_hash
                and (statuses is None or r.status in statuses)
            ]
        records.sort(key=lambda r: r.updated_at, reverse=True)
        return records[:limit]

    def delete_conversation(
        self,
        owner_user_id_hash: str,
        conversation_id: str,
    ) -> bool:
        with self._lock:
            record = self._get_owned(owner_user_id_hash, conversation_id)
            if record is None:
                return False
            self._remove(record)
            return True
