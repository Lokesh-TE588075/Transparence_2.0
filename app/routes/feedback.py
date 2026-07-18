"""Feedback route for user ratings."""

import logging
from fastapi import APIRouter, Request
from pydantic import BaseModel
from typing import Optional

from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)
router = APIRouter()


class FeedbackRequest(BaseModel):
    """Feedback submission payload."""
    rating: int  # 1 (thumbs down) or 5 (thumbs up)
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    comment: Optional[str] = None
    user_query: Optional[str] = None
    sql_generated: Optional[str] = None


@router.post("/feedback")
async def submit_feedback(request: Request, body: FeedbackRequest):
    """Submit feedback on a chatbot response."""
    # Get user from session or header
    user_id = request.headers.get("X-User-Email", "anonymous")

    audit_svc = AuditService()
    feedback_id = audit_svc.submit_feedback(
        user_id=user_id,
        rating=body.rating,
        conversation_id=body.conversation_id,
        message_id=body.message_id,
        comment=body.comment,
        user_query=body.user_query,
        sql_generated=body.sql_generated,
    )

    return {"status": "success", "feedback_id": feedback_id}
