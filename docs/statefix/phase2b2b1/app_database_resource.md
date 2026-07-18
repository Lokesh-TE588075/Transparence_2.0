# Phase 2B2B1 — App Database Resource Attachment

## Summary

Phase 2B2B1 attached one Lakebase Autoscaling database resource to the
`transparence` Databricks App. No deployment was triggered, no source files
changed, and the running app was not restarted.

## Resource Target

| Field           | Value                                                                                          |
|-----------------|------------------------------------------------------------------------------------------------|
| App name        | `transparence`                                                                                 |
| Resource key    | `postgres`                                                                                     |
| Resource type   | Lakebase Autoscaling (`AppResourcePostgres`)                                                   |
| Branch path     | `projects/transparence-sessions/branches/production`                                          |
| Database path   | `projects/transparence-sessions/branches/production/databases/databricks-postgres`             |
| Postgres DB     | `databricks_postgres`                                                                          |
| Permission      | `CAN_CONNECT_AND_CREATE` (display: *Can connect and create*)                                   |

## Method Used

Attachment was performed via the **Databricks Python SDK 0.121.0**
(`w.apps.update`) using the `AppResourcePostgres` class.  The older SDK
0.67.0 lacked this class and only exposed `AppResourceDatabase` (for
Provisioned instances).  The upgrade to `>=0.118.0` was required.

```python
# Conceptual representation — not to be re-executed
from databricks.sdk.service.apps import (
    App, AppResource, AppResourcePostgres,
    AppResourcePostgresPostgresPermission,
)
w.apps.update(
    name="transparence",
    app=App(
        name="transparence",
        resources=[
            AppResource(
                name="postgres",
                postgres=AppResourcePostgres(
                    branch="projects/transparence-sessions/branches/production",
                    database="projects/transparence-sessions/branches/production/databases/databricks-postgres",
                    permission=AppResourcePostgresPostgresPermission.CAN_CONNECT_AND_CREATE,
                ),
            )
        ],
    ),
)
```

## State Before Attachment

- `resources` field on app: `[]` (zero resources)
- Active deployment: `01f181110277111f8f8d22379e477ecc` (SUCCEEDED)
- App status: `RUNNING`
- Compute status: `ACTIVE`

## State After Attachment

- `resources` field on app: one entry with key `postgres`
- Active deployment: `01f181110277111f8f8d22379e477ecc` (UNCHANGED)
- Pending deployment: none
- App status: `RUNNING` (unchanged)
- Compute status: `ACTIVE` (unchanged)

## Verified Resource Config (from `apps get transparence`)

```json
{
  "name": "postgres",
  "postgres": {
    "branch": "projects/transparence-sessions/branches/production",
    "database": "projects/transparence-sessions/branches/production/databases/databricks-postgres",
    "permission": "CAN_CONNECT_AND_CREATE"
  }
}
```

## app.yaml Status

`app.yaml` in the source tree (`transparence_app/app.yaml`) was **not modified**.
The resource attachment is stored in the platform's app configuration only.
The new `PGHOST`, `PGDATABASE`, `PGUSER`, `PGPORT` environment variables will
be injected by the platform into the app process on the **next deployment**.

## Rollback Procedure

> Documentation only — do not execute without explicit authorisation.

To remove this resource attachment:

```python
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.apps import App
w = WorkspaceClient()
w.apps.update(name="transparence", app=App(name="transparence", resources=[]))
```

This sets the resource list to empty.  The Postgres role
`dbrx-apps-488a0acb-5804-42f0-98b1-a02cc13c4573` will remain on the branch
but will lose CONNECT/CREATE on `databricks_postgres`.  No schemas or tables
will be deleted (none were created in this phase).

The Postgres role can then be removed manually via the Lakebase UI or
`postgres delete-role` CLI command if desired.
