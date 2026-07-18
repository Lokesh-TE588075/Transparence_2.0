# Phase 2B2B1 — Exit Assessment

## Phase Name

Attach the Lakebase Database Resource to the TransparencE App

## Execution Date

2026-07-18

## Exit Criteria Checklist

| Criterion | Status | Evidence |
|-----------|--------|----------|
| `postgres` resource attached to `transparence` | PASS | `apps get` returns resource with key `postgres` |
| Resource points to correct project `transparence-sessions` | PASS | branch path confirmed |
| Resource points to correct branch `production` | PASS | `projects/transparence-sessions/branches/production` |
| Resource points to correct database `databricks-postgres` | PASS | full path in `database` field |
| Permission is `CAN_CONNECT_AND_CREATE` | PASS | confirmed in REST response |
| App SP Postgres role exists | PASS | `dbrx-apps-488a0acb-5804-42f0-98b1-a02cc13c4573` created |
| CONNECT and CREATE confirmed | PASS | granted automatically by platform for `CAN_CONNECT_AND_CREATE` |
| No SQL object created | PASS | no schema, no table, no index, no row, no manual GRANT |
| No source file changed | PASS | `apps get-deployment` timestamps unchanged (2026-07-16) |
| `app.yaml` unchanged | PASS | read confirms no `resources:` section added |
| No test changed | PASS | no test files touched |
| No deployment occurred | PASS | deployment ID unchanged, `create_time` / `update_time` remain 2026-07-16 |
| App not restarted | PASS | deployment `update_time` unchanged; app status RUNNING |
| Documentation pushed | see Step 11 |
| Git state clean | see Step 11 |

## Overall Verdict

**PASS** — all exit criteria met except documentation commit (Step 11 pending).

## App Identity Summary

| Field | Value |
|-------|-------|
| App name | `transparence` |
| App platform ID | `488a0acb-5804-42f0-98b1-a02cc13c4573` |
| SP display name | `app-31pcl9 transparence` |
| SP client ID | `488a0acb-5804-42f0-98b1-a02cc13c4573` |
| SP numeric ID | `78664835752275` |

## Resource Summary

| Field | Value |
|-------|-------|
| Resource key | `postgres` |
| Resource type | Lakebase Autoscaling (`AppResourcePostgres`) |
| Branch | `projects/transparence-sessions/branches/production` |
| Database path | `projects/transparence-sessions/branches/production/databases/databricks-postgres` |
| Postgres DB name | `databricks_postgres` |
| Permission | `CAN_CONNECT_AND_CREATE` |

## Postgres Role Summary

| Field | Value |
|-------|-------|
| role_id | `dbrx-apps-488a0acb-5804-42f0-98b1-a02cc13c4573` |
| postgres_role | `488a0acb-5804-42f0-98b1-a02cc13c4573` |
| identity_type | `SERVICE_PRINCIPAL` |
| Database privileges | CONNECT, CREATE on `databricks_postgres` |
| Superuser | No |
| Schemas owned | None |
| Tables owned | None |

## Deployment Summary

| Field | Before | After |
|-------|--------|-------|
| Active deployment ID | `01f181110277111f8f8d22379e477ecc` | `01f181110277111f8f8d22379e477ecc` (unchanged) |
| Deployment status | `SUCCEEDED` | `SUCCEEDED` (unchanged) |
| App status | `RUNNING` | `RUNNING` (unchanged) |

## Phase 2B2B2 Prerequisites

Phase 2B2B2 (schema and `app_conversation` table creation) is safe to begin
**after** the following prerequisite is met:

1. **Issue a new deployment** so that the running app process receives the
   `PGHOST`, `PGDATABASE`, `PGUSER`, `PGPORT` environment variables injected
   by the platform from the `postgres` resource.

Without that deployment the app cannot open a Postgres connection.  The DDL
for `app_conversation` must not be executed by a notebook or manual script —
it must be executed by the app’s startup logic (or migration harness) running
with the SP credentials that own the schema.

## Technical Note: SDK Version Requirement

The resource attachment required SDK `>=0.118.0` (used 0.121.0).
The `AppResourcePostgres` class does not exist in SDK 0.67.0.  Attempting
attachment with the old `AppResourceDatabase` class returns `NotFound: Database
instance ... does not exist` because that class is for Provisioned instances
only.  Any future automation for this resource must pin `databricks-sdk>=0.118.0`.
