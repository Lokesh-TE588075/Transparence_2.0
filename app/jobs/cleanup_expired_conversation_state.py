"""Cleanup expired conversation state sessions from Delta.

Standalone job script — runs as a Databricks Job with ZERO external dependencies
beyond the Databricks SDK (pre-installed on serverless compute).

Usage:
    # Scheduled via Databricks Jobs (every 6 hours)
    # Or import and call:
    from app.jobs.cleanup_expired_conversation_state import run_cleanup
    run_cleanup()

Behavior:
    - Default (soft delete): marks expired sessions as is_active=false
    - Hard delete (CONVERSATION_STATE_CLEANUP_HARD_DELETE=true env var): removes rows
    - Logs results to stdout (captured by Databricks Jobs)
    - Never crashes — all errors are caught and logged
    - Active sessions are never modified

Environment variables (all optional with safe defaults):
    CONVERSATION_STATE_TABLE_NAME  (default: onedata_fn_ion_dev.ion_l0_raw.shipmate_conversation_state)
    CONVERSATION_STATE_TTL_HOURS   (default: 24)
    CONVERSATION_STATE_CLEANUP_HARD_DELETE (default: false)
    DATABRICKS_SQL_WAREHOUSE_PATH  (default: /sql/1.0/warehouses/8e46614f7064d8fd)
"""

import logging
import os
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cleanup_expired_sessions")

# --- Configuration (read from env, no pydantic dependency) ---
TABLE_NAME = os.getenv(
    "CONVERSATION_STATE_TABLE_NAME",
    "onedata_fn_ion_dev.ion_l0_raw.shipmate_conversation_state",
)
TTL_HOURS = int(os.getenv("CONVERSATION_STATE_TTL_HOURS", "24"))
HARD_DELETE = os.getenv(
    "CONVERSATION_STATE_CLEANUP_HARD_DELETE", "false"
).lower() in ("true", "1", "yes")
WAREHOUSE_PATH = os.getenv(
    "DATABRICKS_SQL_WAREHOUSE_PATH", "/sql/1.0/warehouses/8e46614f7064d8fd"
)
WAREHOUSE_ID = WAREHOUSE_PATH.rstrip("/").split("/")[-1]


def _execute_sql(sql: str) -> dict:
    """Execute SQL via Databricks SDK Statement Execution API."""
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import StatementState

    w = WorkspaceClient()
    response = w.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE_ID,
        statement=sql,
        wait_timeout="30s",
    )
    if response.status and response.status.state == StatementState.FAILED:
        error_msg = ""
        if response.status.error:
            error_msg = response.status.error.message or str(response.status.error)
        return {"rows": [], "error": error_msg}

    rows = []
    if response.result and response.result.data_array:
        columns = []
        if response.manifest and response.manifest.schema and response.manifest.schema.columns:
            columns = [c.name for c in response.manifest.schema.columns]
        for row_data in response.result.data_array:
            rows.append(dict(zip(columns, row_data)) if columns else row_data)

    return {"rows": rows, "error": None}


def run_cleanup() -> dict:
    """Run the cleanup process.

    Returns:
        dict with keys: status, soft_deleted, hard_deleted, error
    """
    result = {
        "status": "success",
        "soft_deleted": 0,
        "hard_deleted": 0,
        "error": None,
        "duration_ms": 0,
    }
    start = time.time()

    try:
        logger.info("=" * 60)
        logger.info("CONVERSATION STATE CLEANUP JOB")
        logger.info("=" * 60)
        logger.info(f"  Table: {TABLE_NAME}")
        logger.info(f"  TTL hours: {TTL_HOURS}")
        logger.info(f"  Hard delete: {HARD_DELETE}")
        logger.info(f"  Warehouse ID: {WAREHOUSE_ID}")
        logger.info("=" * 60)

        # Step 1: Soft delete — mark expired sessions inactive
        soft_sql = f"""
        UPDATE {TABLE_NAME}
        SET is_active = false
        WHERE is_active = true AND expires_at < CURRENT_TIMESTAMP()
        """
        logger.info("Running soft cleanup (mark inactive)...")
        soft_result = _execute_sql(soft_sql)

        if soft_result["error"]:
            logger.warning(f"Soft cleanup returned error: {soft_result['error'][:200]}")
        else:
            logger.info("Soft cleanup completed successfully")

        # Try to count affected rows
        count_sql = f"""
        SELECT COUNT(*) as cnt
        FROM {TABLE_NAME}
        WHERE is_active = false AND expires_at < CURRENT_TIMESTAMP()
        """
        count_result = _execute_sql(count_sql)
        if count_result["rows"]:
            row = count_result["rows"][0]
            cnt = int(row.get("cnt", 0) if isinstance(row, dict) else row[0])
            result["soft_deleted"] = cnt
            logger.info(f"Inactive expired sessions: {cnt}")

        # Step 2: Hard delete (only if explicitly enabled)
        if HARD_DELETE:
            logger.info("Hard delete enabled — removing inactive expired rows...")
            hard_sql = f"""
            DELETE FROM {TABLE_NAME}
            WHERE is_active = false AND expires_at < CURRENT_TIMESTAMP()
            """
            hard_result = _execute_sql(hard_sql)
            if hard_result["error"]:
                logger.warning(f"Hard delete error: {hard_result['error'][:200]}")
            else:
                result["hard_deleted"] = result["soft_deleted"]
                logger.info(f"Hard deleted {result['hard_deleted']} rows")
        else:
            logger.info("Hard delete disabled (default). Expired rows marked inactive only.")

        # Step 3: Report active sessions
        active_sql = f"""
        SELECT COUNT(*) as cnt
        FROM {TABLE_NAME}
        WHERE is_active = true
        """
        active_result = _execute_sql(active_sql)
        if active_result["rows"]:
            row = active_result["rows"][0]
            active_cnt = int(row.get("cnt", 0) if isinstance(row, dict) else row[0])
            logger.info(f"Active sessions remaining: {active_cnt}")

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)[:500]
        logger.error(f"Cleanup job failed: {e}", exc_info=True)

    result["duration_ms"] = int((time.time() - start) * 1000)
    logger.info(f"Cleanup completed in {result['duration_ms']}ms — status: {result['status']}")
    return result


if __name__ == "__main__":
    run_cleanup()
