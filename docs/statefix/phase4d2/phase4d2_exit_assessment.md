# Phase 4D2 — Exit Assessment

Phase 4D2 — Validate production configuration and permissions readiness

Generated: 2026-07-20

---

## Verdict

**PASS WITH EXTERNAL PERMISSION BLOCKERS**

Code and configuration are ready. The application can be deployed for a controlled Genie
smoke test with test-deployment flags (durable state and trusted identity disabled).
One external operational blocker must be resolved before the code can be served: the
active app deployment source path points to `transparence_app`, not the git repo.

---

## Readiness Checklist

| Item | Status |
|------|--------|
| Required configuration documented | COMPLETE |
| Safe feature-flag combination validated | COMPLETE |
| Required secrets referenced safely | COMPLETE |
| Startup fail-closed behaviour verified | COMPLETE |
| Production readiness service implemented | COMPLETE |
| Configuration tests (>=35): 47 tests | COMPLETE |
| Permission tests (>=25): 27 tests | COMPLETE |
| Focused Phase 4D2 suite: 74 passed | COMPLETE |
| Expanded suite: 1664 passed | COMPLETE |
| Complete non-live suite: 2374 passed | COMPLETE |
| Zero failures | COMPLETE |
| Zero skips | COMPLETE |
| Zero collection errors | COMPLETE |
| Git state clean | COMPLETE |

---

## External Blockers

### BLOCKER 1 (Required before test deployment)

**Source code path mismatch**

- Current active deployment `01f181110277111f8f8d22379e477ecc` uses `transparence_app`.
- Phase 4D2 changes (and all preceding state-persistence changes) are in `Transparence_2_0_git`
  on branch `feature/genie-state-persistence`.
- Owner: lokesh.choraria@te.com
- Resolution: sync git repo content to `transparence_app` or redeploy from git repo.
- WARNING: remember to delete `frontend/node_modules/` after npm build before deployment.

---

## Safe Test Deployment Confirmation

Controlled test deployment is safe when BLOCKER 1 is resolved, with these flag settings:

```
USE_GENIE_BACKEND=true
GENIE_FALLBACK_TO_CUSTOM_PIPELINE=true
ENABLE_DURABLE_GENIE_SESSION_ADAPTER=false
CONVERSATION_REPOSITORY_BACKEND=memory
ENABLE_LAKEBASE_CONVERSATION_REPOSITORY=false
ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY=false
All debug flags: false
CONVERSATION_STATE_CLEANUP_HARD_DELETE=false
```

Expected behaviour in test deployment:
- Genie conversations route through `01f17a93e6aa1b97a9da7ef329e15e46`
- Session state held in-memory (lost on cold start — acceptable for smoke test)
- Reset endpoint returns 503 (trusted identity disabled — acceptable)
- No durable Lakebase writes
- CSV export uses returned_rows_only mode

---

## Known Non-Blockers (documented for awareness)

| Item | Notes |
|------|-------|
| Lakebase endpoint IDLE | Scale-to-zero; wakes on first connection (~5-30s). Not a blocker for test deployment since durable adapter is disabled. |
| HMAC secret quality unverified | Secret value not read during audit. Will surface as sanitized startup error if short/missing when trusted identity is enabled. |
| Genie Space CAN_RUN unverifiable via non-destructive API | Confirmed by prior working deployment history. Will surface as 401/403 in test if missing. |
| Session state loss on cold start | Known gap (diagnosed 2026-07-14); Delta persistence is a post-4D2 work item. |

---

## Production Deployment Gate Sequence

1. Resolve BLOCKER 1 (source path sync)
2. Deploy test deployment with TEST_DEPLOYMENT_FLAGS
3. Smoke test: send 3–5 chat messages via Genie
4. Verify: responses return, no 5xx errors, no secret leakage in logs
5. Enable ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY=true, redeploy, verify reset endpoint returns 200
6. Enable ENABLE_DURABLE_GENIE_SESSION_ADAPTER=true + lakebase backend, redeploy
7. Verify: conversation state survives app restart (cold-start resilience test)
8. Final production promotion

---

## Confirmed: No Prohibited Actions Taken

- No deployment executed
- No app restart triggered
- No live Genie conversation started
- No Lakebase records created, updated, or deleted
- No production data queried
- No assistant memory modified
- No frontend files changed
- No static artifact changes
- No dependency changes
- uv not staged
