"""Pydantic request/response schemas for all API endpoints."""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# --- Chat ---

class ChatRequest(BaseModel):
    """Chat message from user."""
    message: str = Field(..., min_length=1, max_length=2000, description="User's natural language query")
    conversation_id: Optional[str] = Field(None, description="Existing conversation ID for context")


class TableData(BaseModel):
    """Tabular result set."""
    headers: List[str]
    rows: List[List[Any]]


class ChatResponse(BaseModel):
    """Chat response from system."""
    status: str = Field(..., description="success or error")
    message: str = Field(..., description="Human-readable response text")
    is_table: bool = Field(False, description="Whether table_data is populated")
    table_data: Optional[TableData] = None
    row_count: int = Field(0, description="Total rows in result (may exceed table_data if truncated)")
    download_key: Optional[str] = Field(None, description="Key for CSV export download")
    execution_time_ms: int = Field(0, description="SQL execution time in milliseconds")
    clarification: Optional[str] = Field(None, description="Follow-up clarification question if query was ambiguous")


# --- Feedback ---

class FeedbackRequest(BaseModel):
    """User feedback on a response."""
    conversation_id: str
    message_id: Optional[str] = None
    rating: str = Field(..., description="thumbs_up or thumbs_down")
    comment: Optional[str] = Field(None, max_length=1000)
    user_prompt: Optional[str] = None
    bot_response: Optional[str] = None


# --- Conversations ---

class ConversationSummary(BaseModel):
    """Conversation list item."""
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int = 0


class MessageItem(BaseModel):
    """Single message in a conversation."""
    id: str
    role: str  # user | assistant
    content: str
    sql_generated: Optional[str] = None
    created_at: str
