"""Genie session store.

Maps app-level ``conversation_id`` values to Genie-side conversation and message
IDs while also keeping lightweight app-side business context needed for robust
routing, corrections, downloads, and follow-up handling.

Design:
  - In-memory store with TTL-based expiry.
  - Delta-ready: ``serialize_session`` / ``deserialize_session`` produce plain
    dicts that can later be written to a Delta table row-for-row.
  - Intentionally separate from the custom pipeline's ConversationManager and
    ConversationState classes. Genie conversation state is Genie-side; we only
    track the mapping plus a concise per-conversation context snapshot.
  - Thread-safe locking for concurrent FastAPI workers (in-memory backend).

Contract 7 (message-level export context):
  ``latest_table_result`` stores the export metadata for the most recent table
  response in a conversation.  Unlike the flat ``last_download_key`` slot,
  ``latest_table_result`` is ALWAYS overwritten whenever a new table response
  arrives — even when the export creation silently failed (download_key=None).
  This prevents the stale-download bug where an old 2-row export was served
  after a subsequent 500-row query whose export failed silently.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_TTL_HOURS = 24


@dataclass
class TableExportRecord:
    """Per-table export metadata — one record per assistant table message.

    Created by GeniePipeline after Step 9 (export creation) and stored in the
    session as ``latest_table_result``.  The field ``download_key`` is None
    when the export creation failed silently.

    The ``assistant_message_id`` is the Genie message_id for this turn; it
    provides a stable anchor so tests and audits can correlate the export with
    the exact Genie response.
    """
    assistant_message_id: str
    download_key: Optional[str]
    export_id: Optional[str]
    export_status: Optional[str]
    export_mode: Optional[str]
    export_row_count: Optional[int]
    query_description: Optional[str]
    created_at: datetime

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assistant_message_id": self.assistant_message_id,
            "download_key": self.download_key,
            "export_id": self.export_id,
            "export_status": self.export_status,
            "export_mode": self.export_mode,
            "export_row_count": self.export_row_count,
            "query_description": self.query_description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@dataclass
class GenieSession:
    """Single Genie conversation session mapped to one app conversation."""

    app_conversation_id: str
    genie_conversation_id: Optional[str]
    last_genie_message_id: Optional[str]
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    is_active: bool = True

    last_entities: List[str] = field(default_factory=list)
    last_entity_type: Optional[str] = None
    last_filters: Dict[str, Any] = field(default_factory=dict)
    last_intent: Optional[str] = None
    last_user_prompt: Optional[str] = None
    last_enriched_prompt: Optional[str] = None
    last_download_key: Optional[str] = None
    last_export_id: Optional[str] = None
    last_export_status: Optional[str] = None
    last_export_mode: Optional[str] = None
    last_export_row_count: Optional[int] = None
    last_table_headers: List[str] = field(default_factory=list)
    last_row_count: Optional[int] = None
    last_total_row_count: Optional[int] = None
    last_returned_row_count: Optional[int] = None

    # Contract 7: message-level export context.
    # Always overwritten when a new table response arrives (no None-guard).
    latest_table_result: Optional[TableExportRecord] = None

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        _now = now or datetime.now(timezone.utc)
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return _now >= exp


class GenieSessionStore:
    """In-memory store mapping app conversation IDs to Genie conversation IDs."""

    def __init__(self, ttl_hours: int = _DEFAULT_TTL_HOURS):
        self._ttl_hours = ttl_hours
        self._sessions: Dict[str, GenieSession] = {}
        self._lock = threading.Lock()

    def _expiry(self, now: datetime) -> datetime:
        return now + timedelta(hours=self._ttl_hours)

    def _new_session(self, app_conversation_id: str, now: Optional[datetime] = None) -> GenieSession:
        now = now or datetime.now(timezone.utc)
        return GenieSession(
            app_conversation_id=app_conversation_id,
            genie_conversation_id=None,
            last_genie_message_id=None,
            created_at=now,
            updated_at=now,
            expires_at=self._expiry(now),
            is_active=True,
        )

    def _get_or_create_session(self, app_conversation_id: str) -> GenieSession:
        now = datetime.now(timezone.utc)
        session = self._sessions.get(app_conversation_id)
        if session is None or session.is_expired(now) or not session.is_active:
            session = self._new_session(app_conversation_id, now)
            self._sessions[app_conversation_id] = session
            logger.debug(
                "GenieSessionStore: created session for app_conv=%s",
                app_conversation_id,
            )
            return session

        session.updated_at = now
        session.expires_at = self._expiry(now)
        return session

    def get_genie_conversation_id(self, app_conversation_id: str) -> Optional[str]:
        session = self.get_session(app_conversation_id)
        if session is None:
            return None
        return session.genie_conversation_id

    def set_genie_conversation_id(self, app_conversation_id: str, genie_conversation_id: str) -> None:
        with self._lock:
            session = self._get_or_create_session(app_conversation_id)
            session.genie_conversation_id = genie_conversation_id
            self._sessions[app_conversation_id] = session

    def get_last_message_id(self, app_conversation_id: str) -> Optional[str]:
        session = self.get_session(app_conversation_id)
        if session is None:
            return None
        return session.last_genie_message_id

    def set_last_message_id(self, app_conversation_id: str, message_id: str) -> None:
        with self._lock:
            session = self._get_or_create_session(app_conversation_id)
            session.last_genie_message_id = message_id
            self._sessions[app_conversation_id] = session

    def update_context(
        self,
        app_conversation_id: str,
        *,
        last_entities: Optional[List[str]] = None,
        last_entity_type: Optional[str] = None,
        last_filters: Optional[Dict[str, Any]] = None,
        last_intent: Optional[str] = None,
        last_user_prompt: Optional[str] = None,
        last_enriched_prompt: Optional[str] = None,
        last_download_key: Optional[str] = None,
        last_export_id: Optional[str] = None,
        last_export_status: Optional[str] = None,
        last_export_mode: Optional[str] = None,
        last_export_row_count: Optional[int] = None,
        last_table_headers: Optional[List[str]] = None,
        last_row_count: Optional[int] = None,
        last_total_row_count: Optional[int] = None,
        last_returned_row_count: Optional[int] = None,
        latest_table_result: Optional[TableExportRecord] = None,
    ) -> None:
        """Update the app-side context snapshot for a conversation.

        All flat last_* parameters use ``if value is not None`` guards so that
        passing None does not clear an existing value (backward-compatible behaviour).

        ``latest_table_result`` is the EXCEPTION: it is ALWAYS written when provided,
        even when ``download_key`` is None, so that a new table response with a failed
        export correctly replaces stale export context from a prior turn.  A sentinel
        value of ``_CLEAR_TABLE_RESULT`` (the TableExportRecord itself, even with
        download_key=None) signals "new table, no export" — which is still better than
        serving the old export.
        """
        with self._lock:
            session = self._get_or_create_session(app_conversation_id)

            if last_entities is not None:
                session.last_entities = list(last_entities)
            if last_entity_type is not None:
                session.last_entity_type = last_entity_type
            if last_filters is not None:
                session.last_filters = dict(last_filters)
            if last_intent is not None:
                session.last_intent = last_intent
            if last_user_prompt is not None:
                session.last_user_prompt = last_user_prompt
            if last_enriched_prompt is not None:
                session.last_enriched_prompt = last_enriched_prompt
            if last_download_key is not None:
                session.last_download_key = last_download_key
            if last_export_id is not None:
                session.last_export_id = last_export_id
            if last_export_status is not None:
                session.last_export_status = last_export_status
            if last_export_mode is not None:
                session.last_export_mode = last_export_mode
            if last_export_row_count is not None:
                session.last_export_row_count = last_export_row_count
            if last_table_headers is not None:
                session.last_table_headers = list(last_table_headers)
            if last_row_count is not None:
                session.last_row_count = last_row_count
            if last_total_row_count is not None:
                session.last_total_row_count = last_total_row_count
            if last_returned_row_count is not None:
                session.last_returned_row_count = last_returned_row_count

            # Contract 7: always overwrite latest_table_result when a new table
            # response arrives — regardless of whether download_key is present.
            # This prevents stale export context from persisting across turns.
            if latest_table_result is not None:
                session.latest_table_result = latest_table_result

            self._sessions[app_conversation_id] = session

    def get_context_snapshot(self, app_conversation_id: str) -> Dict[str, Any]:
        session = self.get_session(app_conversation_id)
        if session is None:
            return {}
        ltr = session.latest_table_result
        return {
            "genie_conversation_id": session.genie_conversation_id,
            "last_genie_message_id": session.last_genie_message_id,
            "last_entities": list(session.last_entities),
            "last_entity_type": session.last_entity_type,
            "last_filters": dict(session.last_filters),
            "last_intent": session.last_intent,
            "last_user_prompt": session.last_user_prompt,
            "last_enriched_prompt": session.last_enriched_prompt,
            "last_download_key": session.last_download_key,
            "last_export_id": session.last_export_id,
            "last_export_status": session.last_export_status,
            "last_export_mode": session.last_export_mode,
            "last_export_row_count": session.last_export_row_count,
            "last_table_headers": list(session.last_table_headers),
            "last_row_count": session.last_row_count,
            "last_total_row_count": session.last_total_row_count,
            "last_returned_row_count": session.last_returned_row_count,
            "updated_at": session.updated_at,
            "expires_at": session.expires_at,
            # Contract 7: message-level export context
            "latest_table_result": ltr.to_dict() if ltr is not None else None,
        }

    def get_last_download_key(self, app_conversation_id: str) -> Optional[str]:
        session = self.get_session(app_conversation_id)
        if session is None:
            return None
        return session.last_download_key

    def get_session(self, app_conversation_id: str) -> Optional[GenieSession]:
        with self._lock:
            session = self._sessions.get(app_conversation_id)

        if session is None:
            return None

        if session.is_expired():
            logger.debug(
                "GenieSessionStore: session expired for app_conv=%s",
                app_conversation_id,
            )
            return None

        if not session.is_active:
            return None

        return session

    def reset_session(self, app_conversation_id: str) -> None:
        """Deactivate and clear the Genie session for an app conversation."""
        with self._lock:
            session = self._sessions.get(app_conversation_id)
            if session is not None:
                session.is_active = False
                session.genie_conversation_id = None
                session.last_genie_message_id = None
                session.updated_at = datetime.now(timezone.utc)
                self._sessions[app_conversation_id] = session

        logger.debug(
            "GenieSessionStore: reset session for app_conv=%s",
            app_conversation_id,
        )

    def reset_genie_mapping(self, app_conversation_id: str) -> None:
        """Clear only Genie IDs while preserving app-side business context."""
        with self._lock:
            session = self._sessions.get(app_conversation_id)
            if session is not None:
                now = datetime.now(timezone.utc)
                session.genie_conversation_id = None
                session.last_genie_message_id = None
                session.updated_at = now
                session.expires_at = self._expiry(now)
                session.is_active = True
                self._sessions[app_conversation_id] = session

        logger.debug(
            "GenieSessionStore: reset Genie mapping for app_conv=%s",
            app_conversation_id,
        )

    def cleanup_expired_sessions(self) -> int:
        now = datetime.now(timezone.utc)
        to_remove = []

        with self._lock:
            for app_conv_id, session in list(self._sessions.items()):
                if session.is_expired(now) or not session.is_active:
                    to_remove.append(app_conv_id)

            for app_conv_id in to_remove:
                del self._sessions[app_conv_id]

        if to_remove:
            logger.debug(
                "GenieSessionStore: cleaned up %d expired sessions",
                len(to_remove),
            )

        return len(to_remove)

    def session_count(self) -> int:
        """Return the total number of sessions currently in the store.

        Includes sessions that may be expired or inactive but have not yet
        been cleaned up.  Use ``cleanup_expired_sessions()`` first if you want
        only the live sessions, then call this method.
        """
        with self._lock:
            return len(self._sessions)

    def active_session_count(self) -> int:
        """Return the count of sessions that are both active and not yet expired."""
        now = datetime.now(timezone.utc)
        with self._lock:
            return sum(
                1 for s in self._sessions.values()
                if s.is_active and not s.is_expired(now)
            )

    def serialize_session(self, session: GenieSession) -> Dict[str, Any]:
        def _dt(dt: Optional[datetime]) -> Optional[str]:
            if dt is None:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()

        ltr = session.latest_table_result
        return {
            "app_conversation_id": session.app_conversation_id,
            "genie_conversation_id": session.genie_conversation_id,
            "last_genie_message_id": session.last_genie_message_id,
            "created_at": _dt(session.created_at),
            "updated_at": _dt(session.updated_at),
            "expires_at": _dt(session.expires_at),
            "is_active": session.is_active,
            "last_entities": list(session.last_entities),
            "last_entity_type": session.last_entity_type,
            "last_filters": dict(session.last_filters),
            "last_intent": session.last_intent,
            "last_user_prompt": session.last_user_prompt,
            "last_enriched_prompt": session.last_enriched_prompt,
            "last_download_key": session.last_download_key,
            "last_export_id": session.last_export_id,
            "last_export_status": session.last_export_status,
            "last_export_mode": session.last_export_mode,
            "last_export_row_count": session.last_export_row_count,
            "last_table_headers": list(session.last_table_headers),
            "last_row_count": session.last_row_count,
            "last_total_row_count": session.last_total_row_count,
            "last_returned_row_count": session.last_returned_row_count,
            "latest_table_result": ltr.to_dict() if ltr is not None else None,
        }

    def deserialize_session(self, data: Dict[str, Any]) -> GenieSession:
        def _parse_dt(value: Any) -> datetime:
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    return value.replace(tzinfo=timezone.utc)
                return value
            if value is None:
                return datetime.now(timezone.utc)
            dt = datetime.fromisoformat(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        ltr_data = data.get("latest_table_result")
        ltr = None
        if ltr_data:
            ltr = TableExportRecord(
                assistant_message_id=ltr_data.get("assistant_message_id", ""),
                download_key=ltr_data.get("download_key"),
                export_id=ltr_data.get("export_id"),
                export_status=ltr_data.get("export_status"),
                export_mode=ltr_data.get("export_mode"),
                export_row_count=ltr_data.get("export_row_count"),
                query_description=ltr_data.get("query_description"),
                created_at=_parse_dt(ltr_data.get("created_at")),
            )

        return GenieSession(
            app_conversation_id=data["app_conversation_id"],
            genie_conversation_id=data.get("genie_conversation_id"),
            last_genie_message_id=data.get("last_genie_message_id"),
            created_at=_parse_dt(data.get("created_at")),
            updated_at=_parse_dt(data.get("updated_at")),
            expires_at=_parse_dt(data.get("expires_at")),
            is_active=data.get("is_active", True),
            last_entities=list(data.get("last_entities", [])),
            last_entity_type=data.get("last_entity_type"),
            last_filters=dict(data.get("last_filters", {})),
            last_intent=data.get("last_intent"),
            last_user_prompt=data.get("last_user_prompt"),
            last_enriched_prompt=data.get("last_enriched_prompt"),
            last_download_key=data.get("last_download_key"),
            last_export_id=data.get("last_export_id"),
            last_export_status=data.get("last_export_status"),
            last_export_mode=data.get("last_export_mode"),
            last_export_row_count=data.get("last_export_row_count"),
            last_table_headers=list(data.get("last_table_headers", [])),
            last_row_count=data.get("last_row_count"),
            last_total_row_count=data.get("last_total_row_count"),
            last_returned_row_count=data.get("last_returned_row_count"),
            latest_table_result=ltr,
        )
