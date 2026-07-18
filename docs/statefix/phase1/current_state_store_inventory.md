# Current State Store Inventory — Phase 1 Investigation

**Date:** 2026-07-18  
**Branch:** feature/genie-state-persistence  
**Baseline commit:** 7d9977ff5965b8ee015bda1cc1662a317ae8a0d0

---

## Summary Matrix

| Store | Data held | Storage type | Process-safe? | Restart-safe? | Multi-replica-safe? | Authoritative? |
|---|---|---|---|---|---|---|
| `GenieSessionStore` | genie_conv_id, last_message_id, intent, entities, export state, prompts | In-memory dict (Python) | Yes (threading.Lock) | **NO** | **NO** | YES (sole source of Genie mapping) |
| `ConversationManager` | Conversation rows, message rows | Delta table (Unity Catalog) | Yes (SDK) | **YES** | Yes (Delta ACID) | YES (for old pipeline chat history) |
| `ExportJobManager` | export_id, status, file_path, download_key | In-memory dict (Python) | Yes (threading.Lock) | **NO** | **NO** | YES (sole source of async export status) |
| CSV export files | Downloaded CSV data | `/tmp/chatbot_exports/` (ephemeral container FS) | Yes | **NO** | **NO** | YES (only copy of export) |
| `AuditService` (feedback) | User ratings, comments | Delta table (`chatbot_feedback`) | Yes (SDK) | **YES** | Yes (Delta ACID) | YES |
| `AuditService` (audit log) | Query audit records | Delta table (`query_audit_log`) | Yes (SDK) | **YES** | Yes (Delta ACID) | YES |
| React conversation state | messages[], activeConvId (UUID), genieConversationId | Browser React useState (memory) | N/A | **NO** (page refresh loses it) | N/A | NO (frontend display only) |

---

## 1. GenieSessionStore

**File:** `app/services/genie_session_store.py`  
**Type:** In-memory Python dict with threading.Lock, TTL=24h

**Data held per session (`GenieSession`):**
- `app_conversation_id` — the namespaced key `"{session_id}:{frontend_uuid}"`
- `genie_conversation_id` — the Genie-side conversation ID (from `start-conversation` API)
- `last_genie_message_id` — last Genie message ID (for multi-turn context)
- `last_intent`, `last_entities`, `last_entity_type`, `last_filters`
- `last_user_prompt`, `last_enriched_prompt`
- `last_download_key`, `last_export_id`, `last_export_status`, `last_export_mode`, `last_export_row_count`
- `last_table_headers`, `last_row_count`, `last_total_row_count`, `last_returned_row_count`
- `latest_table_result` (Contract 7 — message-level export record)
- `created_at`, `updated_at`, `expires_at`, `is_active`

**Critical gap:** `serialize_session()` and `deserialize_session()` methods exist and produce complete plain-dict representations. However, they are **never called** in the running application. There is no Delta adapter or Lakebase adapter wired to these methods. All state is lost on FastAPI process restart.

**Required behavior when Lakebase persistence is added (Phase 2):**
1. Lakebase is the **authoritative** store. In-memory state is a non-authoritative cache only.
2. If Lakebase is unavailable and an in-memory mapping is known, the app may continue that conversation using the cached Genie IDs — without making any authoritative write.
3. If Lakebase is unavailable and **no** mapping can be recovered (cold start after restart, no in-memory hit), return a controlled retryable service error to the caller.
4. **Never start a new Genie conversation silently** because the durable store is unavailable.
5. **Never tell the user** that the same conversation was continued when the Genie mapping could not be recovered.

**Root cause of idle-state bug:** Databricks Apps cold-starts the container after inactivity (~1–2 hours). All `_sessions` dict contents are destroyed. On the next request, the session is not found and a new `genie_conversation_id` is obtained from Genie, starting a fresh conversation context. The user loses multi-turn continuity silently.

**Singleton pattern:** `GeniePipeline._session_store` is a single `GenieSessionStore()` instance created in `_build_pipeline()`. The pipeline itself is a module-level singleton (`_genie_pipeline` in `genie_backend_factory.py`). All requests in the same process share the same store.

---

## 2. ConversationManager

**File:** `app/services/conversation_manager.py`  
**Type:** Delta-backed via SQL Warehouse Statement Execution API  
**Tables:**
- `onedata_fn_ion_dev.ion_l0_raw.shipmate_conversations`
- `onedata_fn_ion_dev.ion_l0_raw.shipmate_messages`

**Data held:** conversation rows (conv_id, user_id, title, timestamps, message_count, is_archived), message rows (message_id, conv_id, role, content, intent, timestamps).

**Usage in Genie path:** `chat.py` calls `conversations.add_message(server_conversation_key, ...)` for both user and assistant turns (lines 210–214, 246–251). This writes to Delta. However, the `server_conversation_key` (`session_id:frontend_uuid`) is transient — if the session cookie or browser UUID changes, there is no way to reconnect historical turns.

**`USE_DELTA_CONVERSATION_STATE=true`** in app.yaml, but this flag (config.py line 111) is for the old custom pipeline’s Phase 8 Delta state, not for the Genie session store. The Genie path does not read or write the `shipmate_conversation_state` table.

---

## 3. ExportJobManager

**File:** `app/services/export_job_manager.py`  
**Type:** In-memory dict, threading.Lock, TTL=24h

**Data held:** `ExportJob` records — export_id, app_conversation_id, status (queued/running/ready/failed), mode, file_path, download_key, row_count.

**Critical gap:** Async export jobs registered in memory are lost on restart. If a user triggers a CSV export and the app restarts before the job completes, the `GET /api/export/status/{export_id}` endpoint returns 404.

**serialize/deserialize hooks exist** (`serialize_job`, `deserialize_job`) but are never called in the running app — same pattern as GenieSessionStore.

---

## 4. CSV Export Files

**Location:** `EXPORT_VOLUME_PATH` = `/tmp/chatbot_exports/` (container ephemeral filesystem)  
**Type:** CSV files + `.meta` sidecar files on container local disk

**Critical gap:** Container local disk is wiped on restart. All CSV export files are lost. `AuditService.get_export_path()` scans this directory; on a fresh container it returns `None` for all old keys.

**Note:** This is independent of GenieSessionStore. Even if Genie session state were persisted in Lakebase, the CSV files would still be lost on restart unless they are moved to a persistent location (Unity Catalog volume or object storage).

---

## 5. AuditService (Feedback + Audit Log)

**File:** `app/services/audit_service.py`  
**Type:** Delta-backed via SQL Warehouse  
**Tables:** `chatbot_feedback`, `query_audit_log`

**Restart-safe:** YES. Delta tables survive container restart.

**Note:** `user_id` in audit records uses `X-User-Email` from feedback.py, which is always `"anonymous"` in production (Databricks Apps does not inject `X-User-Email`). This is a separate bug unrelated to Lakebase state persistence.

---

## 6. React Conversation State

**Location:** Browser memory (`useState` in `App.jsx`)  
**Data held:** `conversations[]` array (messages, loading states), `activeConvId` (crypto.randomUUID), `genieConversationId` per message.

**Persistence:** None. Page refresh or tab close destroys all conversation history. The `activeConvId` is regenerated on each page load.

**Impact on state key:** Because `frontend_conversation_id` comes from `crypto.randomUUID()` and is not persisted in localStorage, a page refresh generates a new UUID, which creates a new `server_conversation_key`, which creates a new `GenieSessionStore` session — even if the user and the Genie conversation are the same.

---

## 7. State That Disappears After Restart

- `GenieSessionStore._sessions` — **all Genie conversation mappings**
- `ExportJobManager._jobs` — **all async export job states**
- `AuditService._export_path` (CSV files in `/tmp/`) — **all download files**

## 8. State That Survives Deployment

- `ConversationManager` Delta tables — `shipmate_conversations`, `shipmate_messages`
- `AuditService` Delta tables — `chatbot_feedback`, `query_audit_log`

## 9. State Stored in /tmp

- CSV export files at `/tmp/chatbot_exports/` (ephemeral)

## 10. State Only in React Memory

- Chat message history (messages[] array)
- `activeConvId` (crypto.randomUUID, not persisted)

## 11. Delta State Configured but Not Used by Genie

- `shipmate_conversation_state` table (controlled by `USE_DELTA_CONVERSATION_STATE=true` in old pipeline Phase 8)
- This table is for the **old custom pipeline**, not the Genie path
- The Genie path has no equivalent Delta persistence — this is the gap Lakebase fills

## 12. Can Existing Persistent State Replace Proposed Lakebase?

**NO.** The Delta tables (`shipmate_conversations`, `shipmate_messages`) store human-readable chat history but do **not** store:
- `genie_conversation_id` (the Genie-side continuation token)
- `last_genie_message_id` (for multi-turn follow-up)
- `last_intent`, `last_entities` (business context for follow-up routing)
- `export_id` / `download_key` (async export lifecycle)

The proposed Lakebase `app_conversation` table fills this gap by providing a durable, low-latency store for the Genie session mapping that survives container restarts.
