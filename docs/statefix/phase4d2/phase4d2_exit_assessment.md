# Phase 4D2 — Exit Assessment (Correction)

## Status: PASS WITH EXTERNAL PERMISSION BLOCKERS

---

## Corrections Applied

This correction addresses three issues in the original Phase 4D2 report:

| Issue | Description | Resolution |
|---|---|---|
| Issue 1 | Suite arithmetic: 1473+74=1664 (wrong, correct is 1547→now 1561 after adding 14 more tests) | Arithmetic verified via /tmp run; exact 34-file suite documented |
| Issue 2 | TEST_DEPLOYMENT_FLAGS misclassified as controlled test deployment profile | Renamed to CONNECTIVITY_SMOKE_FLAGS; added CONTROLLED_TEST_DEPLOYMENT_FLAGS; added 3 ReadinessReport tier fields; added 14 profile-separation tests |
| Issue 3 | CANNOT_VERIFY permissions classified as PRESENT_AND_SUFFICIENT | Classification corrected; CANNOT_VERIFY permissions now correctly block controlled_test_deployment_ready |

---

## Previous Phase 4D2 SHA

`a51a90b12fc5ff3e74744fe109da0428f1984451`

---

## Readiness Assessment

### Readiness Service Output (Profile B environment, no permission snapshot)

| Field | Value |
|---|---|
| `connectivity_smoke_ready` | **True** (when Genie config is valid) |
| `controlled_test_deployment_ready` | **False** |
| `production_ready` | **False** |
| `deployment_sync_required` | **True** |
| `overall_ready` | **True** (domain checks pass) |

### Why controlled_test_deployment_ready=False

Two mandatory permissions remain CANNOT_VERIFY_NON_DESTRUCTIVELY:
1. **Genie Space CAN_RUN** — no non-destructive API for Genie Space permissions
2. **SQL Warehouse CAN_USE** — no CAN_MANAGE access to enumerate grants

Both permissions must be explicitly confirmed by DevOps/IT before controlled deployment.

---

## All Gates

| Gate | Result |
|---|---|
| Issue 1: Suite arithmetic | PASS — 1473+88=1561 (34-file), 2300+88=2388 (non-live) |
| Issue 2: Profile definitions | PASS — 3 distinct profiles with clear semantics |
| Issue 3: Permission classification | PASS — CANNOT_VERIFY correctly blocks controlled deployment |
| 61 configuration tests | PASS |
| 27 permission tests | PASS |
| 34-file suite | PASS — 1561/1561 |
| Complete non-live suite | PASS — 2388/2388, 0 skipped |
| No deployment performed | CONFIRMED |
| No live Lakebase/Genie | CONFIRMED |
| No production data modified | CONFIRMED |
| uv not staged | CONFIRMED |

---

## Profile A — Connectivity Smoke

**Safe to run when:**
- App is deployed and running
- Genie Space is shared with SP (CAN_RUN confirmed separately)
- Warehouse has CAN_USE for SP

**Validates only:** app startup, Genie request/response, basic rendering.

**Does NOT authorise controlled test deployment.**

---

## Profile B — Controlled Test Deployment

**Blocked by:**
1. Genie Space CAN_RUN: CANNOT_VERIFY_NON_DESTRUCTIVELY → DevOps/IT to confirm
2. SQL Warehouse CAN_USE: CANNOT_VERIFY_NON_DESTRUCTIVELY → DevOps/IT to confirm
3. Source-sync: git repo must be synchronised to `transparence_app/` → deployment preparation

**Not blocked by:**
- Domain configuration (all checks pass)
- Lakebase permissions (confirmed PRESENT_AND_SUFFICIENT via Phase 2B2B2)
- Shipment table SELECT (confirmed PRESENT_AND_SUFFICIENT via UC API)
- Secret binding (confirmed PRESENT_AND_SUFFICIENT via app resource config)

---

## DevOps/IT Actions Required Before Controlled Test Deployment

1. **Genie Space CAN_RUN**: Confirm `app-31pcl9 transparence` (SP 78664835752275) has CAN_RUN on Genie Space `01f17a93e6aa1b97a9da7ef329e15e46` via the Genie Space Share dialog.

2. **SQL Warehouse CAN_USE**: Confirm SP has CAN_USE on warehouse `8e46614f7064d8fd` via Workspace UI → SQL Warehouses → Permissions.

3. **Source sync**: Synchronise `Transparence_2_0_git` git repository to `transparence_app/` source path before deployment.

---

## Files Changed (Correction)

- `app/services/production_readiness.py` — 4 new ReadinessReport fields, `_check_controlled_deployment_flags`, `CONNECTIVITY_SMOKE_FLAGS`, `CONTROLLED_TEST_DEPLOYMENT_FLAGS`, updated `check_production_readiness`
- `tests/test_production_readiness_configuration.py` — updated imports/tests, added GROUP 9 (14 new tests, total 61)
- `docs/statefix/phase4d2/configuration_contract.md` — 3 profiles, corrected semantics
- `docs/statefix/phase4d2/permissions_matrix.md` — corrected classifications, CANNOT_VERIFY properly noted
- `docs/statefix/phase4d2/test_report.md` — corrected arithmetic (1561, 2388), corrected counts
- `docs/statefix/phase4d2/phase4d2_exit_assessment.md` — this file

`tests/test_production_readiness_permissions.py` — unchanged (correctly handles CANNOT_VERIFY already).

---

## Phase 4D2: PASS WITH EXTERNAL PERMISSION BLOCKERS

Controlled test deployment is NOT safe while Genie Space CAN_RUN and SQL Warehouse CAN_USE remain CANNOT_VERIFY_NON_DESTRUCTIVELY. DevOps/IT must supply explicit evidence before proceeding.

Connectivity smoke (Profile A) is safe to run independently once SP permissions are confirmed for Genie Space and warehouse.
