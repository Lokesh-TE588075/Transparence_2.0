"""In-memory export job manager for async Genie CSV exports.

Designed for single-worker UAT use today, with plain-dict serialization hooks so
it can be swapped to Delta-backed persistence later without changing callers.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional


@dataclass
class ExportJob:
    export_id: str
    app_conversation_id: str
    source: str
    status: str
    mode: str
    file_path: Optional[str]
    download_key: Optional[str]
    row_count: Optional[int]
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class ExportJobManager:
    """Thread-safe in-memory store for export job lifecycle state."""

    def __init__(self, ttl_hours: int = 24):
        self._ttl_hours = ttl_hours
        self._jobs: Dict[str, ExportJob] = {}
        self._lock = threading.Lock()

    def _expiry(self, now: datetime) -> datetime:
        return now + timedelta(hours=self._ttl_hours)

    def create_job(
        self,
        *,
        app_conversation_id: str,
        source: str = "genie",
        mode: str = "returned_rows_only",
    ) -> ExportJob:
        now = datetime.now(timezone.utc)
        job = ExportJob(
            export_id=str(uuid.uuid4()),
            app_conversation_id=app_conversation_id,
            source=source,
            status="queued",
            mode=mode,
            file_path=None,
            download_key=None,
            row_count=None,
            error_message=None,
            created_at=now,
            updated_at=now,
            expires_at=self._expiry(now),
        )
        with self._lock:
            self._jobs[job.export_id] = job
        return job

    def mark_running(self, export_id: str) -> Optional[ExportJob]:
        with self._lock:
            job = self._jobs.get(export_id)
            if job is None:
                return None
            now = datetime.now(timezone.utc)
            job.status = "running"
            job.updated_at = now
            job.expires_at = self._expiry(now)
            return job

    def mark_ready(
        self,
        export_id: str,
        *,
        file_path: str,
        download_key: str,
        row_count: int,
    ) -> Optional[ExportJob]:
        with self._lock:
            job = self._jobs.get(export_id)
            if job is None:
                return None
            now = datetime.now(timezone.utc)
            job.status = "ready"
            job.file_path = file_path
            job.download_key = download_key
            job.row_count = row_count
            job.error_message = None
            job.updated_at = now
            job.expires_at = self._expiry(now)
            return job

    def mark_failed(self, export_id: str, error_message: str) -> Optional[ExportJob]:
        with self._lock:
            job = self._jobs.get(export_id)
            if job is None:
                return None
            now = datetime.now(timezone.utc)
            job.status = "failed"
            job.error_message = error_message
            job.updated_at = now
            job.expires_at = self._expiry(now)
            return job

    def get_job(self, export_id: str) -> Optional[ExportJob]:
        with self._lock:
            job = self._jobs.get(export_id)
        if job is None:
            return None
        now = datetime.now(timezone.utc)
        if now >= job.expires_at:
            with self._lock:
                self._jobs.pop(export_id, None)
            return None
        return job

    def cleanup_expired_jobs(self) -> int:
        now = datetime.now(timezone.utc)
        with self._lock:
            expired = [job_id for job_id, job in self._jobs.items() if now >= job.expires_at]
            for job_id in expired:
                self._jobs.pop(job_id, None)
        return len(expired)

    def serialize_job(self, job: ExportJob) -> dict:
        return {
            "export_id": job.export_id,
            "app_conversation_id": job.app_conversation_id,
            "source": job.source,
            "status": job.status,
            "mode": job.mode,
            "file_path": job.file_path,
            "download_key": job.download_key,
            "row_count": job.row_count,
            "error_message": job.error_message,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
            "expires_at": job.expires_at.isoformat(),
        }

    def deserialize_job(self, data: dict) -> ExportJob:
        def _dt(value: str) -> datetime:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        return ExportJob(
            export_id=data["export_id"],
            app_conversation_id=data["app_conversation_id"],
            source=data.get("source", "genie"),
            status=data.get("status", "queued"),
            mode=data.get("mode", "returned_rows_only"),
            file_path=data.get("file_path"),
            download_key=data.get("download_key"),
            row_count=data.get("row_count"),
            error_message=data.get("error_message"),
            created_at=_dt(data["created_at"]),
            updated_at=_dt(data["updated_at"]),
            expires_at=_dt(data["expires_at"]),
        )


_lock = threading.Lock()
_manager: Optional[ExportJobManager] = None


def get_export_job_manager(ttl_hours: int = 24) -> ExportJobManager:
    global _manager
    if _manager is None:
        with _lock:
            if _manager is None:
                _manager = ExportJobManager(ttl_hours=ttl_hours)
    return _manager


def reset_export_job_manager() -> None:
    global _manager
    with _lock:
        _manager = None
