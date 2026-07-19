"""Durable conversation reset coordinator.

Orchestrates the authoritative reset of a Genie conversation session by:

1. Validating caller-supplied owner hash and frontend conversation ID.
2. Constructing the owner-scoped durable key.
3. Loading the authoritative record via the durable adapter.
4. Performing the appropriate CAS status transition or tombstone creation.
5. Removing the complete process-local session ONLY after durable success.
6. Returning a sanitized internal result suitable for the later HTTP route.

Design constraints (Phase 4C4B1):

* No HTTP route exposure.
* No request-header parsing.
* No email-address acceptance or identity derivation.
* No delete operations.
* No direct repository access — all durable operations through the adapter.
* No live Lakebase connections in tests.
* Degraded or cache-only adapter results are rejected (fail closed).
* Durable success MUST precede local session removal.
* Exception messages and repr values never expose identifiers.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Optional

from app.services.conversation_repository import ConversationStatus
from app.services.process_local_conversation_key import is_valid_process_local_key
from app.services.durable_genie_session_adapter import (
    DurableGenieSessionAdapter,
    DurableGenieSessionKey,
    DurableGenieSessionUnavailableError,
    DurableGenieSessionVersionConflictError,
    GenieSessionLookupResult,
)
from app.services.genie_session_store import GenieSessionStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------

_OWNER_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class ResetOutcome(str, Enum):
    """Sanitized outcome categories for a reset operation."""

    RESET = "RESET"
    ALREADY_INACTIVE = "ALREADY_INACTIVE"
    TOMBSTONE_CREATED = "TOMBSTONE_CREATED"


class ResetResult:
    """Sanitized internal result returned by the coordinator.

    Exposes only the success boolean and an outcome category.
    No raw owner hash, frontend ID, record ID, Genie ID, or version.
    """

    __slots__ = ("_success", "_outcome")

    def __init__(self, *, success: bool, outcome: ResetOutcome) -> None:
        self._success = success
        self._outcome = outcome

    @property
    def success(self) -> bool:
        return self._success

    @property
    def outcome(self) -> ResetOutcome:
        return self._outcome

    def __repr__(self) -> str:
        return f"ResetResult(success={self._success!r}, outcome={self._outcome.value!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ResetResult):
            return NotImplemented
        return self._success == other._success and self._outcome == other._outcome


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class ResetCoordinatorError(Exception):
    """Base exception for coordinator failures."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class ResetCoordinatorInvalidInputError(ResetCoordinatorError):
    """Raised when input validation fails."""


class ResetCoordinatorConflictError(ResetCoordinatorError):
    """Raised when a version conflict cannot be resolved idempotently."""


class ResetCoordinatorUnavailableError(ResetCoordinatorError):
    """Raised when the durable backend is unavailable."""


class ResetCoordinatorInternalError(ResetCoordinatorError):
    """Raised for unexpected internal failures."""


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class ConversationResetCoordinator:
    """Coordinates durable reset of a Genie conversation session.

    Accepts:
    - A DurableGenieSessionAdapter (through explicit dependency injection).
    - A GenieSessionStore for process-local session removal.

    Does NOT:
    - Parse request headers.
    - Accept email addresses.
    - Derive owner identity.
    - Access the repository directly.
    - Perform delete operations.
    """

    def __init__(
        self,
        *,
        adapter: DurableGenieSessionAdapter,
        session_store: GenieSessionStore,
    ) -> None:
        if adapter is None:
            raise ResetCoordinatorInvalidInputError(
                "Adapter must not be None."
            )
        if session_store is None:
            raise ResetCoordinatorInvalidInputError(
                "Session store must not be None."
            )
        self._adapter = adapter
        self._session_store = session_store

    def reset(
        self,
        *,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
        process_local_conversation_key: str,
    ) -> ResetResult:
        """Execute the durable reset flow.

        Parameters
        ----------
        owner_user_id_hash
            Exactly 64 lowercase hexadecimal characters.
        frontend_conversation_id
            Validated via DurableGenieSessionKey contract.  Used exclusively
            to construct the durable repository key together with
            ``owner_user_id_hash``.  Never used for process-local removal.
        process_local_conversation_key
            Opaque owner-scoped local key produced by
            ``build_process_local_conversation_key()``.  Must match the
            pattern ``^plc_v1_[0-9a-f]{64}$``.  Used exclusively to remove
            the process-local GenieSessionStore entry.  Never used for the
            durable repository key.

        Returns
        -------
        ResetResult
            Sanitized success outcome.

        Raises
        ------
        ResetCoordinatorInvalidInputError
            When input validation fails.
        ResetCoordinatorConflictError
            When a version conflict cannot be resolved idempotently.
        ResetCoordinatorUnavailableError
            When the durable backend is unavailable.
        ResetCoordinatorInternalError
            When an unexpected internal failure occurs.
        """
        # --- Input validation ---
        self._validate_owner_hash(owner_user_id_hash)
        self._validate_process_local_key(process_local_conversation_key)
        key = self._build_key(owner_user_id_hash, frontend_conversation_id)

        # --- Load authoritative state ---
        lookup = self._authoritative_load(key)

        if lookup is not None:
            return self._handle_existing_record(
                key, lookup, process_local_conversation_key
            )
        else:
            return self._handle_missing_record(
                key, process_local_conversation_key
            )

    # -----------------------------------------------------------------------
    # Input validation
    # -----------------------------------------------------------------------

    def _validate_owner_hash(self, owner_user_id_hash: str) -> None:
        """Validate owner hash: exactly 64 lowercase hex characters."""
        if not owner_user_id_hash or not isinstance(owner_user_id_hash, str):
            raise ResetCoordinatorInvalidInputError(
                "Owner identifier validation failed."
            )
        if not _OWNER_HASH_PATTERN.match(owner_user_id_hash):
            raise ResetCoordinatorInvalidInputError(
                "Owner identifier validation failed."
            )

    def _validate_process_local_key(self, process_local_conversation_key: str) -> None:
        """Validate process-local key: must match plc_v1_ output format."""
        if not is_valid_process_local_key(process_local_conversation_key):
            raise ResetCoordinatorInvalidInputError(
                "Process-local key validation failed."
            )

    def _build_key(
        self,
        owner_user_id_hash: str,
        frontend_conversation_id: str,
    ) -> DurableGenieSessionKey:
        """Construct the durable key, rejecting invalid frontend IDs."""
        try:
            return DurableGenieSessionKey(
                owner_user_id_hash=owner_user_id_hash,
                frontend_conversation_id=frontend_conversation_id,
            )
        except (ValueError, TypeError) as exc:
            raise ResetCoordinatorInvalidInputError(
                "Conversation identifier validation failed."
            ) from exc

    # -----------------------------------------------------------------------
    # Authoritative state loading
    # -----------------------------------------------------------------------

    def _authoritative_load(
        self,
        key: DurableGenieSessionKey,
    ) -> Optional[GenieSessionLookupResult]:
        """Load from adapter, rejecting degraded results."""
        try:
            result = self._adapter.load(key)
        except DurableGenieSessionUnavailableError as exc:
            raise ResetCoordinatorUnavailableError(
                "Durable state is temporarily unavailable."
            ) from exc
        except Exception as exc:
            raise ResetCoordinatorInternalError(
                "An internal error occurred."
            ) from exc

        if result is not None and result.degraded:
            raise ResetCoordinatorUnavailableError(
                "Durable state is temporarily unavailable."
            )

        return result

    # -----------------------------------------------------------------------
    # Existing record handling
    # -----------------------------------------------------------------------

    def _handle_existing_record(
        self,
        key: DurableGenieSessionKey,
        lookup: GenieSessionLookupResult,
        process_local_conversation_key: str,
    ) -> ResetResult:
        """Handle reset when an authoritative record exists."""
        record = lookup.record
        status = record.status

        if status == ConversationStatus.ACTIVE:
            return self._transition_active_to_reset(
                key, record.version, process_local_conversation_key
            )

        # RESET, STALE, or EXPIRED — already inactive
        self._remove_local_session(process_local_conversation_key)
        return ResetResult(
            success=True,
            outcome=ResetOutcome.ALREADY_INACTIVE,
        )

    def _transition_active_to_reset(
        self,
        key: DurableGenieSessionKey,
        expected_version: int,
        process_local_conversation_key: str,
    ) -> ResetResult:
        """CAS transition from ACTIVE to RESET."""
        try:
            updated_record = self._adapter.set_status(
                key,
                ConversationStatus.RESET,
                expected_version=expected_version,
            )
        except DurableGenieSessionVersionConflictError:
            return self._handle_version_conflict(
                key, process_local_conversation_key
            )
        except DurableGenieSessionUnavailableError as exc:
            raise ResetCoordinatorUnavailableError(
                "Durable state is temporarily unavailable."
            ) from exc
        except Exception as exc:
            raise ResetCoordinatorInternalError(
                "An internal error occurred."
            ) from exc

        # Confirm the returned record has RESET status
        if updated_record.status != ConversationStatus.RESET:
            raise ResetCoordinatorInternalError(
                "An internal error occurred."
            )

        # Durable success confirmed — now remove local session
        self._remove_local_session(process_local_conversation_key)
        return ResetResult(
            success=True,
            outcome=ResetOutcome.RESET,
        )

    # -----------------------------------------------------------------------
    # Missing record tombstone flow
    # -----------------------------------------------------------------------

    def _handle_missing_record(
        self,
        key: DurableGenieSessionKey,
        process_local_conversation_key: str,
    ) -> ResetResult:
        """Handle reset when no authoritative record exists."""
        # Call get_or_create exactly once
        try:
            create_result = self._adapter.get_or_create(key)
        except DurableGenieSessionUnavailableError as exc:
            raise ResetCoordinatorUnavailableError(
                "Durable state is temporarily unavailable."
            ) from exc
        except Exception as exc:
            raise ResetCoordinatorInternalError(
                "An internal error occurred."
            ) from exc

        # Reject degraded/unconfirmed results
        if create_result.degraded:
            raise ResetCoordinatorUnavailableError(
                "Durable state is temporarily unavailable."
            )

        record = create_result.record

        if record.status == ConversationStatus.ACTIVE:
            # Fresh ACTIVE record — transition to RESET
            return self._transition_to_reset_tombstone(
                key, record.version, process_local_conversation_key
            )

        # Another request already made this key non-active (RESET, STALE, EXPIRED)
        self._remove_local_session(process_local_conversation_key)
        return ResetResult(
            success=True,
            outcome=ResetOutcome.TOMBSTONE_CREATED,
        )

    def _transition_to_reset_tombstone(
        self,
        key: DurableGenieSessionKey,
        expected_version: int,
        process_local_conversation_key: str,
    ) -> ResetResult:
        """Transition a freshly-created ACTIVE to RESET for tombstone."""
        try:
            updated_record = self._adapter.set_status(
                key,
                ConversationStatus.RESET,
                expected_version=expected_version,
            )
        except DurableGenieSessionVersionConflictError:
            return self._handle_version_conflict(
                key, process_local_conversation_key
            )
        except DurableGenieSessionUnavailableError as exc:
            raise ResetCoordinatorUnavailableError(
                "Durable state is temporarily unavailable."
            ) from exc
        except Exception as exc:
            raise ResetCoordinatorInternalError(
                "An internal error occurred."
            ) from exc

        # Confirm RESET
        if updated_record.status != ConversationStatus.RESET:
            raise ResetCoordinatorInternalError(
                "An internal error occurred."
            )

        self._remove_local_session(process_local_conversation_key)
        return ResetResult(
            success=True,
            outcome=ResetOutcome.TOMBSTONE_CREATED,
        )

    # -----------------------------------------------------------------------
    # Version conflict handling
    # -----------------------------------------------------------------------

    def _handle_version_conflict(
        self,
        key: DurableGenieSessionKey,
        process_local_conversation_key: str,
    ) -> ResetResult:
        """Handle version conflict: reload at most once."""
        reload_result = self._reload_after_conflict(key)

        if reload_result is None:
            # Reload returned None — conflict
            raise ResetCoordinatorConflictError(
                "A concurrent modification prevented the reset."
            )

        record = reload_result.record
        status = record.status

        if status in (
            ConversationStatus.RESET,
            ConversationStatus.STALE,
            ConversationStatus.EXPIRED,
        ):
            # Idempotent success — already non-active
            self._remove_local_session(process_local_conversation_key)
            return ResetResult(
                success=True,
                outcome=ResetOutcome.ALREADY_INACTIVE,
            )

        if status == ConversationStatus.ACTIVE:
            # Still ACTIVE after conflict — do not retry
            raise ResetCoordinatorConflictError(
                "A concurrent modification prevented the reset."
            )

        # Unknown status — should not happen
        raise ResetCoordinatorInternalError(
            "An internal error occurred."
        )

    def _reload_after_conflict(
        self,
        key: DurableGenieSessionKey,
    ) -> Optional[GenieSessionLookupResult]:
        """Reload the record once after a version conflict."""
        try:
            result = self._adapter.load(key)
        except DurableGenieSessionUnavailableError as exc:
            raise ResetCoordinatorUnavailableError(
                "Durable state is temporarily unavailable."
            ) from exc
        except Exception as exc:
            raise ResetCoordinatorInternalError(
                "An internal error occurred."
            ) from exc

        # Reject degraded reloads
        if result is not None and result.degraded:
            raise ResetCoordinatorUnavailableError(
                "Durable state is temporarily unavailable."
            )

        return result

    # -----------------------------------------------------------------------
    # Local session removal
    # -----------------------------------------------------------------------

    def _remove_local_session(self, process_local_conversation_key: str) -> None:
        """Remove the process-local session using the opaque local key.

        Non-throwing: an unexpected exception from remove_session does NOT
        reverse the authoritative durable RESET.  The session will be cleaned
        up on the next TTL expiry cycle.
        """
        try:
            self._session_store.remove_session(process_local_conversation_key)
        except Exception:
            logger.warning(
                "Local session removal encountered an unexpected condition."
            )
