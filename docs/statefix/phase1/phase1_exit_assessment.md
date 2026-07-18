# Phase 1 Exit Assessment

**Date:** 2026-07-18  
**Branch:** feature/genie-state-persistence  
**Baseline commit:** 7d9977ff5965b8ee015bda1cc1662a317ae8a0d0

---

## Phase 1 Verdict

### PASS WITH CONDITIONS

All Phase 1 investigation objectives are met. One manual infrastructure action (Lakebase project creation + app resource attachment) must be completed by the application owner before Phase 2 implementation begins. No code changes are blocked pending that action — Phase 2 code can be written and tested locally while the infrastructure is prepared.

---

## Phase 1 Exit Criteria Checklist

| Criterion | Status |
|---|---|
| Git working state clean except Phase 1 documentation | PASS |
| Current app resources inventoried | PASS |
| Lakebase availability determined | PASS |
| Current user and SP permissions separated | PASS |
| Current identity headers mapped | PASS |
| Current Genie authorization path proven | PASS |
| Current state stores inventoried | PASS |
| Minimal schema designed | PASS |
| Required manual infrastructure action identified | PASS |
| No application source changed | PASS |
| Nothing deployed | PASS |
| Application not restarted | PASS |

**Condition:** Phase 2A (standalone repository module + unit tests) is safe to begin immediately. It must not modify application source, frontend, or app.yaml, and does not require Lakebase infrastructure. The Lakebase project and app resource attachment must be completed before live integration or deployment.

---

## All Phase 1 Findings at a Glance

### Git Baseline
- Repository: `https://github.com/Lokesh-TE588075/Transparence_2.0.git`
- Branch: `feature/genie-state-persistence`
- HEAD commit: `7d9977ff5965b8ee015bda1cc1662a317ae8a0d0` (exact baseline match)
- State: clean, 0 uncommitted files

### Active Deployment
- Deployment ID: `01f181110277111f8f8d22379e477ecc` (SUCCEEDED, RUNNING)
- Source: `/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app` (SNAPSHOT mode)

### App Service Principal
- SP ID: `78664835752275`
- SP client ID / app ID: `488a0acb-5804-42f0-98b1-a02cc13c4573`
- SP display name: `app-31pcl9 transparence`

### Current App Resources
- **NONE.** No `resources:` section in app.yaml. No Lakebase, Genie agent, SQL warehouse, volume, or secret resources declared.

### Lakebase Availability
- Lakebase Autoscaling: **AVAILABLE** in workspace `te-ss-coe-dev`
- Existing projects: **0** (none exist)
- Existing Provisioned instances: **0**
- Interactive user can use the API: **CONFIRMED**

### Existing Projects, Branches, Databases
- None. A new project must be created.

### Current User Lakebase Permissions
- Can call `w.postgres.list_projects()`: YES
- Can create a project: YES (workspace-level entitlement confirmed by API access)
- Will be project owner: YES

### Current App SP Lakebase Permissions
- **Not configured.** No resource attached. No database access.

### Lakebase Resource Currently Attached
- **NO**

### Identity Headers Currently Used

| Header | Read? | Where |
|---|---|---|
| `X-Forwarded-Email` | YES | chat.py line 186 (primary user ID) |
| `X-User-Email` | YES | chat.py line 187 (fallback); feedback.py line 28 (only source) |
| `X-Forwarded-User` | NO | Not read anywhere |
| `X-Forwarded-Preferred-Username` | NO | Not read anywhere |
| `X-Forwarded-Access-Token` | NO | Explicitly excluded by design |
| `X-Request-Id` | NO | Not read anywhere |
| `transparence_session_id` cookie | YES | main.py session_middleware |
| React `crypto.randomUUID()` | YES | Sent as `body.conversation_id` |

### Recommended Durable User Identifier
- **`HMAC-SHA256(configured server secret, X-Forwarded-User)`** — stable, opaque, cryptographically bound to the server secret.
- Fall back to `HMAC-SHA256(secret, X-Forwarded-Email)` only when `X-Forwarded-User` is absent.
- `X-Forwarded-Email` is for display and audit only; it is not the primary ownership key.
- Do not hardcode the HMAC secret.

### Current Authoritative Conversation Key
- `server_conversation_key = f"{session_id}:{frontend_conversation_id}"` (chat.py line 207)
- `session_id` = cookie value (ephemeral, lost on new browser session)
- `frontend_conversation_id` = `crypto.randomUUID()` (ephemeral, lost on page refresh)
- **This key is not durable.** It is the key used in GenieSessionStore. Phase 2 maps it to a Lakebase row.

### Current Genie Authorization Identity
- **App service principal** via Databricks SDK credential chain
- All users share the same Genie Space authorization
- `user_token=None` is passed explicitly from chat.py — confirmed in code

### User Authorization Status and Scopes
- `effective_user_api_scopes`: `["iam.access-control:read", "iam.current-user:read"]`
- IAM-only scopes. User-delegated data access (Model B) is **not currently possible** without app config change.

### Authorization Decision — Recorded

**Model A — Shared Data Access is the current implementation decision.**

| Dimension | Decision |
|---|---|
| Conversation ownership | `X-Forwarded-User` (primary); `X-Forwarded-Email` for display/audit |
| Genie API calls | App service principal via SDK credential chain |
| Lakebase operational state | Owned and managed by the app service principal |
| Dataset permissions | All authorised app users share the Genie Space permissions granted to the SP |
| Per-user data isolation | Not implemented. Model B is deferred. |

**This decision must be revisited if future users require business-unit, region, customer, row-level, or column-level data restrictions.** Model B requires adding data-plane OAuth scopes to the app and is deferred.

### Current State Store Findings

| Store | Restart-safe? | Root cause role |
|---|---|---|
| `GenieSessionStore` | NO | **Primary root cause**: genie_conv_id lost on restart |
| `ConversationManager` (Delta) | YES | Not involved in idle-state bug |
| `ExportJobManager` | NO | Secondary: async export jobs lost on restart |
| CSV export files (`/tmp/`) | NO | Secondary: download files lost on restart |
| React conversation state | NO | Tertiary: page refresh loses frontend UUID |

### Idle-State Root Cause Confirmed from Code
`GenieSessionStore._sessions` is a Python dict in the FastAPI worker process. `serialize_session()` and `deserialize_session()` exist but are never called. On container cold-start, the dict is empty. The first request after a restart finds no session, calls `start-conversation` to get a new `genie_conversation_id`, and multi-turn context is lost. The Genie Space itself retains the old conversation server-side but the app has no way to reconnect to it.

### Proposed Phase 2 Schema
- Table: `app_conversation` in Lakebase `databricks_postgres` database
- Fields: `conversation_id` (PK UUID), `owner_user_id_hash`, `frontend_conversation_id`, `genie_conversation_id`, `last_genie_message_id`, `status`, `version`, `created_at`, `updated_at`, `last_active_at`
- Constraints: PK + UNIQUE(owner_user_id_hash, frontend_conversation_id)
- Optimistic concurrency via `version` field
- See `proposed_conversation_schema.md` for full DDL

### Proposed Connection Approach
- **psycopg 3** (`psycopg[binary]>=3.1`) + **psycopg_pool** (`psycopg-pool>=3.1`)
- Callable `conninfo` passed to `ConnectionPool` — fresh OAuth credentials generated per new physical connection via `WorkspaceClient().postgres.generate_database_credential(endpoint=...)`
- Bounded pool: `min_size=1`, `max_size=5`, `open=False` (lazy), `reconnect_timeout=30s`
- `sslmode` from injected `PGSSLMODE` env var (default `require`); `connect_timeout=10s`
- Resource-injected `PGHOST`, `PGDATABASE`, `PGPORT`, `PGUSER`, `PGSSLMODE` — never hardcoded
- **Persistence failure policy:** Lakebase is the authoritative store. If unavailable and no mapping is recoverable, return a controlled retryable error — never start a new Genie conversation silently.
- Flag: `USE_LAKEBASE_SESSION_STORE` (additive, default false until Lakebase resource is attached)

### Manual Infrastructure Action Required
- **Result C**: Create Lakebase Autoscaling project, then attach to app.
- Project: `transparence-sessions` (display: `TransparencE Session Store`)
- Branch: `production` (auto-created)
- Endpoint: `primary` (auto-created)
- Database: `databricks_postgres` (auto-created)
- App resource key: `genie_session_db`, permission: `CAN_CONNECT_AND_CREATE`
- See `manual_infrastructure_action.md` for exact commands

---

## Documentation Created

| File | Status |
|---|---|
| `docs/statefix/phase1/lakebase_availability.md` | Created |
| `docs/statefix/phase1/app_resource_inventory.md` | Created |
| `docs/statefix/phase1/identity_and_authorization.md` | Created |
| `docs/statefix/phase1/current_state_store_inventory.md` | Created |
| `docs/statefix/phase1/proposed_conversation_schema.md` | Created |
| `docs/statefix/phase1/manual_infrastructure_action.md` | Created |
| `docs/statefix/phase1/phase1_exit_assessment.md` | This file |

---

## Confirmation

- No application source file was changed.
- No tests were changed.
- No app resources were changed.
- No database objects were created.
- No deployment occurred.
- The running app was not restarted.
- Git branch was not changed.
- No commits were made.
- Nothing was pushed.

---

## Phase 2A Safe to Begin?

**YES — Phase 2A may begin immediately.**

### Phase 2A: Standalone Repository Module + Unit Tests

Phase 2A is scoped to writing and testing the repository abstraction in isolation.

**Phase 2A must implement:**
- `app/repository/conversation_repository.py` — the Lakebase-backed `ConversationRepository` class
- Unit tests for the repository (with a real or in-process Postgres instance, or fully mocked)
- DDL migration helper (`app/repository/migrations.py`) that creates `app_conversation` on first connection

**Phase 2A must NOT:**
- Modify `chat.py`
- Modify `genie_pipeline.py`
- Modify `genie_session_store.py`
- Modify `genie_backend_factory.py`
- Modify any frontend file
- Modify `app.yaml`
- Create the Lakebase project
- Attach app resources
- Deploy or restart the app

### Prerequisite for Live Integration (Phase 2B)

The Lakebase project must exist and the app resource must be attached before Phase 2B (wire-up) can be deployed. Phase 2B wires the repository into the Genie pipeline and enables the `USE_LAKEBASE_SESSION_STORE` flag. Phase 2B deployment requires infrastructure to be in place.
