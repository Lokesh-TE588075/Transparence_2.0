"""Diagnostic trace persistence — writes trace records to a Delta table.

Design principles:
  - All writes are fire-and-forget on daemon threads.  A write failure logs
    a WARNING and never raises.
  - When TRANSPARENCE_DIAGNOSTIC_STORE=none the store is skipped entirely.
  - The Delta table is created on first write attempt (CREATE IF NOT EXISTS).
    Creation failure is cached so repeated attempts do not spam errors.
  - TTL cleanup (DELETE rows older than TTL_DAYS) runs at most once per 24h
    per process to avoid a hot path on every request.
  - No authentication tokens, cookies, emails, or raw row values are written.
    The caller (DiagnosticTrace) is responsible for hashing sensitive fields
    before passing them here.

Table schema:
  trace_id        STRING   — UUID4 per request
  trace_timestamp TIMESTAMP — write time
  trace_json      STRING   — full JSON trace record (all fields flattened)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Process-local state
# ---------------------------------------------------------------------------

_table_ensured: bool    = False          # set True after first successful CREATE
_table_ensure_lock      = threading.Lock()
_last_cleanup_ts: float = 0.0
_CLEANUP_INTERVAL_S     = 86400          # 24 h


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _get_store_type() -> str:
    try:
        from app.config import settings
        return str(getattr(settings, "TRANSPARENCE_DIAGNOSTIC_STORE", "delta")).lower()
    except Exception:
        return os.getenv("TRANSPARENCE_DIAGNOSTIC_STORE", "delta").lower()


def _get_table() -> str:
    default = "onedata_fn_ion_dev.ion_l0_raw.transparence_diagnostic_trace"
    try:
        from app.config import settings
        return getattr(settings, "TRANSPARENCE_DIAGNOSTIC_TABLE", default) or default
    except Exception:
        return os.getenv("TRANSPARENCE_DIAGNOSTIC_TABLE", default)


def _get_ttl_days() -> int:
    try:
        from app.config import settings
        return int(getattr(settings, "TRANSPARENCE_DIAGNOSTIC_TTL_DAYS", 7))
    except Exception:
        return int(os.getenv("TRANSPARENCE_DIAGNOSTIC_TTL_DAYS", "7"))


def _warehouse_id() -> str:
    """Extract warehouse ID from DATABRICKS_SQL_WAREHOUSE_PATH."""
    try:
        from app.config import settings
        path = settings.DATABRICKS_SQL_WAREHOUSE_PATH
    except Exception:
        path = os.getenv("DATABRICKS_SQL_WAREHOUSE_PATH", "")
    m = re.search(r"warehouses/([a-fA-F0-9]+)", path)
    return m.group(1) if m else ""


def _auth_token() -> str:
    token = os.getenv("DATABRICKS_TOKEN", "")
    if not token:
        try:
            from app.config import settings
            token = settings.DATABRICKS_TOKEN
        except Exception:
            pass
    if not token:
        try:
            from databricks.sdk import WorkspaceClient
            w = WorkspaceClient()
            headers = w.config.authenticate()
            for k, v in headers.items():
                if k.lower() == "authorization":
                    token = v.replace("Bearer ", "")
                    break
        except Exception:
            pass
    return token


def _host() -> str:
    try:
        from app.config import settings
        h = settings.DATABRICKS_HOST
    except Exception:
        h = os.getenv("DATABRICKS_HOST", "")
    if not h.startswith("http"):
        h = f"https://{h}"
    return h.rstrip("/")


# ---------------------------------------------------------------------------
# SQL execution via Statement Execution API
# ---------------------------------------------------------------------------

def _execute_sql(statement: str, parameters: Optional[list] = None, wait_timeout: str = "30s") -> bool:
    """Execute a SQL statement against the warehouse.  Returns True on success."""
    try:
        import requests as _req

        wid = _warehouse_id()
        if not wid:
            logger.warning("DiagnosticTraceStore: warehouse ID not configured")
            return False

        token = _auth_token()
        if not token:
            logger.warning("DiagnosticTraceStore: no auth token available")
            return False

        payload: Dict[str, Any] = {
            "warehouse_id":  wid,
            "statement":     statement,
            "wait_timeout":  wait_timeout,
            "on_wait_timeout": "CANCEL",
        }
        if parameters:
            payload["parameters"] = parameters

        resp = _req.post(
            f"{_host()}/api/2.0/sql/statements",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=35,
        )
        if resp.status_code >= 400:
            logger.warning(
                "DiagnosticTraceStore: SQL API returned %d: %s",
                resp.status_code, resp.text[:200],
            )
            return False

        result = resp.json()
        state  = result.get("status", {}).get("state", "")
        if state in ("FAILED", "CANCELED", "CLOSED"):
            err = result.get("status", {}).get("error", {}).get("message", "")
            logger.warning("DiagnosticTraceStore: statement %s: %s", state, err[:200])
            return False
        return True

    except Exception as e:
        logger.warning("DiagnosticTraceStore: _execute_sql error: %s", str(e)[:200])
        return False


# ---------------------------------------------------------------------------
# Table lifecycle
# ---------------------------------------------------------------------------

def _ensure_table() -> bool:
    """Create the diagnostic trace table if it does not exist.

    Caches success so this is attempted only once per process.
    """
    global _table_ensured
    if _table_ensured:
        return True
    with _table_ensure_lock:
        if _table_ensured:          # double-check after acquiring lock
            return True
        table = _get_table()
        ok = _execute_sql(
            f"""CREATE TABLE IF NOT EXISTS {table} (
                trace_id        STRING,
                trace_timestamp TIMESTAMP,
                trace_json      STRING
            ) USING DELTA""",
            wait_timeout="20s",
        )
        if ok:
            _table_ensured = True
            logger.info("DiagnosticTraceStore: table ready: %s", table)
        else:
            logger.warning("DiagnosticTraceStore: table creation failed for %s", table)
        return ok


def _maybe_cleanup() -> None:
    """Delete rows older than TTL_DAYS.  Runs at most once per 24h per process."""
    global _last_cleanup_ts
    now = time.time()
    if now - _last_cleanup_ts < _CLEANUP_INTERVAL_S:
        return
    with _table_ensure_lock:
        if time.time() - _last_cleanup_ts < _CLEANUP_INTERVAL_S:
            return
        _last_cleanup_ts = time.time()

    ttl_days = _get_ttl_days()
    table    = _get_table()
    ok = _execute_sql(
        f"DELETE FROM {table} WHERE trace_timestamp < CURRENT_TIMESTAMP() - INTERVAL {ttl_days} DAYS",
        wait_timeout="30s",
    )
    if ok:
        logger.info("DiagnosticTraceStore: TTL cleanup complete (ttl=%d days)", ttl_days)
    else:
        logger.warning("DiagnosticTraceStore: TTL cleanup failed")


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------

def _write_worker(record: Dict[str, Any]) -> None:
    """Background thread: ensure table + insert trace row + maybe cleanup."""
    try:
        if not _ensure_table():
            return

        table      = _get_table()
        trace_id   = str(record.get("trace_id", ""))
        trace_json = json.dumps(record, default=str, ensure_ascii=False)

        ok = _execute_sql(
            f"INSERT INTO {table} (trace_id, trace_timestamp, trace_json) "
            f"VALUES (:trace_id, CURRENT_TIMESTAMP(), :trace_json)",
            parameters=[
                {"name": "trace_id",   "value": trace_id,   "type": "STRING"},
                {"name": "trace_json", "value": trace_json, "type": "STRING"},
            ],
            wait_timeout="15s",
        )
        if ok:
            logger.debug("DiagnosticTraceStore: trace written trace_id=%s", trace_id)

        _maybe_cleanup()

    except Exception as e:
        logger.warning(
            "DiagnosticTraceStore: worker failed for trace_id=%s: %s",
            record.get("trace_id", "?"), str(e)[:200],
        )


def write_trace_async(record: Dict[str, Any]) -> None:
    """Write a trace record asynchronously.  Never raises.

    When TRANSPARENCE_DIAGNOSTIC_STORE=none, records are only logged at DEBUG
    level (no Delta write, useful for local development).
    """
    if not record:
        return
    store = _get_store_type()
    if store == "none":
        logger.debug(
            "DIAG_TRACE_NOOP trace_id=%s stages=%s",
            record.get("trace_id"), [k for k in record if k.startswith("stage_")],
        )
        return

    # "log" store: write summary to INFO log only (no Delta, no threads)
    if store == "log":
        logger.info(
            "DIAG_TRACE trace_id=%s startup_id=%s route=%s shape_valid=%s final_source=%s",
            record.get("trace_id"),
            record.get("application_startup_id"),
            record.get("route_intent"),
            record.get("first_contract_contract_valid"),
            record.get("final_source"),
        )
        return

    # "delta" store (default): async write
    t = threading.Thread(target=_write_worker, args=(record,), daemon=True)
    t.start()


def reset_table_cache_for_testing() -> None:
    """Reset process-local table-creation cache.  For use in tests only."""
    global _table_ensured, _last_cleanup_ts
    _table_ensured    = False
    _last_cleanup_ts  = 0.0
