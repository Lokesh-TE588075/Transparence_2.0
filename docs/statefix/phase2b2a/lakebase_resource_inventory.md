# Phase 2B2A — Lakebase Resource Inventory

All identifiers recorded here were obtained via read-only SDK calls
(`w.postgres.get_project`, `list_branches`, `list_endpoints`, `list_databases`,
`list_roles`, `get_endpoint`). No SQL was executed. No credentials are printed.

---

## Project

| Field                       | Value                                             |
|-----------------------------|---------------------------------------------------|
| Project ID                  | `transparence-sessions`                           |
| Resource name               | `projects/transparence-sessions`                  |
| Display name                | `TransparencE Sessions`                           |
| PostgreSQL version          | `17`                                              |
| Region                      | `us-east-1` (AWS)                                 |
| Owner                       | `lokesh.choraria@te.com`                          |
| Default branch              | `projects/transparence-sessions/branches/production` |
| History retention           | `604800 s` (7 days)                               |
| Branch size limit           | `17,592,186,044,416 bytes` (∼16 TiB)             |
| Project state               | ACTIVE (no explicit state field in SDK v0.121.0;  |
|                             | owner+id present and branch READY confirm active) |
| Created (approx.)           | `2026-07-18T10:59:30Z UTC` (branch state_change_time) |

---

## Branch: production

| Field          | Value                                                            |
|----------------|------------------------------------------------------------------|
| Branch ID      | `production`                                                     |
| Resource name  | `projects/transparence-sessions/branches/production`             |
| Display name   | `production`                                                     |
| Branch type    | Default / production (not reported by API — only branch in project) |
| State          | `READY`                                                          |
| Default        | `True`                                                           |
| Is protected   | `False`                                                          |
| Logical size   | `0 bytes` (newly created)                                        |
| READY since    | `2026-07-18T10:59:30Z UTC`                                       |

---

## Database: databricks-postgres

| Field          | Value                                                                          |
|----------------|--------------------------------------------------------------------------------|
| Database ID    | `databricks-postgres`                                                          |
| Resource name  | `projects/transparence-sessions/branches/production/databases/databricks-postgres` |
| Database name  | `databricks-postgres` (resource ID; actual PG DB name: `databricks_postgres`)  |
| Owner          | Not exposed via list API (platform-managed)                                    |
| Type           | Platform-generated default database                                            |

> **Note:** The resource ID uses hyphens (`databricks-postgres`) per RFC 1123.
> The actual PostgreSQL database name inside the instance uses underscores
> (`databricks_postgres`). Use the underscore form in psycopg/SQLAlchemy
> connection strings.

---

## Endpoint: primary

| Field                | Value                                                                              |
|----------------------|------------------------------------------------------------------------------------|
| Endpoint ID          | `primary`                                                                          |
| Resource name        | `projects/transparence-sessions/branches/production/endpoints/primary`             |
| Endpoint type        | `ENDPOINT_TYPE_READ_WRITE`                                                         |
| State                | `IDLE` (scale-to-zero suspended — expected; wakes in ~100 ms on first connection)   |
| Autoscaling min CU   | `0.5` (∼1 GB RAM)                                                                  |
| Autoscaling max CU   | `1.0` (∼2 GB RAM)                                                                  |
| Scale-to-zero        | Enabled (platform default; `no_suspension` not set)                               |
| Suspend timeout      | `300 s` (5 minutes)                                                                |
| HA (secondaries)     | Disabled — `EndpointGroupStatus(min=1, max=1, enable_readable_secondaries=False)` |
| Disabled flag        | `False`                                                                            |
| Host                 | **REDACTED** — not recorded here; obtainable via `w.postgres.get_endpoint(name=...).status.hosts.host` |
| Read-write pooled    | **REDACTED** — same endpoint, pooler variant                                      |
| Last active time     | `1970-01-01T00:00:00Z` (platform default placeholder for idle endpoint)            |

---

## Auto-created Role

| Field            | Value                                                             |
|------------------|-------------------------------------------------------------------|
| Role ID          | `lokesh-choraria`                                                 |
| Resource name    | `projects/transparence-sessions/branches/production/roles/lokesh-choraria` |
| Postgres role    | `lokesh.choraria@te.com`                                          |
| Identity type    | `USER`                                                            |
| Auth method      | `LAKEBASE_OAUTH_V1`                                               |
| Membership       | `DATABRICKS_SUPERUSER`                                            |
| Attributes       | `bypassrls=True`, `createdb=True`, `createrole=True`             |

---

## Resource Hierarchy Summary

```
projects/transparence-sessions
  └── branches/production                    [READY]
        ├── endpoints/primary                 [IDLE — scale-to-zero]
        ├── databases/databricks-postgres      [present]
        └── roles/lokesh-choraria              [DATABRICKS_SUPERUSER]
```
