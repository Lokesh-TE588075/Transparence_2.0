# App Resource Inventory — Phase 1 Investigation

**Date:** 2026-07-18  
**Branch:** feature/genie-state-persistence  
**Baseline commit:** 7d9977ff5965b8ee015bda1cc1662a317ae8a0d0

---

## 1. App Identity

| Field | Value |
|---|---|
| App name | `transparence` |
| App ID (UUID) | `488a0acb-5804-42f0-98b1-a02cc13c4573` |
| App URL | `https://transparence-4310366453016539.aws.databricksapps.com` |
| Service Principal ID | `78664835752275` |
| SP client ID | `488a0acb-5804-42f0-98b1-a02cc13c4573` |
| SP display name | `app-31pcl9 transparence` |
| OAuth2 app client ID | `7a1d8828-bddf-41cf-9621-ee98d46d80fd` |
| Active deployment ID | `01f181110277111f8f8d22379e477ecc` |
| Deployment status | SUCCEEDED |
| App status | RUNNING |
| Compute status | ACTIVE |
| Compute size | MEDIUM |
| Source path | `/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app` |
| Snapshot path | `/Workspace/Users/488a0acb-5804-42f0-98b1-a02cc13c4573/src/01f181110277111f8f8d22379e477ecc` |
| Creator | `lokesh.choraria@te.com` |
| Deploy mode | SNAPSHOT |

## 2. Effective User API Scopes (Authorization)

The `effective_user_api_scopes` field from the Apps API:

```
[
  "iam.access-control:read",
  "iam.current-user:read"
]
```

**Critical observation:** These are **IAM-only** scopes. The app does **not** have `sql`, `genie`, `serving-endpoints`, `compute`, or any data-plane scopes in the forwarded user token. This means:
- `X-Forwarded-Access-Token` (if forwarded) cannot call the Genie API.
- `X-Forwarded-Access-Token` (if forwarded) cannot query the SQL warehouse.
- User-delegated Genie access (Model B) requires adding `databricks-genie` scope to the app config. This is a prerequisite for Model B.

## 3. Attached Resources

**Result: NO RESOURCES ATTACHED**

The `app.yaml` (both source and deployed snapshot) contains **no `resources:` section**.
The `apps get transparence` API response contains **no `resources` field**.

| Resource key | Type | Resource identifier | Permission | In app.yaml? | Used in code? |
|---|---|---|---|---|---|
| (none) | — | — | — | — | — |

**Explicit answers:**
- **Does the app currently have a Lakebase resource?** NO.
- **Does the app currently have a Genie Agent resource?** NO.
- **Does the app currently have a SQL warehouse resource?** NO (warehouse is provided via `DATABRICKS_SQL_WAREHOUSE_PATH` env var only).
- **Does the app currently have any secrets resource?** NO.
- **Does the app currently have a Unity Catalog table or volume resource?** NO.

## 4. Environment Variables (app.yaml `env:` section)

All app configuration is delivered via env vars only, not via Databricks App resource bindings:

| Env var | Value | Category |
|---|---|---|
| `DATABRICKS_SQL_WAREHOUSE_PATH` | `/sql/1.0/warehouses/8e46614f7064d8fd` | Data |
| `SHIPMENT_TABLE_NAME` | `onedata_fn_ion_dev.ion_l0_raw.lbn_with_scorecard` | Data |
| `GENIE_SPACE_ID` | `01f17a93e6aa1b97a9da7ef329e15e46` | Genie |
| `LLM_ENDPOINT_PRIMARY` | `databricks-claude-sonnet-5` | LLM |
| `LLM_ENDPOINT_FAST` | `databricks-gpt-5-4-mini` | LLM |
| `EXPORT_VOLUME_PATH` | `/tmp/chatbot_exports` | Export |
| `FEEDBACK_TABLE_NAME` | `onedata_fn_ion_dev.ion_l0_raw.chatbot_feedback` | Data |
| `AUDIT_TABLE_NAME` | `onedata_fn_ion_dev.ion_l0_raw.query_audit_log` | Data |
| `CONVERSATIONS_TABLE_NAME` | `onedata_fn_ion_dev.ion_l0_raw.shipmate_conversations` | Data |
| `MESSAGES_TABLE_NAME` | `onedata_fn_ion_dev.ion_l0_raw.shipmate_messages` | Data |
| `USE_GENIE_BACKEND` | `true` | Feature flag |
| `GENIE_FALLBACK_TO_CUSTOM_PIPELINE` | `true` | Feature flag |
| `USE_NEW_ACCURACY_PIPELINE` | `true` | Feature flag |
| `USE_DELTA_CONVERSATION_STATE` | `true` | Feature flag |
| `GENIE_EXPORT_MODE` | `returned_rows_only` | Export |
| `GENIE_ASYNC_EXPORT_ENABLED` | `true` | Export |

Notably absent (Lakebase connection vars):
- `PGHOST` — **not configured**
- `PGDATABASE` — **not configured**
- `PGPORT` — **not configured**
- `PGUSER` — **not configured**
- `PGSSLMODE` — **not configured**
- `PGAPPNAME` — **not configured**

These will only be available once a Lakebase resource is attached.

## 5. App Permissions

| Principal | Permission | Inherited |
|---|---|---|
| `lokesh.choraria@te.com` | CAN_MANAGE | No (direct) |
| `admins` group | CAN_MANAGE | Yes (from `/apps`) |

No other users or groups have explicit access grants to this app.

## 6. No Lakebase Connection Should Be Expected at Runtime

Because no Lakebase resource is attached, the app runtime will **not** receive any `PGHOST`, `PGDATABASE`, or credential environment variables. Any code that attempts to read these will receive `None` or raise a `KeyError`. This is the correct baseline state; Phase 2 adds the resource.
