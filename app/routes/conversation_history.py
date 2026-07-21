"""Conversation history HTTP endpoint.

GET /api/conversations/{frontend_conversation_id}/messages

Security contract:
  - Trusted server-derived identity REQUIRED (fails closed when disabled).
  - Owner-hash enforced at the repository layer: a different user cannot
    retrieve another user's messages even if they know the frontend ID.
  - RESET/STALE/EXPIRED conversations return empty message list (deactivated).
  - No owner hash, process-local key, Genie ID, or internal identifier
    is present in any HTTP response body.
  - No raw exception traces in any response.
  - Message repository unavailable returns 503.
  - Feature disabled returns 503 (fail closed).

Response format:
  {
    "conversation_id": "<frontend_conversation_id>",
    "messages": [{...}, ...],
    "page": 1,
    "page_size": 50,
    "total_messages": 10,
    "has_more": false
  }

H1 -- TransparencE Conversation History Persistence
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.services.request_owner_identity_runtime import (
    REQUEST_OWNER_IDENTITY_STATE_ATTRIBUTE,
    RequestOwnerIdentityRuntimeConfigurationError,
    RequestOwnerIdentityRuntimeResolutionError,
    resolve_request_owner_identity,
)
from app.services.message_history_service import parse_response_payload
from app.services.message_repository import MAX_PAGE_SIZE, MessageRepositoryUnavailableError
from app.services.message_repository_runtime import get_message_repository

log = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class HistoryMessageItem(BaseModel):
    """Sanitized message item safe for frontend consumption.

    Absent fields are NOT included in the JSON response (None = excluded).
    No internal identifiers (owner hashes, Genie IDs, process-local keys)
    are present in this model.
    """
    id: str                              # opaque message UUID
    role: str                            # 'user' | 'assistant'
    content: str                         # plain-text message
    sequence: int
    timestamp: str                       # ISO-8601 UTC
    # Assistant-only fields (None for user messages)
    status: Optional[str] = None
    is_table: bool = False
    table_data: Optional[dict] = None
    row_count: int = 0
    suggested_questions: Optional[list] = None
    computed_chart_data: Optional[dict] = None
    computed_metrics: Optional[dict] = None
    query_description: Optional[str] = None
    has_visualization: bool = False
    source: Optional[str] = None

    class Config:
        # Exclude None-valued fields from JSON output
        json_encoders = {}


class HistoryResponse(BaseModel):
    conversation_id: str
    messages: List[HistoryMessageItem]
    page: int
    page_size: int
    total_messages: int
    has_more: bool


# ---------------------------------------------------------------------------
# Static sanitized error bodies
# ---------------------------------------------------------------------------

_RESP_IDENTITY_UNAVAILABLE = {
    "status": "error",
    "message": "Trusted request identity is unavailable.",
}
_RESP_IDENTITY_REQUIRED = {
    "status": "error",
    "message": "Trusted request identity is required.",
}
_RESP_INVALID_CONV = {
    "status": "error",
    "message": "Invalid conversation identifier.",
}
_RESP_HISTORY_UNAVAILABLE = {
    "status": "error",
    "message": "Conversation history is temporarily unavailable.",
}
_RESP_FEATURE_DISABLED = {
    "status": "error",
    "message": "Conversation history is not enabled.",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _canonicalize_frontend_id(raw: str) -> Optional[str]:
    """Return canonical frontend ID or None if invalid."""
    canonical = raw.strip()
    if not canonical:
        return None
    if "@" in canonical:
        return None
    return canonical


def _build_message_item(record: Any) -> HistoryMessageItem:
    """Map a MessageRecord to a sanitized HistoryMessageItem.

    For assistant messages, the response_payload_json is parsed and safe
    fields are extracted.  Internal identifiers are never forwarded.
    """
    timestamp = (
        record.created_at.isoformat().replace("+00:00", "Z")
        if record.created_at
        else ""
    )

    if record.role == "user":
        return HistoryMessageItem(
            id=record.message_id,
            role="user",
            content=record.message_text,
            sequence=record.message_sequence,
            timestamp=timestamp,
        )

    # Assistant message — parse stored payload
    payload = parse_response_payload(record.response_payload_json)
    return HistoryMessageItem(
        id=record.message_id,
        role="assistant",
        content=record.message_text,
        sequence=record.message_sequence,
        timestamp=timestamp,
        status=payload.get("status"),
        is_table=bool(payload.get("is_table", False)),
        table_data=payload.get("table_data"),
        row_count=int(payload.get("row_count", 0)),
        suggested_questions=payload.get("suggested_questions"),
        computed_chart_data=payload.get("computed_chart_data"),
        computed_metrics=payload.get("computed_metrics"),
        query_description=payload.get("query_description"),
        has_visualization=bool(payload.get("has_visualization", False)),
        source=payload.get("source"),
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/conversations/{frontend_conversation_id}/messages")
async def get_conversation_history(
    frontend_conversation_id: str,
    request: Request,
    page: int = Query(default=1, ge=1, description="1-based page number"),
    page_size: int = Query(
        default=50, ge=1, le=MAX_PAGE_SIZE, description="Records per page"
    ),
) -> JSONResponse:
    """Return paginated message history for an owner-scoped conversation.

    Security: trusted identity required; ownership enforced by repository layer.
    """

    # ------------------------------------------------------------------
    # Step 1: Resolve trusted identity
    # ------------------------------------------------------------------
    try:
        trusted_identity = resolve_request_owner_identity(headers=request.headers)
    except RequestOwnerIdentityRuntimeConfigurationError:
        return JSONResponse(status_code=503, content=_RESP_IDENTITY_UNAVAILABLE)
    except RequestOwnerIdentityRuntimeResolutionError:
        return JSONResponse(status_code=401, content=_RESP_IDENTITY_REQUIRED)
    except Exception:
        log.error("conversation_history: unexpected identity resolution failure")
        return JSONResponse(status_code=503, content=_RESP_IDENTITY_UNAVAILABLE)

    if trusted_identity is None:
        # Fail closed: trusted identity is disabled — cannot establish ownership
        return JSONResponse(status_code=503, content=_RESP_IDENTITY_UNAVAILABLE)

    setattr(request.state, REQUEST_OWNER_IDENTITY_STATE_ATTRIBUTE, trusted_identity)
    owner_hash: str = trusted_identity.owner_user_id_hash

    # ------------------------------------------------------------------
    # Step 2: Validate and canonicalize the frontend conversation ID
    # ------------------------------------------------------------------
    canonical_id = _canonicalize_frontend_id(frontend_conversation_id)
    if canonical_id is None:
        return JSONResponse(status_code=400, content=_RESP_INVALID_CONV)

    # ------------------------------------------------------------------
    # Step 3: Obtain message repository
    # ------------------------------------------------------------------
    repo = get_message_repository()
    if repo is None:
        # Feature disabled or Lakebase unconfigured — fail closed
        return JSONResponse(status_code=503, content=_RESP_FEATURE_DISABLED)

    # ------------------------------------------------------------------
    # Step 4: Query — owner-scoped; cross-user access prevented at repo layer
    # ------------------------------------------------------------------
    try:
        records, total = repo.list_messages(
            owner_user_id_hash=owner_hash,
            frontend_conversation_id=canonical_id,
            page=page,
            page_size=page_size,
        )
    except MessageRepositoryUnavailableError:
        log.error(
            "conversation_history: repository unavailable for frontend_id=%s",
            canonical_id[:8] + "...",
        )
        return JSONResponse(status_code=503, content=_RESP_HISTORY_UNAVAILABLE)
    except Exception:
        log.error(
            "conversation_history: unexpected error fetching messages for frontend_id=%s",
            canonical_id[:8] + "...",
        )
        return JSONResponse(status_code=503, content=_RESP_HISTORY_UNAVAILABLE)

    # ------------------------------------------------------------------
    # Step 5: Build sanitized response — no internal identifiers
    # ------------------------------------------------------------------
    message_items = [_build_message_item(rec) for rec in records]
    has_more = (page * page_size) < total

    response_body = HistoryResponse(
        conversation_id=canonical_id,
        messages=message_items,
        page=page,
        page_size=page_size,
        total_messages=total,
        has_more=has_more,
    )

    return JSONResponse(
        status_code=200,
        content=response_body.model_dump(exclude_none=True),
    )
