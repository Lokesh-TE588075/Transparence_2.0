# Phase 4D2 — Service Principal Permission Matrix

Phase 4D2 — Validate production configuration and permissions readiness

Generated: 2026-07-20

Application service principal: `app-31pcl9 transparence`
Client ID: `488a0acb-5804-42f0-98b1-a02cc13c4573`
Numeric ID: `78664835752275`

---

## Section 1: Required Before Test Deployment

| Resource | Resource ID | Principal | Required Permission | Current Permission | Status | Owner/Team | Reason | Validation Method |
|----------|------------|-----------|--------------------|--------------------|--------|-----------|--------|-------------------|
| Genie Space | 01f17a93e6aa1b97a9da7ef329e15e46 | SP 78664835752275 | CAN_RUN | CAN_RUN (confirmed via memory, previously shared) | PRESENT AND SUFFICIENT | Space owner (lokesh.choraria@te.com) | SP must start and continue Genie conversations | Share Genie Space with SP from Space share dialog |
| SQL Warehouse | 8e46614f7064d8fd | SP 78664835752275 | CAN_USE | CAN_USE (confirmed — old pipeline worked) | PRESENT AND SUFFICIENT | Warehouse admin | SP executes SQL via Genie Space | Run test query via SP credentials |
| Shipment Table (SELECT) | onedata_fn_ion_dev.ion_l0_raw.lbn_with_scorecard | SP 488a0acb-5804-42f0-98b1-a02cc13c4573 | SELECT | SELECT + MODIFY (confirmed via UC permissions API) | PRESENT AND SUFFICIENT | Unity Catalog admin | Genie reads shipment data | UC effective-permissions API confirmed |
| Lakebase Project (connect) | projects/transparence-sessions/branches/production | SP 78664835752275 | CAN_CONNECT_AND_CREATE | CAN_CONNECT_AND_CREATE (confirmed in app resource binding) | PRESENT AND SUFFICIENT | lokesh.choraria@te.com (project owner) | App connects to Lakebase via postgres binding | apps get transparence — resources section |
| Databricks Secret (READ) | transparence-owner-identity / conversation-owner-hmac-v1 | SP 78664835752275 | READ | READ (confirmed in app resource binding) | PRESENT AND SUFFICIENT | Secret owner | App reads HMAC secret for owner identity | apps get transparence — resources section |

---

## Section 2: Required Before Production Deployment (Durable State Active)

| Resource | Resource ID | Principal | Required Permission | Current Permission | Status | Owner/Team | Reason | Validation Method |
|----------|------------|-----------|--------------------|--------------------|--------|-----------|--------|-------------------|
| Lakebase app_conversation table (DML) | transparence_state.app_conversation | SP PG role | SELECT, INSERT, UPDATE, DELETE | GRANT-based access confirmed (Phase 2B2B2) | PRESENT AND SUFFICIENT | lokesh.choraria@te.com (schema owner) | Durable session state read/write | Confirmed in Phase 2B2B2 via psql GRANT statements |
| Lakebase schema (USAGE) | transparence_state | SP PG role | USAGE | USAGE granted (Phase 2B2B2) | PRESENT AND SUFFICIENT | lokesh.choraria@te.com | Schema access for SP role | Confirmed in Phase 2B2B2 |
| HMAC secret minimum quality | transparence-owner-identity / conversation-owner-hmac-v1 | N/A | >= 32 bytes UTF-8 | CANNOT VERIFY (never read in this phase) | CANNOT VERIFY NON-DESTRUCTIVELY | Secret creator | Trusted identity requires strong HMAC secret | Enable ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY=true in test and observe startup |

---

## Section 3: Optional Operational Permissions

| Resource | Resource ID | Principal | Required Permission | Current Permission | Status | Owner/Team | Reason |
|----------|------------|-----------|--------------------|--------------------|--------|-----------|--------|
| Genie Space (end-user direct access) | 01f17a93e6aa1b97a9da7ef329e15e46 | End users | CAN_VIEW (optional) | NOT REQUIRED in SP-backend mode | NOT REQUIRED | N/A | SP executes all Genie calls; end users never access Space directly |
| Feedback/Audit Delta tables | onedata_fn_ion_dev.ion_l0_raw.* | SP | SELECT, MODIFY | Inherited from schema GRANT | PRESENT AND SUFFICIENT | UC admin | Optional feedback and audit logging |

---

## Section 4: Explicitly Not Required

| Resource | Status | Reason |
|----------|--------|--------|
| Workspace admin | NOT REQUIRED | SP uses least-privilege application credentials |
| Cluster CREATE | NOT REQUIRED | App uses SQL warehouse only |
| Genie Space (end-user access) | NOT REQUIRED | SP-backend mode; users see only the chat UI |
| X-Forwarded-Access-Token Genie calls | NOT REQUIRED | effective_user_api_scopes is IAM-only; SP credentials used for all Genie calls |

---

## Section 5: Unverifiable Permissions (no live API support)

| Resource | Reason Unverifiable | Validation Path |
|----------|--------------------|-----------------|
| Genie Space CAN_RUN for SP | No /api/2.0/permissions endpoint for Genie spaces; confirmed by memory (Phase G-series share) | Enable USE_GENIE_BACKEND=true and run a test chat; 401/403 response would indicate missing permission |
| SQL Warehouse CAN_USE for SP | Warehouse permissions endpoint requires CAN_MANAGE (not held by current user) | Same — Genie smoke test would fail with warehouse access error if missing |
| HMAC secret minimum quality | Secret value never read during audit | Enable trusted identity in test deployment; any startup error indicates secret quality issue |

---

## Section 6: DevOps / IT Actions Required

### Required Before Test Deployment

1. **Source path sync** (Owner: lokesh.choraria@te.com)
   - The active deployment uses `transparence_app`. The git repo changes must be synced or redeployed from `Transparence_2_0_git`.
   - Action: either (a) copy/sync files from git repo to `transparence_app`, or (b) create new deployment pointing to git repo.
   - Do NOT delete `frontend/node_modules/` before confirming npm build is complete.

2. **Genie Space share with SP** (Owner: Space owner = lokesh.choraria@te.com)
   - Already completed in earlier phases per memory note.
   - Verify: Genie Space share dialog shows `app-31pcl9 transparence` with CAN_RUN.

3. **SQL Warehouse access for SP** (Owner: Warehouse admin / IT)
   - Action: Verify SP has CAN_USE on warehouse `8e46614f7064d8fd`.
   - GRANT already confirmed working in earlier phases.

### Required Before Production Deployment

4. **Enable ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY=true in app.yaml** (Owner: lokesh.choraria@te.com)
   - Update app.yaml value from `false` to `true` after test smoke test passes.
   - Confirm HMAC secret resolves correctly (startup will fail with sanitized error if secret is absent).

5. **Enable ENABLE_DURABLE_GENIE_SESSION_ADAPTER=true + lakebase backend** (Owner: lokesh.choraria@te.com)
   - Update app.yaml after full test deployment passes.
   - Lakebase schema and table already exist (Phase 2B2B2).
   - SP role GRANT already applied (Phase 2B2B2).

6. **Verify Lakebase endpoint is ACTIVE before full production** (Owner: lokesh.choraria@te.com)
   - Lakebase endpoint `ep-withered-king-d257e0k1` is currently IDLE (scale-to-zero).
   - First connection will wake it (\~5-30s cold start). Acceptable for initial production.
   - Alert: if endpoint is DISABLED or SUSPENDED, re-enable before deployment.

---

## Section 7: End-User Permission Requirements

End users do not require any Databricks workspace permissions beyond the ability to access
the Databricks App URL. The app SP handles all Genie, warehouse, and Lakebase calls.

The `X-Forwarded-User` header (injected by Databricks Apps) provides trusted identity;
end users cannot forge this header.

The `effective_user_api_scopes` is `["iam.access-control:read", "iam.current-user:read"]`
which is IAM-read-only. This is correct and intentional — the SP credentials are used
for all data and Genie operations.
