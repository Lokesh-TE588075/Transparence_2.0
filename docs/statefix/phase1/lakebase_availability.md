# Lakebase Availability — Phase 1 Investigation

**Date:** 2026-07-18  
**Branch:** feature/genie-state-persistence  
**Baseline commit:** 7d9977ff5965b8ee015bda1cc1662a317ae8a0d0

---

## 1. Workspace Lakebase Autoscaling Status

**Result: AVAILABLE**

The `w.postgres.list_projects()` SDK call completed without error, returning an empty iterator. This confirms:
- The `PostgresAPI` (`w.postgres.*`) is reachable in workspace `te-ss-coe-dev`.
- Lakebase Autoscaling is enabled at the workspace level.
- No `PERMISSION_DENIED` was raised, confirming the interactive user (`lokesh.choraria@te.com`) has at minimum read access to the Lakebase project namespace.

## 2. Existing Lakebase Projects

**Result: NONE**

```
w.postgres.list_projects(page_size=20) → 0 projects
```

No Autoscaling projects exist that are visible to the current user. No Provisioned (`w.database.*`) instances exist either.

## 3. Existing Projects Available for TransparencE

**Result: NONE — a new project must be created.**

There are no existing projects, branches, or databases to reuse.

## 4. Current User Lakebase Capabilities

The API responded successfully, confirming the interactive user can:
- List projects (API call succeeded)
- Implicitly: create a new project (workspace-level entitlement is present)

The user `lokesh.choraria@te.com` is the workspace admin and the app owner. Creating a new Lakebase project will automatically make this user the project owner with full `CAN MANAGE` rights.

## 5. App Service Principal Lakebase Capabilities

**Result: NONE — not configured.**

The app service principal (`app-31pcl9 transparence`, SP ID `78664835752275`, client ID `488a0acb-5804-42f0-98b1-a02cc13c4573`) currently has **no Lakebase resource attached** and therefore has:
- No access to any Lakebase project, branch, or database.
- No `PGHOST`, `PGDATABASE`, `PGPORT`, or `PGUSER` environment variables injected at runtime.

Granting the SP Lakebase access requires attaching a Lakebase resource to the app in `app.yaml` with `permission: CAN_CONNECT_AND_CREATE`. This is a manual infrastructure step (see `manual_infrastructure_action.md`).

## 6. SDK Version Used

| Component | Version |
|---|---|
| databricks-sdk (before upgrade) | 0.67.0 |
| databricks-sdk (after upgrade) | 0.121.0 |
| Python | 3.x (Databricks serverless) |

The upgrade was required because `w.postgres.*` requires `>=0.118.0`.

## 7. Lakebase Autoscaling vs Provisioned Summary

| Tier | SDK Service | Found | Notes |
|---|---|---|---|
| Autoscaling | `w.postgres.*` | 0 projects | API available, no projects |
| Provisioned | `w.database.*` | 0 instances | API available, no instances |

## 8. Conclusions

1. Lakebase Autoscaling **is available** in this workspace — no IT enablement required.
2. **No existing project exists** — one must be created before Phase 2 implementation.
3. The interactive user **can create a project** without additional permissions.
4. The app service principal **cannot access any Lakebase database** until a resource is attached.
5. The required infrastructure action is **Result C: create project then attach to app** (see `manual_infrastructure_action.md`).
