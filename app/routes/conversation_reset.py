"""Secure conversation reset HTTP endpoint — Phase 4C4B3B.

POST /api/conversations/{frontend_conversation_id}/reset

Security properties:
- Trusted server-derived identity ONLY.  No owner identifier from body,
  query string, cookies, or headers controlled by the frontend.
- Fails closed when trusted identity is disabled OR unavailable.
- Canonical frontend conversation ID is derived via strip(); whitespace
  variants resolve to the same logical conversation.
- Opaque process-local key derived from (owner_hash, session_id, frontend_id)
  via build_process_local_conversation_key().
- Shared GenieSessionStore and DurableGenieSessionAdapter obtained via
  get_conversation_reset_coordinator() — no duplicate objects created.
- All HTTP responses are static and sanitized — no owner hash, session ID,
  process-local key, record ID, Genie ID, version, or exception text.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.services.request_owner_identity_runtime import (
    REQUEST_OWNER_IDENTITY_STATE_ATTRIBUTE,
    RequestOwnerIdentityRuntimeConfigurationError,
    RequestOwnerIdentityRuntimeResolutionError,
    resolve_request_owner_identity,
)
from app.services.process_local_conversation_key import (
    build_process_local_conversation_key,
    ProcessLocalConversationKeyError,
)
from app.services.conversation_reset_coordinator import (
    ResetCoordinatorInvalidInputError,
    ResetCoordinatorConflictError,
    ResetCoordinatorUnavailableError,
    ResetCoordinatorInternalError,
)
from app.services.conversation_reset_runtime import (
    get_conversation_reset_coordinator,
    ConversationResetRuntimeUnavailableError,
)
from app.services.message_history_service import deactivate_conversation_messages
from app.services.message_repository_runtime import get_message_repository

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Static sanitized response bodies
# No identifiers, versions, internal state, or exception text in any response.
# ---------------------------------------------------------------------------

_RESP_SUCCESS: dict = {
    "status": "reset",
    "message": "Conversation reset successfully.",
}
_RESP_INVALID_CONV: dict = {
    "status": "error",
    "message": "Invalid conversation identifier.",
}
_RESP_IDENTITY_REQUIRED: dict = {
    "status": "error",
    "message": "Trusted request identity is required.",
}
_RESP_IDENTITY_UNAVAILABLE: dict = {
    "status": "error",
    "message": "Trusted request identity is unavailable.",
}
_RESP_CONFLICT: dict = {
    "status": "error",
    "message": "Conversation reset could not be completed. Please retry.",
}
_RESP_UNAVAILABLE: dict = {
    "status": "error",
    "message": "Conversation reset is temporarily unavailable.",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _canonicalize_frontend_id(raw: str) -> str | None:
    """Return the canonical frontend conversation ID, or None if invalid.

    Applies the same canonicalization contract as DurableGenieSessionKey and
    build_process_local_conversation_key: strip whitespace, reject empty
    strings and strings containing '@'.
    """
    canonical = raw.strip()
    if not canonical:
        return None
    if "@" in canonical:
        return None
    return canonical


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/conversations/{frontend_conversation_id}/reset")
async def reset_conversation(
    frontend_conversation_id: str,
    request: Request,
) -> JSONResponse:
    """Reset the Genie conversation session for the authenticated owner.

    Endpoint contract (Phase 4C4B3B):
    - Accepts path parameter only; no JSON body required.
    - Owner identity derived exclusively from trusted server-side headers.
    - Fails closed (503) when trusted identity is disabled.
    - Returns 200 for all successful reset outcomes (RESET, ALREADY_INACTIVE,
      TOMBSTONE_CREATED).
    - Returns static sanitized errors for all failure cases.
    - No identifiers are logged or returned in any HTTP response.
    """

    # ------------------------------------------------------------------
    # Step 1: Resolve trusted identity from server-derived headers.
    # This is the SOLE approved owner source — no body, query, or
    # frontend-controlled header is ever consulted for identity.
    # ------------------------------------------------------------------
    try:
        trusted_identity = resolve_request_owner_identity(headers=request.headers)
    except RequestOwnerIdentityRuntimeConfigurationError:
        # HMAC secret missing/invalid, flag misconfigured, or internal error.
        return JSONResponse(
            status_code=503,
            content=_RESP_IDENTITY_UNAVAILABLE,
        )
    except RequestOwnerIdentityRuntimeResolutionError:
        # Feature enabled but the trusted header is missing or invalid.
        return JSONResponse(
            status_code=401,
            content=_RESP_IDENTITY_REQUIRED,
        )
    except Exception:
        # Unexpected failure — fail closed.
        logger.error("conversation_reset: unexpected identity resolution failure.")
        return JSONResponse(
            status_code=503,
            content=_RESP_IDENTITY_UNAVAILABLE,
        )

    # Fail closed when trusted identity is disabled.
    # The reset endpoint requires a verified owner; anonymous/default reset
    # cannot establish durable ownership and is therefore never permitted.
    if trusted_identity is None:
        return JSONResponse(
            status_code=503,
            content=_RESP_IDENTITY_UNAVAILABLE,
        )

    # Attach to request.state following the established pattern from chat.py.
    setattr(request.state, REQUEST_OWNER_IDENTITY_STATE_ATTRIBUTE, trusted_identity)
    trusted_owner_hash: str = trusted_identity.owner_user_id_hash

    # ------------------------------------------------------------------
    # Step 2: Validate and canonicalize the frontend conversation ID.
    # The same canonical value must be used for the durable key, the
    # process-local key, and the coordinator invocation.
    # Malformed values are rejected before any backend call.
    # ------------------------------------------------------------------
    canonical_frontend_id = _canonicalize_frontend_id(frontend_conversation_id)
    if canonical_frontend_id is None:
        return JSONResponse(
            status_code=400,
            content=_RESP_INVALID_CONV,
        )

    # ------------------------------------------------------------------
    # Step 3: Obtain session ID from established server middleware state.
    # Set by session_middleware in main.py for every HTTP request.
    # Never accepted from request body, query string, or frontend headers.
    # ------------------------------------------------------------------
    session_id: str = (
        getattr(getattr(request, "state", None), "session_id", None) or ""
    )

    # ------------------------------------------------------------------
    # Step 4: Derive the opaque owner-scoped process-local key.
    # Binds (owner_hash, session_id, frontend_id) into a single opaque
    # digest that can safely be used as a GenieSessionStore key.
    # ProcessLocalConversationKeyError maps to an invalid-conversation 400.
    # ------------------------------------------------------------------
    try:
        process_local_key: str = build_process_local_conversation_key(
            owner_user_id_hash=trusted_owner_hash,
            session_id=session_id,
            frontend_conversation_id=canonical_frontend_id,
        )
    except ProcessLocalConversationKeyError:
        return JSONResponse(
            status_code=400,
            content=_RESP_INVALID_CONV,
        )
    except Exception:
        logger.error("conversation_reset: unexpected error deriving process-local key.")
        return JSONResponse(
            status_code=400,
            content=_RESP_INVALID_CONV,
        )

    # ------------------------------------------------------------------
    # Step 5: Obtain the shared coordinator.
    # The coordinator wraps the SAME GenieSessionStore and adapter that
    # the production chat pipeline uses — no duplicate instances.
    # ------------------------------------------------------------------
    try:
        coordinator = get_conversation_reset_coordinator()
    except ConversationResetRuntimeUnavailableError:
        return JSONResponse(
            status_code=503,
            content=_RESP_UNAVAILABLE,
        )
    except Exception:
        logger.error("conversation_reset: unexpected error obtaining coordinator.")
        return JSONResponse(
            status_code=503,
            content=_RESP_UNAVAILABLE,
        )

    # ------------------------------------------------------------------
    # Step 6: Invoke the coordinator.
    # The coordinator owns all tombstone, CAS, and conflict logic.
    # The route is only an identity/validation/DI/HTTP-mapping boundary.
    # Success: all three outcomes (RESET, ALREADY_INACTIVE, TOMBSTONE_CREATED)
    # are idempotent successes that return 200.
    # ------------------------------------------------------------------
    try:
        coordinator.reset(
            owner_user_id_hash=trusted_owner_hash,
            frontend_conversation_id=canonical_frontend_id,
            process_local_conversation_key=process_local_key,
        )
    except ResetCoordinatorInvalidInputError:
        # Input validation failure inside coordinator (e.g. malformed frontend ID
        # that passed the route-level check but failed the stricter key contract).
        return JSONResponse(
            status_code=400,
            content=_RESP_INVALID_CONV,
        )
    except ResetCoordinatorConflictError:
        return JSONResponse(
            status_code=409,
            content=_RESP_CONFLICT,
        )
    except (ResetCoordinatorUnavailableError, ResetCoordinatorInternalError):
        return JSONResponse(
            status_code=503,
            content=_RESP_UNAVAILABLE,
        )
    except Exception:
        logger.error("conversation_reset: unexpected error during coordinator reset.")
        return JSONResponse(
            status_code=503,
            content=_RESP_UNAVAILABLE,
        )

    # ------------------------------------------------------------------
    # Step 7: H1 — Deactivate message history for this conversation.
    # Non-blocking: failure must not affect the reset success response.
    # The message repo returns None when ENABLE_MESSAGE_HISTORY=false,
    # in which case this is a no-op.
    # ------------------------------------------------------------------
    _msg_repo = get_message_repository()
    if _msg_repo is not None:
        deactivate_conversation_messages(
            repo=_msg_repo,
            owner_user_id_hash=trusted_owner_hash,
            frontend_conversation_id=canonical_frontend_id,
        )

    # ------------------------------------------------------------------
    # Step 8: Return static success response.
    # Never exposes which internal outcome occurred.
    # ------------------------------------------------------------------
    return JSONResponse(
        status_code=200,
        content=_RESP_SUCCESS,
    )
