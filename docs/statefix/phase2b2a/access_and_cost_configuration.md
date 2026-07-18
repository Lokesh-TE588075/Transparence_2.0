# Phase 2B2A — Access Control and Cost Configuration

---

## Access Control Findings

### Interactive User

| Field                | Value                                    |
|----------------------|------------------------------------------|
| User                 | `lokesh.choraria@te.com`                 |
| Project owner        | YES — `owner` field set to user          |
| Postgres role        | `lokesh-choraria` (auto-created)         |
| Membership           | `DATABRICKS_SUPERUSER`                   |
| Auth method          | `LAKEBASE_OAUTH_V1`                      |
| Can manage project   | YES (owner + superuser)                  |
| CAN MANAGE confirmed | INFERRED from owner + superuser role      |
|                      | (no explicit IAM permission API queried) |

### TransparencE App Service Principal

| Field                    | Value                                          |
|--------------------------|------------------------------------------------|
| SP name                  | `app-31pcl9 transparence`                      |
| SP ID                    | `78664835752275`                               |
| SP client ID             | `488a0acb-5804-42f0-98b1-a02cc13c4573`         |
| Role on this project     | **NONE** — confirmed via `list_roles`          |
| Database access          | **NONE**                                       |
| App resource attached    | **NO**                                         |

### Other Principals

No other principals were observed with roles on this project at provisioning
time. The `list_roles` API returned exactly one role (the creator's).

---

## Compute and Cost Configuration

### Endpoint Autoscaling

| Parameter             | Value    | Source           |
|-----------------------|----------|------------------|
| Min compute units     | `0.5 CU` | Explicitly set   |
| Max compute units     | `1.0 CU` | Explicitly set   |
| CU range              | `0.5 CU` | < 16 CU limit ✓ |
| RAM at min            | ~1 GB    | 2 GB/CU          |
| RAM at max            | ~2 GB    | 2 GB/CU          |

### Scale-to-Zero

| Parameter             | Value                          | Source                        |
|-----------------------|--------------------------------|-------------------------------|
| Scale-to-zero         | Enabled                        | Platform default (no_suspension not set) |
| Inactivity timeout    | `300 s` (5 minutes)            | Explicitly set                |
| Wake-up latency       | ~100 ms (platform documented)  |                               |

**Note on `no_suspension` field behavior (API discovery in this phase):**
The Lakebase API rejects `no_suspension=False` with
`InvalidParameterValue: no_suspension must be true when set`.
The field is a one-directional opt-out from scale-to-zero.
To enable scale-to-zero (the desired behavior), simply omit the field.

### High Availability

| Parameter              | Value                                             |
|------------------------|---------------------------------------------------|
| HA secondaries         | Disabled                                          |
| Readable secondaries   | `False`                                           |
| Primary count          | 1 (`EndpointGroupStatus: min=1, max=1`)           |

### Storage

| Parameter            | Value                               |
|----------------------|-------------------------------------|
| Branch size limit    | 17,592,186,044,416 bytes (~16 TiB)  |
| Logical size at rest | 0 bytes (newly created)             |
| History retention    | 604,800 s (7 days)                  |

---

## Cost Expectations

- Compute charges accrue only when the endpoint is **active** (not IDLE).
- With scale-to-zero enabled and a 5-minute timeout, cost is near-zero when
  no sessions are active.
- At 0.5 CU minimum, this is the smallest billable Lakebase compute unit.
- Storage cost is negligible at 0 bytes logical size.
- For development use, expected cost: fraction of a CU-hour per actual usage.

---

## Phase 2B2B Access Prerequisites

The following grants are needed in Phase 2B2B but must **not** be performed
before the app resource is attached:

1. The Lakebase project must be attached to the `transparence` app resource
   via `apps update-resource` (Phase 2B2B).
2. The app SP will receive a database connection credential after attachment.
3. The `app_conversation` schema and table are created by the SP in Phase 2B2B
   (SP becomes owner of any objects it creates).
4. The interactive user retains DATABRICKS_SUPERUSER for DML access during
   local development.

Do **not** grant SP access to this project before Phase 2B2B app attachment.
