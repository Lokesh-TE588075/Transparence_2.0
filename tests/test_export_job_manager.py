import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(
    0, "/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app"
)

from app.services.export_job_manager import ExportJobManager


class TestExportJobManager:
    def test_create_job_defaults_to_queued(self):
        mgr = ExportJobManager(ttl_hours=24)
        job = mgr.create_job(app_conversation_id="conv-1", mode="returned_rows_only")

        assert job.export_id
        assert job.status == "queued"
        assert job.mode == "returned_rows_only"
        assert job.source == "genie"

    def test_job_lifecycle_queued_running_ready(self):
        mgr = ExportJobManager(ttl_hours=24)
        job = mgr.create_job(app_conversation_id="conv-1", mode="returned_rows_only")

        mgr.mark_running(job.export_id)
        running = mgr.get_job(job.export_id)
        assert running.status == "running"

        mgr.mark_ready(
            job.export_id,
            file_path="/tmp/export.csv",
            download_key="dl-1",
            row_count=123,
        )
        ready = mgr.get_job(job.export_id)
        assert ready.status == "ready"
        assert ready.file_path == "/tmp/export.csv"
        assert ready.download_key == "dl-1"
        assert ready.row_count == 123

    def test_job_failure_state(self):
        mgr = ExportJobManager(ttl_hours=24)
        job = mgr.create_job(app_conversation_id="conv-1", mode="async_full_query")

        mgr.mark_failed(job.export_id, "unsafe SQL")
        failed = mgr.get_job(job.export_id)
        assert failed.status == "failed"
        assert failed.error_message == "unsafe SQL"

    def test_cleanup_expired_jobs(self):
        mgr = ExportJobManager(ttl_hours=24)
        job = mgr.create_job(app_conversation_id="conv-1")
        mgr._jobs[job.export_id].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        removed = mgr.cleanup_expired_jobs()
        assert removed == 1
        assert mgr.get_job(job.export_id) is None

    def test_serialize_deserialize_roundtrip(self):
        mgr = ExportJobManager(ttl_hours=24)
        job = mgr.create_job(app_conversation_id="conv-1", mode="async_full_query")
        mgr.mark_ready(job.export_id, file_path="/tmp/a.csv", download_key="dl-2", row_count=77)
        ready = mgr.get_job(job.export_id)

        payload = mgr.serialize_job(ready)
        restored = mgr.deserialize_job(payload)

        assert restored.export_id == ready.export_id
        assert restored.status == "ready"
        assert restored.download_key == "dl-2"
        assert restored.row_count == 77
