# Manual Infrastructure Action — Phase 1 Decision

**Date:** 2026-07-18  
**Branch:** feature/genie-state-persistence  
**Baseline commit:** 7d9977ff5965b8ee015bda1cc1662a317ae8a0d0

---

## Determination

**Result C applies.**

No Lakebase project exists. No suitable database is available. A new project must be created and then attached to the TransparencE app before Phase 2 implementation can begin.

---

## Precondition Check (verified in Phase 1)

| Check | Status |
|---|---|
| Lakebase Autoscaling enabled in workspace | CONFIRMED |
| Interactive user can list projects (API access) | CONFIRMED |
| Existing projects visible | NONE |
| Existing databases to reuse | NONE |
| App currently has a Lakebase resource | NO |

---

## Required Manual Action

### Step 1: Create a Lakebase Autoscaling Project

This creates the project, a `production` branch, and a `primary` read-write endpoint automatically.

**Project naming convention:**
- Project ID: `transparence-sessions` (or similar short, RFC-1123-compliant name)
- Display name: `TransparencE Session Store`
- Postgres version: 17

**Do not perform this step until explicitly instructed to begin Phase 2.**

```python
# Python SDK — run in a Databricks notebook (not an agent) after user approval
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.postgres import Project, ProjectSpec

w = WorkspaceClient()
op = w.postgres.create_project(
    project=Project(spec=ProjectSpec(display_name="TransparencE Session Store", pg_version=17)),
    project_id="transparence-sessions",
)
project = op.wait()
print("Created:", project.name)
```

### Step 2: Note the Branch and Endpoint Names

After creation, list branches and endpoints to confirm the auto-created resources:

```python
for b in w.postgres.list_branches(parent="projects/transparence-sessions"):
    print("Branch:", b.name, b.status.current_state if b.status else "")

for e in w.postgres.list_endpoints(parent="projects/transparence-sessions/branches/production"):
    print("Endpoint:", e.name, e.status.hosts.host if e.status and e.status.hosts else "")
```

Expected:
- Branch: `projects/transparence-sessions/branches/production`
- Endpoint: `projects/transparence-sessions/branches/production/endpoints/primary`

### Step 3: Attach the Lakebase Resource to the App

Add the following `resources:` section to `app.yaml` (in the source at `/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app/app.yaml`):

```yaml
resources:
  - name: genie_session_db
    database:
      instance_name: projects/transparence-sessions/branches/production/databases/databricks_postgres
      permission: CAN_CONNECT_AND_CREATE
```

**Notes:**
- `name: genie_session_db` is the resource key that the app code will reference.
- `CAN_CONNECT_AND_CREATE` grants the SP the Postgres role that allows connecting and creating schemas/tables.
- The database name `databricks_postgres` is the default name auto-created on every new Lakebase branch.
- Do **not** set `PGHOST`, `PGDATABASE`, `PGPORT`, or `PGUSER` as manual env vars — the platform injects them automatically when the resource is attached.
- **This app.yaml change requires a redeployment to take effect.**

### Step 4: Verify the Resource Was Injected

After redeployment, verify the PG* env vars are available in the app runtime by checking the startup logs:

```bash
# CLI read-only check after redeployment
databricks apps get transparence --output JSON | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('resources', 'no resources'))"
```

The runtime should now receive:
- `PGHOST` — hostname of the Lakebase endpoint
- `PGDATABASE` — `databricks_postgres`
- `PGPORT` — `5432`
- `PGUSER` — the SP client ID
- `DATABRICKS_CLIENT_ID` — already set (SP identity)

---

## Connection Approach (for Phase 2 implementation)

When the Lakebase resource is attached, the app should connect using **psycopg 3** with **psycopg_pool**.

A callable `conninfo` is passed to `ConnectionPool` so that fresh OAuth credentials are generated for every new physical connection. This eliminates the need for a fixed token-refresh interval: credentials are always current because they are requested on demand.

```python
import os
import psycopg
from psycopg_pool import ConnectionPool
from databricks.sdk import WorkspaceClient

_w = WorkspaceClient()
_ENDPOINT = (
    "projects/transparence-sessions/branches/production/endpoints/primary"
)


def _conninfo() -> str:
    """Generate a connection string with a fresh OAuth token.

    Called by the pool for every new physical connection.
    PGHOST, PGDATABASE, PGPORT, PGUSER and PGSSLMODE are injected
    automatically by Databricks Apps when the Lakebase resource is attached.
    Do not hardcode any value — read exclusively from the environment.
    """
    cred = _w.postgres.generate_database_credential(endpoint=_ENDPOINT)
    return (
        f"host={os.environ['PGHOST']} "
        f"dbname={os.environ['PGDATABASE']} "
        f"user={os.environ['PGUSER']} "
        f"port={os.environ.get('PGPORT', '5432')} "
        f"password={cred.token} "
        f"sslmode={os.environ.get('PGSSLMODE', 'require')} "
        f"connect_timeout=10"
    )


pool = ConnectionPool(
    conninfo=_conninfo,   # callable — fresh credentials on every new connection
    min_size=1,
    max_size=5,
    open=False,           # open lazily on first use; do not block startup
    reconnect_timeout=30, # bounded wait before giving up a reconnect attempt
    kwargs={"autocommit": True},
)
```

**Key requirements:**
- Use resource-injected `PGHOST`, `PGDATABASE`, `PGPORT`, `PGUSER`, `PGSSLMODE` — never hardcode.
- Obtain fresh OAuth credentials via `WorkspaceClient().postgres.generate_database_credential(endpoint=...)` for each new physical connection. Do not assume a fixed token lifetime or cache tokens with a fixed interval.
- Use a bounded pool (`max_size=5`) with a bounded connection timeout (`connect_timeout=10`).
- Use `sslmode` from the injected `PGSSLMODE` env var (defaults to `require`).
- Validate connections before use (psycopg_pool's `check` callback or `pool.check()` at startup).
- Open the pool lazily (`open=False`) so a Lakebase startup failure does not prevent the FastAPI app from starting.

**Persistence failure policy (Correction 2):**
1. Lakebase is the **authoritative** store. In-memory state is a non-authoritative cache only.
2. If Lakebase is unavailable and an existing in-memory mapping is known, the app **may** continue that conversation using the cached Genie IDs — without making any authoritative write.
3. If Lakebase is unavailable and **no** mapping can be recovered (cold start, no cache hit), return a **controlled retryable service error** to the caller. Do not start a new Genie conversation silently.
4. Never start a new Genie conversation silently because the durable store is unavailable.
5. Never tell the user that the same conversation was continued when the Genie mapping could not be recovered.

**Python dependencies:**
- `psycopg[binary]>=3.1` (psycopg 3)
- `psycopg-pool>=3.1`
- `databricks-sdk>=0.118.0` (already present)

Do not modify `requirements.txt` in this task. Dependencies are listed here for Phase 2 planning only.

---

## Summary

| Item | Action |
|---|---|
| Create Lakebase project | Required before live integration / deployment. Phase 2A standalone module does not require infrastructure. |
| Project ID | `transparence-sessions` (proposed) |
| Branch | `production` (auto-created) |
| Endpoint | `primary` (auto-created) |
| Database | `databricks_postgres` (auto-created) |
| Attach to app | Add `resources:` block to app.yaml, redeploy |
| Permission | `CAN_CONNECT_AND_CREATE` |
| Resource key in app.yaml | `genie_session_db` |
| PG* vars auto-injected after attach | YES |
| Code changes for Phase 2A | Standalone `app/repository/conversation_repository.py` module + unit tests only. No app wire-up in Phase 2A. |
