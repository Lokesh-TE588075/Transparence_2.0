# Phase 2B2A — Lakebase Project Provisioning

## Summary

Phase 2B2A provisioned one Lakebase Autoscaling Postgres project in the
`te-ss-coe-dev` workspace as the persistence backend for TransparencE Genie
session state. The project was **newly created** (no prior project existed).

## Workspace Context

| Field             | Value                                         |
|-------------------|-----------------------------------------------|
| Workspace URL     | `te-ss-coe-dev.cloud.databricks.com`          |
| Workspace ID      | `4310366453016539`                            |
| Cloud             | AWS                                           |
| Region            | `us-east-1`                                   |
| Metastore         | `te-databricks-metastore`                     |
| Authenticated user| `lokesh.choraria@te.com`                      |

## SDK Used

| SDK            | Version  | Notes                                                  |
|----------------|----------|--------------------------------------------------------|
| databricks-sdk | `0.121.0`| Upgraded from `0.67.0` — prior version lacked          |
|                |          | `databricks.sdk.service.postgres` entirely.            |
|                |          | requirements.txt was **not** modified.                 |

## Pre-Creation Checks

| Check                           | Result                                           |
|---------------------------------|--------------------------------------------------|
| Pre-existing projects           | 0 (workspace was empty)                          |
| Exact match `transparence-sessions` | Not found — safe to create                  |
| Similar names (`transparence*`) | None found                                       |
| Workspace project quota         | 0 / 1000 — quota permits creation               |
| `pg_version=17` supported       | Confirmed — valid field in `ProjectSpec`        |
| Region (AWS us-east-1)          | Lakebase Autoscaling GA on AWS                   |
| `list_projects` permission      | CONFIRMED — API call succeeded                   |
| `create_project` permission     | INFERRED — workspace user; no explicit blocker   |

## Creation Call

```python
w.postgres.create_project(
    project=Project(spec=ProjectSpec(
        display_name="TransparencE Sessions",
        pg_version=17,
        default_endpoint_settings=ProjectDefaultEndpointSettings(
            autoscaling_limit_min_cu=0.5,
            autoscaling_limit_max_cu=1.0,
            # no_suspension NOT SET — platform default = scale-to-zero enabled
            suspend_timeout_duration=Duration(seconds=300),
        ),
    )),
    project_id="transparence-sessions",
)
```

**API note:** Setting `no_suspension=False` explicitly is rejected by the API
(`InvalidParameterValue: no_suspension must be true when set`). Scale-to-zero
is the platform default and does not require explicit opt-in. The field was
omitted after discovering this constraint through a rejected first attempt.

## Outcome

- Project **newly created** — confirmed via `w.postgres.get_project()`
  immediately after the LRO call returned.
- `op.wait(timeout=...)` is unsupported in this SDK version; the project was
  verified via `get_project` polling instead.
- Branch `production` reached state `READY` at `2026-07-18T10:59:30Z UTC`.
- Endpoint `primary` is in state `IDLE` (scale-to-zero suspended — expected
  for a new endpoint with scale-to-zero enabled; wakes in ~100 ms).

## What Was Not Done

- No SQL was executed.
- No custom database was created.
- No custom schema was created.
- No custom table was created.
- No application role was created manually.
- No app resource was attached.
- No deployment occurred.
- The running TransparencE app was not modified.
