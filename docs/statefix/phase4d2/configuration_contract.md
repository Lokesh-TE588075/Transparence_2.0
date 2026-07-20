# Phase 4D2 — Configuration Contract

## Status: CORRECTED

## Three Deployment Profiles

### Profile A — Connectivity Smoke (CONNECTIVITY_SMOKE_FLAGS)

Validates: app startup, Genie connectivity, basic response rendering.

Does NOT validate:
- Trusted owner identity or owner isolation
- Durable session persistence
- Browser-refresh or backend-restart recovery
- Reset tombstone enforcement
- Lakebase integration
- Production fail-closed behaviour

**Must NOT authorise controlled test deployment.**

```
USE_GENIE_BACKEND=true
GENIE_FALLBACK_TO_CUSTOM_PIPELINE=true      # OK for smoke
ENABLE_DURABLE_GENIE_SESSION_ADAPTER=false  # Disabled for smoke
CONVERSATION_REPOSITORY_BACKEND=memory
ENABLE_LAKEBASE_CONVERSATION_REPOSITORY=false
ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY=false  # Disabled for smoke
NEW_PIPELINE_FALLBACK_TO_OLD=true           # OK for smoke
GENIE_EXPORT_MODE=returned_rows_only
GENIE_DEBUG=false
NEW_PIPELINE_DEBUG=false
CONVERSATION_STATE_CLEANUP_HARD_DELETE=false
```

**Readiness result:** `connectivity_smoke_ready=True`, `controlled_test_deployment_ready=False`

---

### Profile B — Controlled Test Deployment (CONTROLLED_TEST_DEPLOYMENT_FLAGS)

Exercises the full target architecture. All mandatory features enabled.

```
USE_GENIE_BACKEND=true
GENIE_FALLBACK_TO_CUSTOM_PIPELINE=false    # No fallback — required
ENABLE_DURABLE_GENIE_SESSION_ADAPTER=true  # Required
CONVERSATION_REPOSITORY_BACKEND=lakebase  # Required
ENABLE_LAKEBASE_CONVERSATION_REPOSITORY=true # Required
ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY=true # Required
NEW_PIPELINE_FALLBACK_TO_OLD=false        # No fallback — required
GENIE_EXPORT_MODE=returned_rows_only
GENIE_DEBUG=false
NEW_PIPELINE_DEBUG=false
CONVERSATION_STATE_CLEANUP_HARD_DELETE=false
```

**Readiness result:** `controlled_test_deployment_ready=True` only when:
1. All domain configuration checks pass
2. All CONTROLLED_DEPLOYMENT_BLOCKER flags satisfied
3. Permission snapshot provided with all mandatory permissions PRESENT_AND_SUFFICIENT
4. DEPLOYMENT_PREPARATION_BLOCKER resolved (git → app sync)

---

### Production (PRODUCTION_FLAGS)

Same mandatory requirements as Profile B at this stage.

No debug, no fallback, no hard-delete, all durable features enabled.

---

## Feature-Flag Dependency Invariants

| # | Invariant |
|---|-----------|
| 1 | ENABLE_DURABLE_GENIE_SESSION_ADAPTER=true REQUIRES ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY=true |
| 2 | CONVERSATION_REPOSITORY_BACKEND=lakebase REQUIRES ENABLE_LAKEBASE_CONVERSATION_REPOSITORY=true |
| 3 | ENABLE_DURABLE_GENIE_SESSION_ADAPTER=true without lakebase backend → warning (in-memory only) |
| 4 | USE_GENIE_BACKEND=true REQUIRES GENIE_SPACE_ID non-empty |
| 5 | CONVERSATION_STATE_CLEANUP_HARD_DELETE must remain false unless explicitly approved |
| 6 | GENIE_EXPORT_MODE must be: returned_rows_only | async_full_query |
| 7 | TRANSPARENCE_DIAGNOSTIC_STORE must be delta | none when tracing enabled |

## Invalid Flag Combinations

| Combination | Result |
|---|---|
| Smoke flags with controlled deployment gate | controlled_test_deployment_ready=False (6 CONTROLLED_DEPLOYMENT_BLOCKER reasons) |
| Durable=true without trusted=true | INVARIANT VIOLATION (domain blocker) |
| lakebase backend without lakebase enabled | INVARIANT VIOLATION (domain blocker) |
| hard-delete=true | Blocking reason (domain blocker) |
| GENIE_FALLBACK=true in controlled check | CONTROLLED_DEPLOYMENT_BLOCKER |
| Memory backend in controlled check | CONTROLLED_DEPLOYMENT_BLOCKER |

## HMAC Secret Readiness

- Reference: `conversation-owner-hmac-secret` app binding → `CONVERSATION_OWNER_HMAC_SECRET`
- Secret scope: `transparence-owner-identity`, key: `conversation-owner-hmac-v1`
- Reference present: confirmed (app resource binding exists)
- No default value: confirmed (env var not set unless binding is active)
- Minimum-length validation: occurs at runtime in `request_owner_identity.py` on first use
- Value never printed: confirmed (readiness checker checks presence only, never reads value)
- Status: PRESENT_AND_SUFFICIENT when app binding is active
