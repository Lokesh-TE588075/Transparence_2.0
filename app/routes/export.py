"""Export route for CSV downloads."""

import logging
import os
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.services.audit_service import AuditService
from app.services.export_job_manager import get_export_job_manager

logger = logging.getLogger(__name__)
router = APIRouter()


def _status_message(status: str, mode: str | None, row_count: int | None) -> str:
    if status == "queued":
        return "CSV export is queued."
    if status == "running":
        return "CSV export is being prepared."
    if status == "ready":
        if mode == "returned_rows_only":
            return (
                f"Download includes {row_count} available returned result rows."
                if row_count is not None
                else "Download includes the available returned result rows."
            )
        return (
            f"Download includes {row_count} rows."
            if row_count is not None
            else "CSV export is ready."
        )
    if status == "failed":
        return "CSV export failed. Try narrowing the query."
    return "Export status unavailable."


@router.get("/export/status/{export_id}")
async def export_status(export_id: str):
    """Return the current async export lifecycle state."""
    manager = get_export_job_manager()
    manager.cleanup_expired_jobs()
    job = manager.get_job(export_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Export not found or expired")
    return {
        "export_id": job.export_id,
        "status": job.status,
        "row_count": job.row_count,
        "download_key": job.download_key,
        "message": _status_message(job.status, job.mode, job.row_count),
        "mode": job.mode,
    }


@router.get("/download/{download_key}/ready")
async def check_download_ready(download_key: str):
    """Check if a CSV export is ready for download."""
    audit_svc = AuditService()
    ready = audit_svc.is_export_ready(download_key)
    return {"ready": ready, "download_key": download_key}


@router.get("/download/{download_key}")
async def download_file(download_key: str):
    """Download an exported CSV file."""
    audit_svc = AuditService()
    filepath = audit_svc.get_export_path(download_key)

    if not filepath:
        raise HTTPException(status_code=404, detail="Export not found or expired")

    filename = os.path.basename(filepath)
    return FileResponse(
        path=filepath,
        media_type="text/csv",
        filename=filename,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
