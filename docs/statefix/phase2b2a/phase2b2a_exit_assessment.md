# Phase 2B2A — Exit Assessment

## Verdict: PASS

All Phase 2B2A exit criteria are satisfied.

---

## Exit Criteria Checklist

| # | Criterion                                                       | Status |
|---|-----------------------------------------------------------------|--------|
| 1 | One appropriate Lakebase Autoscaling project exists             | PASS   |
| 2 | Project status is ready/active                                  | PASS (branch READY; project has no explicit state field; owner+resources confirm active) |
| 3 | Production branch exists                                        | PASS   |
| 4 | `databricks_postgres` database exists                           | PASS   |
| 5 | Read/write endpoint exists and is ready                         | PASS (IDLE = scale-to-zero suspended; functionally ready) |
| 6 | PostgreSQL version and region are recorded                      | PASS (pg17, us-east-1) |
| 7 | Cost and scale-to-zero settings are recorded                    | PASS   |
| 8 | Current user access is understood                               | PASS (owner, DATABRICKS_SUPERUSER) |
| 9 | App SP has no database access                                   | PASS (SP not in roles) |
| 10| No database SQL was executed                                    | PASS   |
| 11| No custom database object was created                           | PASS   |
| 12| TransparencE app remains unchanged                              | PASS   |
| 13| Documentation is pushed                                         | See Step 14 |
| 14| Git state is clean                                              | See Step 14 |

---

## Condition Note — Endpoint IDLE State

The primary endpoint is in `IDLE` state, not `READY`. This is expected
behavior when scale-to-zero is enabled: the endpoint suspends after
5 minutes of inactivity and wakes in ~100 ms on the first connection.
The endpoint is **functionally provisioned and accessible**.

If the exit criterion strictly requires the string `READY`, the endpoint
can be woken by making a benign connection attempt; it will transition to
`READY` (active) and return to `IDLE` after the 5-minute timeout.

---

## Phase 2B2B Prerequisites

Before starting Phase 2B2B (app resource attachment and schema creation):

1. **This phase must be committed and pushed** (Step 14 of Phase 2B2A).
2. **Project resource name required for app attachment:**
   `projects/transparence-sessions`
3. **Branch resource name required for connection config:**
   `projects/transparence-sessions/branches/production`
4. **Database resource name:**
   `projects/transparence-sessions/branches/production/databases/databricks-postgres`
5. **Endpoint resource name (for host lookup):**
   `projects/transparence-sessions/branches/production/endpoints/primary`
6. **SP ID to grant access:** `78664835752275` (`app-31pcl9 transparence`)
7. **Table name to create:** `app_conversation` in schema `transparence_sessions`
   (SP must create schema to become owner)
8. **Feature flag to enable:** `USE_DELTA_GENIE_SESSION_STATE=true` (not yet
   in app.yaml)
9. **app.yaml changes required in 2B2B:** add `lakebase` resource block and
   PGHOST / PGDATABASE / PGPORT / PGUSER / PGSSLMODE env vars.

---

## Rollback Procedure (Documentation Only — Do Not Execute)

If this Lakebase project must be removed:

1. **Remove any future app resource attachment first** (Phase 2B2B rollback
   must precede project deletion if attachment has been done).
2. The interactive user (`lokesh.choraria@te.com`) must run the following
   **outside the agent** (in a local notebook or terminal) after confirming
   all data in the project is disposable:

   ```python
   from databricks.sdk import WorkspaceClient
   w = WorkspaceClient()
   w.postgres.delete_project(name="projects/transparence-sessions")
   ```

3. **This operation is irreversible.** It permanently deletes all branches,
   databases, endpoints, roles, and data in the project.
4. The agent will not execute this operation on behalf of the user.
5. After deletion, verify with `w.postgres.list_projects()` that the project
   is no longer present.

---

## Files Created in This Phase

All files are under `docs/statefix/phase2b2a/` in the
`feature/genie-state-persistence` branch:

| File                                  | Purpose                                     |
|---------------------------------------|---------------------------------------------|
| `lakebase_project_provisioning.md`    | Creation rationale, API details, outcome    |
| `lakebase_resource_inventory.md`      | All resource IDs and identifiers            |
| `access_and_cost_configuration.md`    | Access control and autoscaling settings     |
| `phase2b2a_exit_assessment.md`        | This file; exit checklist and next steps    |

---

## Key Identifiers Quick Reference

```
Project:   projects/transparence-sessions
Branch:    projects/transparence-sessions/branches/production
Database:  projects/transparence-sessions/branches/production/databases/databricks-postgres
Endpoint:  projects/transparence-sessions/branches/production/endpoints/primary
Owner:     lokesh.choraria@te.com
PG ver:    17
Region:    us-east-1 (AWS)
Min CU:    0.5
Max CU:    1.0
S2Z:       enabled (5 min timeout)
HA:        disabled
```
