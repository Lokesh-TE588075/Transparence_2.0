# Phase 4D2 — Permission Evidence Matrix

## Status: CORRECTED

## Classification Rules

- **PRESENT_AND_SUFFICIENT**: Supported by explicit evidence from non-destructive API inspection.
- **PRESENT_BUT_INSUFFICIENT**: Permission exists but grants fewer rights than required.
- **MISSING**: Confirmed absent.
- **CANNOT_VERIFY_NON_DESTRUCTIVELY**: No safe API to confirm; DevOps/IT must validate before controlled deployment.
- **NOT_REQUIRED**: Not needed in the current architecture.

A permission may be classified PRESENT_AND_SUFFICIENT ONLY when supported by explicit evidence.
CANNOT_VERIFY is treated as a blocker for `controlled_test_deployment_ready`.

---

## Permission Matrix

| Resource | Principal | Required Level | Evidence Source | Status | Blocking Owner | Validation Needed |
|---|---|---|---|---|---|---|
| Genie Space `01f17a93e6aa1b97a9da7ef329e15e46` | SP `app-31pcl9 transparence` (78664835752275) | CAN_RUN | None available — no non-destructive API for Genie Space permissions | **CANNOT_VERIFY_NON_DESTRUCTIVELY** | DevOps/IT | Grant CAN_RUN via Genie Space Share dialog; confirm via live smoke test |
| SQL Warehouse `8e46614f7064d8fd` | SP `app-31pcl9 transparence` (78664835752275) | CAN_USE | No CAN_MANAGE — permissionLevels API only; cannot inspect grants | **CANNOT_VERIFY_NON_DESTRUCTIVELY** | DevOps/IT | Confirm via Workspace UI → SQL Warehouses → Permissions |
| `onedata_fn_ion_dev.ion_l0_raw.lbn_with_scorecard` | SP `app-31pcl9 transparence` (78664835752275) | SELECT | Unity Catalog effective-permissions API confirmed SELECT + MODIFY | **PRESENT_AND_SUFFICIENT** | — | None |
| Lakebase project `projects/transparence-sessions/branches/production` | SP via app binding | CAN_CONNECT_AND_CREATE | App resource config shows `postgres` binding with CAN_CONNECT_AND_CREATE level | **PRESENT_AND_SUFFICIENT** | — | Confirm app is running with binding active |
| Lakebase `transparence_state.app_conversation` | SP PG role | SELECT, INSERT, UPDATE | GRANT statement executed in Phase 2B2B2; confirmed with `\dp` in psql session | **PRESENT_AND_SUFFICIENT** | — | None — grants are permanent |
| Lakebase `transparence_state.app_conversation` | SP PG role | DELETE | Not required by app_conversation lifecycle (soft-delete only; hard-delete flag=false) | **NOT_REQUIRED** | — | None |
| Secret `transparence-owner-identity/conversation-owner-hmac-v1` | SP via app binding | READ | App resource config shows `conversation-owner-hmac-secret` binding with READ scope | **PRESENT_AND_SUFFICIENT** | — | Confirm app binding is active |
| Genie Space `01f17a93e6aa1b97a9da7ef329e15e46` | Authenticated end users | CAN_VIEW | Not needed — SP executes all Genie calls on behalf of users | **NOT_REQUIRED** | — | None |

---

## Outstanding Unverifiable Permissions

| Resource | Principal | Why Unverifiable | DevOps/IT Action Required |
|---|---|---|---|
| Genie Space CAN_RUN | SP `app-31pcl9 transparence` | No non-destructive API endpoint exists for Genie Space permission inspection | Grant via Genie Space → Share dialog: "Can Run" for `app-31pcl9 transparence`. Confirm via live Genie smoke test (Profile A). |
| SQL Warehouse CAN_USE | SP `app-31pcl9 transparence` | Only permissionLevels endpoint accessible; not CAN_MANAGE — cannot enumerate grants | Verify via Workspace UI → SQL Warehouses → `8e46614f7064d8fd` → Permissions. Confirm SP has CAN_USE. |

**These two permissions must be explicitly confirmed before `controlled_test_deployment_ready` can be True.**

---

## Impact on Readiness Gates

| Gate | Result | Reason |
|---|---|---|
| `connectivity_smoke_ready` | **True** (when config is valid) | Genie config present; domain checks pass |
| `controlled_test_deployment_ready` | **False** | Genie Space CAN_RUN and SQL Warehouse CAN_USE are CANNOT_VERIFY_NON_DESTRUCTIVELY |
| `production_ready` | **False** | Same blockers as controlled_test_deployment_ready |
| `deployment_sync_required` | **True** | Active deployment is from `transparence_app/`, not git repo |

`controlled_test_deployment_ready` will remain False until:
1. DevOps/IT confirms Genie Space CAN_RUN and SQL Warehouse CAN_USE
2. A permission snapshot with all mandatory permissions PRESENT_AND_SUFFICIENT is provided to `check_production_readiness()`
3. The DEPLOYMENT_PREPARATION_BLOCKER is resolved (git sync)
