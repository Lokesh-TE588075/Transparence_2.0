# Phase 2B2B1 — Service Principal Database Role

## App Service Principal Identity

| Field                      | Value                                          |
|----------------------------|------------------------------------------------|
| App name                   | `transparence`                                 |
| SP display name            | `app-31pcl9 transparence`                      |
| SP client ID (application) | `488a0acb-5804-42f0-98b1-a02cc13c4573`         |
| SP numeric ID              | `78664835752275`                               |
| App platform ID            | `488a0acb-5804-42f0-98b1-a02cc13c4573`         |

## Postgres Role Created by Platform

When the `postgres` resource was attached, the Lakebase platform automatically
created a Postgres role for the app service principal.

| Field              | Value                                                                          |
|--------------------|--------------------------------------------------------------------------------|
| Role resource name | `projects/transparence-sessions/branches/production/roles/dbrx-apps-488a0acb-5804-42f0-98b1-a02cc13c4573` |
| role_id            | `dbrx-apps-488a0acb-5804-42f0-98b1-a02cc13c4573`                              |
| postgres_role      | `488a0acb-5804-42f0-98b1-a02cc13c4573`                                        |
| identity_type      | `SERVICE_PRINCIPAL`                                                            |
| auth_method        | `LAKEBASE_OAUTH_V1`                                                            |
| created_at         | `2026-07-18T11:49:06Z`                                                         |

## Role Attributes

| Attribute    | Value   | Meaning                                         |
|--------------|---------|-------------------------------------------------|
| `bypassrls`  | `false` | Row-level security is NOT bypassed — safe       |
| `createdb`   | `false` | Cannot create new Postgres databases            |
| `createrole` | `false` | Cannot create other Postgres roles              |
| `membership_roles` | (absent) | Not a member of `DATABRICKS_SUPERUSER`  |

The role is **not a Postgres superuser** and carries no elevated privileges
beyond what was explicitly granted.

## Database-Level Privileges Granted

The platform granted the following privileges on database `databricks_postgres`:

| Privilege  | Granted | Effect                                                     |
|------------|---------|------------------------------------------------------------||
| `CONNECT`  | YES     | App can connect to `databricks_postgres`                   |
| `CREATE`   | YES     | App can create schemas within `databricks_postgres`        |

Source: Databricks documentation confirms that `CAN_CONNECT_AND_CREATE`
automatically issues `GRANT CONNECT, CREATE ON DATABASE databricks_postgres
TO "488a0acb-5804-42f0-98b1-a02cc13c4573"`.

These privileges were granted by the platform; **no manual SQL GRANT was
executed**.

## Objects Owned by This Role

None.  No schema, table, index, or sequence was created by this phase.
The `app_conversation` table does not yet exist.

## Next Step: Schema and Table Creation (Phase 2B2B2)

With CONNECT and CREATE confirmed, the app service principal may:
1. Create a schema (e.g., `transparence_app_schema_488a0acb58044...__`) in
   `databricks_postgres` once a deployment that injects PG env vars is made.
2. Create the `app_conversation` table within that schema.

These operations must NOT be performed until a new deployment (Phase 2B2B3 or
equivalent) injects the `PGHOST`, `PGDATABASE`, `PGUSER`, `PGPORT` environment
variables into the running app.

## Rollback

> Documentation only.

To remove the Postgres role after removing the app resource:

```bash
databricks postgres delete-role \
  projects/transparence-sessions/branches/production/roles/dbrx-apps-488a0acb-5804-42f0-98b1-a02cc13c4573
```

Deleting the role removes its database privileges automatically.  Since no
schemas or tables are owned by this role, there is no data to migrate.
