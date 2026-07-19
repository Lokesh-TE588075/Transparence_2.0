"""Narrowly-scoped runtime accessor for the conversation reset coordinator.

Phase 4C4B3B — Retrieves the shared GenieSessionStore and
DurableGenieSessionAdapter from the existing application-scoped
GeniePipeline singleton and constructs a ConversationResetCoordinator
that uses exactly those objects.

Design constraints:
- No environment access at module import time.
- No new GenieSessionStore instance is ever created here.
- No new DurableGenieSessionAdapter instance is ever created here.
- No Lakebase connection at import time or at call time (connections are
  owned by the adapter that was already built by the pipeline factory).
- No credentials are generated or read here.
- Raises ConversationResetRuntimeUnavailableError (sanitized) when the
  shared runtime is not available, disabled, or not yet initialized.
- No resource ownership: this module holds no state and performs no
  cleanup; resource lifecycle belongs to genie_backend_factory.py.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Attribute name used by genie_backend_factory to attach the runtime bundle.
_DURABLE_RUNTIME_BUNDLE_ATTR: str = "_durable_session_runtime_bundle"

# Pipeline session-store attribute name (set in GeniePipeline.__init__).
_PIPELINE_SESSION_STORE_ATTR: str = "_store"


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class ConversationResetRuntimeError(Exception):
    """Base error for the conversation reset runtime."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class ConversationResetRuntimeUnavailableError(ConversationResetRuntimeError):
    """Raised when the shared durable runtime is not available.

    This covers:
    - Pipeline singleton not yet initialized.
    - Durable runtime bundle not attached to the pipeline.
    - Durable adapter disabled (bundle.adapter is None).
    - Any unexpected failure during coordinator construction.

    The message never contains host, credentials, or internal details.
    """


# ---------------------------------------------------------------------------
# Public accessor
# ---------------------------------------------------------------------------


def get_conversation_reset_coordinator():
    """Return a ConversationResetCoordinator using the shared pipeline's
    session store and durable adapter.

    The coordinator is constructed fresh on each call, but the *store* and
    *adapter* it receives are always the exact objects owned by the singleton
    GeniePipeline.  This guarantees that reset operations affect the same
    in-memory session state that the chat pipeline uses.

    Returns
    -------
    ConversationResetCoordinator
        Wired with the shared session store and durable adapter.

    Raises
    ------
    ConversationResetRuntimeUnavailableError
        When the pipeline singleton is unavailable, the durable bundle is
        missing, or the durable adapter is disabled or None.
    """
    # Deferred imports — no side effects at module load time.
    from app.services.genie_backend_factory import get_genie_pipeline  # noqa: PLC0415
    from app.services.conversation_reset_coordinator import (  # noqa: PLC0415
        ConversationResetCoordinator,
        ResetCoordinatorInvalidInputError,
    )

    # Retrieve (or lazily build) the singleton pipeline.
    try:
        pipeline = get_genie_pipeline(user_token=None)
    except Exception:
        logger.error(
            "conversation_reset_runtime: pipeline singleton unavailable."
        )
        raise ConversationResetRuntimeUnavailableError(
            "Conversation reset runtime is unavailable."
        )

    # Extract the shared session store (GeniePipeline stores it as _store).
    store = getattr(pipeline, _PIPELINE_SESSION_STORE_ATTR, None)
    if store is None:
        logger.error(
            "conversation_reset_runtime: shared session store not found on pipeline."
        )
        raise ConversationResetRuntimeUnavailableError(
            "Conversation reset runtime is unavailable."
        )

    # Extract the durable runtime bundle.
    bundle = getattr(pipeline, _DURABLE_RUNTIME_BUNDLE_ATTR, None)
    if bundle is None:
        logger.error(
            "conversation_reset_runtime: durable runtime bundle not attached to pipeline."
        )
        raise ConversationResetRuntimeUnavailableError(
            "Conversation reset runtime is unavailable."
        )

    # Extract the adapter from the bundle (None when durable mode is disabled).
    adapter = getattr(bundle, "adapter", None)
    if adapter is None:
        logger.error(
            "conversation_reset_runtime: durable adapter is disabled or None."
        )
        raise ConversationResetRuntimeUnavailableError(
            "Conversation reset runtime is unavailable."
        )

    # Construct the coordinator with the shared objects.
    try:
        return ConversationResetCoordinator(adapter=adapter, session_store=store)
    except ResetCoordinatorInvalidInputError:
        logger.error(
            "conversation_reset_runtime: coordinator construction failed (invalid input)."
        )
        raise ConversationResetRuntimeUnavailableError(
            "Conversation reset runtime is unavailable."
        )
    except Exception:
        logger.error(
            "conversation_reset_runtime: unexpected error constructing coordinator."
        )
        raise ConversationResetRuntimeUnavailableError(
            "Conversation reset runtime is unavailable."
        )


__all__ = [
    "ConversationResetRuntimeError",
    "ConversationResetRuntimeUnavailableError",
    "get_conversation_reset_coordinator",
]
