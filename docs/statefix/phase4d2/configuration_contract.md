# Phase 4D2 — Production Configuration Contract

Phase 4D2 — Validate production configuration and permissions readiness

Generated: 2026-07-20

---

## Configuration Inventory (28 items)

### Category 1: Databricks Connectivity

| Name | Required | Default | Source | Sensitive | Fail-closed | Purpose |
|------|----------|---------|--------|-----------|-------------|---------|
| DATABRICKS_HOST | Yes | Auto-detected from env | env / auto-detect | No | Startup degraded | Workspace API host for Genie calls |
| DATABRICKS_TOKEN | No | SDK credential chain | env / SDK | Yes | No (SDK fallback) | Auth token; absent = SDK chain used |
| DATABRICKS_SQL_WAREHOUSE_PATH | Yes | /sql/1.0/warehouses/8e46614f7064d8fd | app.yaml env | No | Warning only | SQL warehouse for Genie Space |

### Category 2: Data Tables

| Name | Required | Default | Source | Sensitive | Fail-closed | Purpose |
|------|----------|---------|--------|-----------|-------------|---------|
| SHIPMENT_TABLE_NAME | Yes | onedata_fn_ion_dev.ion_l0_raw.lbn_with_scorecard | app.yaml env | No | Warning only | Fully qualified shipment fact table |
| FEEDBACK_TABLE_NAME | Optional | onedata_fn_ion_dev.ion_l0_raw.chatbot_feedback | app.yaml env | No | No | Feedback storage |
| AUDIT_TABLE_NAME | Optional | onedata_fn_ion_dev.ion_l0_raw.query_audit_log | app.yaml env | No | No | Audit log table |
| CONVERSATIONS_TABLE_NAME | Optional | onedata_fn_ion_dev.ion_l0_raw.shipmate_conversations | app.yaml env | No | No | Legacy conversation storage |
| MESSAGES_TABLE_NAME | Optional | onedata_fn_ion_dev.ion_l0_raw.shipmate_messages | app.yaml env | No | No | Legacy message storage |

### Category 3: LLM Endpoints

| Name | Required | Default | Source | Sensitive | Fail-closed | Purpose |
|------|----------|---------|--------|-----------|-------------|---------|
| LLM_ENDPOINT_PRIMARY | Required for custom pipeline | (empty) | app.yaml env | No | Warning | Primary LLM: databricks-claude-sonnet-5 |
| LLM_ENDPOINT_FAST | Optional | (empty) | app.yaml env | No | No | Fast LLM: databricks-gpt-5-4-mini |

### Category 4: Genie Backend

| Name | Required | Default | Source | Sensitive | Fail-closed | Purpose |
|------|----------|---------|--------|-----------|-------------|---------|
| GENIE_SPACE_ID | Required when USE_GENIE_BACKEND=true | 01f17a93e6aa1b97a9da7ef329e15e46 | app.yaml env | No | Yes | TransparencE Shipment Intelligence space |
| GENIE_RESPONSE_TIMEOUT_SECONDS | Optional | 120 | app.yaml env | No | No | Max wait for Genie response |
| GENIE_DEBUG | Optional | false | app.yaml env | No | No (warning) | Verbose Genie logging — must be false in prod |
| GENIE_ENABLE_PROMPT_ENRICHMENT | Optional | true | app.yaml env | No | No | Steer Genie toward analytical output |
| GENIE_SHOW_SQL | Optional | false | app.yaml env | No | No | Hide SQL from end users |
| GENIE_MAX_DOWNLOAD_ROWS | Optional | 5000 | app.yaml env | No | No | CSV export row cap |
| GENIE_EXPORT_MODE | Optional | returned_rows_only | app.yaml env | No | Validation error | returned_rows_only or async_full_query |
| GENIE_ASYNC_EXPORT_ENABLED | Optional | true | app.yaml env | No | No | Enable async export |
| GENIE_ENABLE_TABLE_SUMMARY | Optional | true | app.yaml env | No | No | Deterministic table summarizer |
| GENIE_ENABLE_COMPUTED_CHART | Optional | true | app.yaml env | No | No | Computed chart from table summarizer |

### Category 5: Diagnostic Tracing

| Name | Required | Default | Source | Sensitive | Fail-closed | Purpose |
|------|----------|---------|--------|-----------|-------------|---------|
| TRANSPARENCE_DIAGNOSTIC_TRACING_ENABLED | Optional | false | app.yaml env | No | No (warning) | Master switch — must be false in prod |
| TRANSPARENCE_DIAGNOSTIC_LOG_SQL | Optional | false | app.yaml env | No | No (warning) | Persist SQL in traces — must be false in prod |
| TRANSPARENCE_DIAGNOSTIC_STORE | Optional | delta | app.yaml env | No | No | Backend: delta or none |
| TRANSPARENCE_DEPLOYMENT_ID | Optional | (empty) | app.yaml env | No | No | Deployment tag for traces |

### Category 6: Lakebase (injected by app resource binding)

| Name | Required | Default | Source | Sensitive | Fail-closed | Purpose |
|------|----------|---------|--------|-----------|-------------|---------|
| LAKEBASE_ENDPOINT_NAME | Required when durable adapter enabled | via valueFrom: postgres | app.yaml resource | No | Yes | Lakebase endpoint resource path |
| PGHOST | Required when Lakebase active | Injected by platform | App resource binding | No | Yes | PostgreSQL host |
| PGDATABASE | Required when Lakebase active | Injected by platform | App resource binding | No | Yes | PostgreSQL database name |
| PGPORT | Required when Lakebase active | Injected by platform | App resource binding | No | Yes | PostgreSQL port |
| PGUSER | Required when Lakebase active | Injected by platform | App resource binding | No | Yes | PostgreSQL user (SP role) |
| PGSSLMODE | Required when Lakebase active | Injected by platform | App resource binding | No | Yes | SSL mode — must not be disable/allow/prefer |

### Category 7: Owner Identity and Secrets

| Name | Required | Default | Source | Sensitive | Fail-closed | Purpose |
|------|----------|---------|--------|-----------|-------------|---------|
| CONVERSATION_OWNER_HMAC_SECRET | Required when ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY=true | via valueFrom: conversation-owner-hmac-secret | Databricks secret scope | YES — never log | Yes | HMAC-SHA256 secret for owner identity. Min 32 bytes. |

---

## Feature-Flag Inventory (15 flags)

| Flag | Default | Parser | Production value | Test-deployment value | Failure on invalid |
|------|---------|--------|------------------|-----------------------|--------------------|
| USE_GENIE_BACKEND | false | lower in (true,1,yes,false,0,no,off) | true | true | Yes — unrecognised value is blocked |
| GENIE_FALLBACK_TO_CUSTOM_PIPELINE | true | same | false | true | Yes |
| USE_NEW_ACCURACY_PIPELINE | false (code) / true (app.yaml) | same | true | true | Yes |
| NEW_PIPELINE_FALLBACK_TO_OLD | true | same | false | true | Yes |
| NEW_PIPELINE_DEBUG | false | same | false | false | Yes (warning only) |
| GENIE_DEBUG | false | same | false | false | Yes (warning only) |
| ENABLE_DURABLE_GENIE_SESSION_ADAPTER | false | strict (true/false/1/0/yes/no/on/off) | true | false | Yes — raises config error |
| CONVERSATION_REPOSITORY_BACKEND | memory | enum parser (memory/lakebase) | lakebase | memory | Yes |
| ENABLE_LAKEBASE_CONVERSATION_REPOSITORY | false | strict | true | false | Yes |
| ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY | false | strict | true | false | Yes |
| CONVERSATION_STATE_CLEANUP_HARD_DELETE | false | same | false | false | YES — true is explicitly blocked |
| USE_DELTA_CONVERSATION_STATE | false | same | false | false | No |
| TRANSPARENCE_DIAGNOSTIC_TRACING_ENABLED | false | same | false | false | No (warning if true) |
| TRANSPARENCE_DIAGNOSTIC_LOG_SQL | false | same | false | false | No (warning if true) |
| GENIE_EXPORT_MODE | returned_rows_only | string enum | returned_rows_only | returned_rows_only | Yes — unknown value blocked |

---

## Feature-Flag Dependency Graph

```
INVARIANT 1: ENABLE_DURABLE_GENIE_SESSION_ADAPTER=true
    REQUIRES: ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY=true
    VIOLATION: durable state cannot establish ownership without verified identity

INVARIANT 2: CONVERSATION_REPOSITORY_BACKEND=lakebase
    REQUIRES: ENABLE_LAKEBASE_CONVERSATION_REPOSITORY=true
    VIOLATION: lakebase backend disabled by default

INVARIANT 3: ENABLE_DURABLE_GENIE_SESSION_ADAPTER=true
    RECOMMENDED: CONVERSATION_REPOSITORY_BACKEND=lakebase
    WARNING if not lakebase: sessions will not survive restarts

INVARIANT 4: USE_GENIE_BACKEND=true
    REQUIRES: GENIE_SPACE_ID non-empty
    VIOLATION: no routing target

INVARIANT 5: CONVERSATION_STATE_CLEANUP_HARD_DELETE=true
    ALWAYS BLOCKED: requires explicit approval

INVARIANT 6: production debug flags
    GENIE_DEBUG=true: WARNING (not blocked; logs sensitive query patterns)
    NEW_PIPELINE_DEBUG=true: WARNING
    TRANSPARENCE_DIAGNOSTIC_TRACING_ENABLED=true: WARNING (acceptable for soak test)
    TRANSPARENCE_DIAGNOSTIC_LOG_SQL=true: WARNING (SQL in traces)
```

---

## Safe Test-Deployment Flag Combination

Objective: Genie smoke test only. Durable state and trusted identity disabled.

```yaml
USE_GENIE_BACKEND: "true"
GENIE_FALLBACK_TO_CUSTOM_PIPELINE: "true"
ENABLE_DURABLE_GENIE_SESSION_ADAPTER: "false"
CONVERSATION_REPOSITORY_BACKEND: "memory"
ENABLE_LAKEBASE_CONVERSATION_REPOSITORY: "false"
ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY: "false"
GENIE_DEBUG: "false"
NEW_PIPELINE_DEBUG: "false"
CONVERSATION_STATE_CLEANUP_HARD_DELETE: "false"
TRANSPARENCE_DIAGNOSTIC_TRACING_ENABLED: "false"
TRANSPARENCE_DIAGNOSTIC_LOG_SQL: "false"
USE_NEW_ACCURACY_PIPELINE: "true"
NEW_PIPELINE_FALLBACK_TO_OLD: "true"
GENIE_EXPORT_MODE: "returned_rows_only"
```

Required resource bindings for this deployment:
- `postgres` binding: present (injected by platform even if adapter disabled)
- `conversation-owner-hmac-secret` binding: present (injected; secret not read when trusted identity disabled)

---

## Safe Production Flag Combination

Objective: Full durable state, trusted identity, no legacy fallback.

```yaml
USE_GENIE_BACKEND: "true"
GENIE_FALLBACK_TO_CUSTOM_PIPELINE: "false"
ENABLE_DURABLE_GENIE_SESSION_ADAPTER: "true"
CONVERSATION_REPOSITORY_BACKEND: "lakebase"
ENABLE_LAKEBASE_CONVERSATION_REPOSITORY: "true"
ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY: "true"
GENIE_DEBUG: "false"
NEW_PIPELINE_DEBUG: "false"
CONVERSATION_STATE_CLEANUP_HARD_DELETE: "false"
TRANSPARENCE_DIAGNOSTIC_TRACING_ENABLED: "false"
TRANSPARENCE_DIAGNOSTIC_LOG_SQL: "false"
USE_NEW_ACCURACY_PIPELINE: "true"
NEW_PIPELINE_FALLBACK_TO_OLD: "false"
GENIE_EXPORT_MODE: "returned_rows_only"
```

Additional required resources:
- `CONVERSATION_OWNER_HMAC_SECRET` via `conversation-owner-hmac-secret` secret binding (min 32 bytes)
- All Lakebase PG vars injected by `postgres` binding
- `LAKEBASE_ENDPOINT_NAME` injected by `postgres` binding

---

## Required Secret References (values not stored here)

| Secret reference name | Secret scope | Secret key | Minimum quality | Used when |
|-----------------------|-------------|------------|-----------------|----------|
| conversation-owner-hmac-secret | transparence-owner-identity | conversation-owner-hmac-v1 | 32+ bytes UTF-8 | ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY=true |

Secret binding status: **PRESENT** — confirmed in app resource binding.
Secret value quality: **CANNOT VERIFY** without enabling trusted identity.

---

## App Resource Bindings (current)

| Resource key | Type | Binding target | Status |
|-------------|------|----------------|--------|
| postgres | Lakebase | projects/transparence-sessions/branches/production | PRESENT |
| conversation-owner-hmac-secret | Secret | transparence-owner-identity / conversation-owner-hmac-v1 | PRESENT |

---

## CRITICAL DEPLOYMENT NOTE: Source Code Path

The current active Databricks App deployment (`01f181110277111f8f8d22379e477ecc`) uses
source path `/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app`.

The Phase 4D2 changes (and all preceding state-persistence phases) are in the Git repo
at `/Workspace/Users/lokesh.choraria@te.com/Transparence/Transparence_2_0_git`
on branch `feature/genie-state-persistence`.

**Before test deployment, the source_code_path in the deployment must point to the git repo,
or the git repo content must be synced to `transparence_app`.**

This source path mismatch is the primary deployment gate for Phase 4D2.
