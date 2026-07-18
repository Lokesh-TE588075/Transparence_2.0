# Phase 2B2B1 — Runtime Impact Assessment

## Scope

This document assesses the impact on the running `transparence` app from
attaching the Lakebase `postgres` resource in Phase 2B2B1.

## Active Deployment

| Field              | Before attachment            | After attachment             |
|--------------------|------------------------------|------------------------------|
| Deployment ID      | `01f181110277111f8f8d22379e477ecc` | `01f181110277111f8f8d22379e477ecc` |
| Deployment status  | `SUCCEEDED`                  | `SUCCEEDED` (unchanged)      |
| App status         | `RUNNING`                    | `RUNNING` (unchanged)        |
| Compute status     | `ACTIVE`                     | `ACTIVE` (unchanged)         |
| Pending deployment | none                         | none                         |
| Source path        | `/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app` | unchanged |
| Source SHA (deploy)| `2026-07-16T12:22:28Z` build | unchanged                    |

## Impact on Running Process

**Zero impact on the currently running process.**

The resource attachment is a configuration change at the platform level.  The
Databricks Apps platform does **not** restart the app or inject new environment
variables into an already-running process when a resource is added without a
new deployment.

The `PGHOST`, `PGDATABASE`, `PGUSER`, `PGPORT`, and `DATABRICKS_CLIENT_ID`
environment variables that Lakebase injects are only present after the **next
deployment** that picks up the resource configuration.  The current running
process does not have these variables and cannot connect to Postgres.

## Expected PG Environment Variables (Post-Deployment)

After a future deployment, the app process will receive:

| Variable               | Expected Value                                                                |
|------------------------|-------------------------------------------------------------------------------|
| `PGHOST`               | `ep-withered-king-d257e0k1.database.us-east-1.cloud.databricks.com`           |
| `PGDATABASE`           | `databricks_postgres`                                                         |
| `PGUSER`               | `488a0acb-5804-42f0-98b1-a02cc13c4573`                                        |
| `PGPORT`               | `5432`                                                                        |
| `DATABRICKS_CLIENT_ID` | `488a0acb-5804-42f0-98b1-a02cc13c4573`                                        |

## User-Facing Impact

None.  The running app serves traffic unchanged.  The Genie pipeline,
chat backend, and all existing features continue to operate normally.

## Lakebase Endpoint State

The endpoint `primary` was `IDLE` at the time of attachment (scale-to-zero
enabled, 5-minute suspend timeout).  The platform did not wake the endpoint to
attach the resource.  The endpoint will wake on first connection attempt from
the app (expected to take ~100 ms).

## Files Changed

| File | Change |
|------|--------|
| `app.yaml` | **None** — byte-for-byte unchanged |
| Application source (`*.py`, `*.jsx`, etc.) | **None** |
| Tests | **None** |
| `requirements.txt` | **None** |

Only four documentation files in `docs/statefix/phase2b2b1/` were created.

## SQL Objects Created

| Object type | Created | Notes |
|-------------|---------|-------|
| Schema | No | None |
| Table (`app_conversation`) | No | None |
| Index | No | None |
| Row | No | None |
| Manual GRANT | No | Platform-managed only |

The only database-side change was the automatic creation of the Postgres role
`dbrx-apps-488a0acb-5804-42f0-98b1-a02cc13c4573` and the automatic grant of
CONNECT + CREATE on `databricks_postgres`, both managed by the platform.

## Pre-Deployment Prerequisite for Phase 2B2B2

Before schema and table creation can begin, a new deployment must be issued so
that the running app process receives the `PG*` environment variables.  Only
after that deployment should `genie_session_store_pg.py` (or equivalent) be
enabled to establish the first Postgres connection and create the
`app_conversation` table.
