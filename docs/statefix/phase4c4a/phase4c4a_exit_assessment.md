# Phase 4C4A — Exit Assessment

## Verdict

**PASS.** All contract surfaces inspected, all design decisions documented,
recommended reset approach confirmed as implementable with existing primitives.

---

## Selected Design

**Design A:** Mark old record RESET via `set_status` with optimistic
concurrency + generate new frontend conversation ID.

---

## Exact Reset Endpoint Recommendation

```
POST /api/conversations/{frontend_conversation_id}/reset
```

- HTTP Method: POST
- Identity: Server-derived from trusted header (`X-Forwarded-Access-Token`)
- Request body: empty `{}`
- Success: 200 `{"status": "reset", "conversation_id": "...", "message": "Conversation has been reset."}`
- Already RESET: 200 (idempotent, no version bump)
- Not found / ownership mismatch: 200 (idempotent — postcondition satisfied)
- Repository unavailable: 503
- Version conflict (reload=RESET): 200
- Version conflict (reload=ACTIVE): 409
- Durable disabled: 503 (cannot confirm durable deactivation)

---

## Phase 4C4B File Scope

### Backend (new files)

| File | Purpose |
|------|---------|
| `app/routes/conversation_reset.py` | Reset endpoint route, request/response models |
| `app/services/conversation_reset_coordinator.py` | Reset orchestration: load → set_status → in-memory cleanup |

### Backend (modified files)

| File | Change |
|------|--------|
| `app/main.py` | Register reset router (`router.include_router(...)`) |

### Frontend (modified files)

| File | Change |
|------|--------|
| `frontend/src/App.jsx` | `handleNewChat`: add async reset call before generating new ID |

### Tests (new files)

| File | Coverage |
|------|----------|
| `tests/test_conversation_reset_coordinator.py` | Unit tests for coordinator logic |
| `tests/test_conversation_reset_route.py` | Route tests: identity, ownership, responses |

---

## Detailed Backend Scope

### conversation_reset.py (route)
- Reset request model (empty body)
- Reset response model
- `POST /api/conversations/{frontend_conversation_id}/reset` handler
- Trusted identity extraction via `resolve_request_owner_identity`
- Error response mapping (404, 409, 503)

### conversation_reset_coordinator.py (service)
- Load record by `(owner_hash, frontend_conversation_id)`
- Check current status:
  - Already RESET → return success (idempotent)
  - ACTIVE/STALE/EXPIRED → proceed with `set_status(RESET, expected_version)`
- Handle version conflict:
  - Reload once
  - RESET on reload → return success
  - ACTIVE on reload → return conflict
  - Not found on reload → return not-found
- Call `GenieSessionStore.reset_session(frontend_conversation_id)` on success
- Handle durable-runtime-disabled: skip durable path, do in-memory only

---

## Detailed Frontend Scope

### App.jsx changes
- `handleNewChat` becomes async
- Sends `POST /api/conversations/{activeConvId}/reset` before ID generation
- On 200: generates new UUID, activates new conversation
- On 503: **fail closed** — show error, retain current chat, allow retry
- On 409: **fail closed** — show error, retain current chat, allow retry
- On network error: **fail closed** — show error, retain current chat, allow retry
- Loading state management during reset call

---

## Required Tests

| Test | Scope |
|------|-------|
| Route identity extraction | Trusted header → owner hash |
| Route ownership isolation | Different owner → 404 |
| Durable reset success | ACTIVE → RESET transition |
| Already-reset idempotency | RESET → 200 without version bump |
| Missing record | 200 idempotent response |
| Durable runtime disabled | 503 response (cannot confirm deactivation) |
| Version conflict (reload=RESET) | 200 idempotent |
| Version conflict (reload=ACTIVE) | 409 response |
| Repository unavailable | 503 response |
| In-memory cleanup called | `reset_session` invoked on success |
| In-memory cleanup failure | Logged; 200 still returned |
| Frontend New Chat sequence | Reset call → new ID → UI clear |
| Inactive recovery prevention | Message on RESET ID → rejected |
| Same-key reuse prevention | `get_or_create` on RESET key → returns RESET record |
| Owner isolation in coordinator | Owner-A cannot reset Owner-B’s record |
| Race: reset vs bind | Version conflict handled correctly |
| Race: dual reset | Second reset gets idempotent 200 |

---

## Key Decisions

| Question | Answer |
|----------|--------|
| Adapter production changes required? | **No.** `set_status` already exists and is sufficient. |
| Repository production changes required? | **No.** All needed primitives exist. |
| Schema migration required? | **No.** `status` column and all needed columns already exist. |
| New feature flag required? | **No.** Reset endpoint uses the existing durable-session runtime bundle via `ENABLE_DURABLE_GENIE_SESSION_ADAPTER`. When the durable adapter is disabled, reset returns 503 (cannot confirm durable deactivation). |
| Existing identity flag sufficient? | **Yes.** `ENABLE_TRUSTED_REQUEST_OWNER_IDENTITY` controls whether owner identity is available. When disabled, reset returns 503 (cannot derive trusted owner). |
| Existing durable-state flag sufficient? | **Yes.** `ENABLE_DURABLE_GENIE_SESSION_ADAPTER` + `CONVERSATION_REPOSITORY_BACKEND` + `ENABLE_LAKEBASE_CONVERSATION_REPOSITORY` control the durable path. |
| Frontend and backend must deploy together? | **Backend first or atomic.** Preferred: deploy backend reset endpoint first, validate availability and feature-flag behaviour, then deploy frontend integration. Alternative: deploy both atomically in one controlled release. **Prohibited:** frontend reset integration before the backend endpoint is available. Frontend and backend must be tested together before production activation. |

---

## Blockers

**None identified.** All required primitives exist:
- `set_status` with CAS semantics
- `resolve_request_owner_identity` for trusted identity
- `GenieSessionStore.reset_session` for in-memory cleanup
- `_newConvId()` for UUID generation
- Feature flags for runtime control

---

## Phase 4C4B Safety Assessment

**Phase 4C4B is SAFE to begin.**

Conditions met:
1. All contract surfaces verified and documented.
2. No ambiguity in design decision.
3. No schema migration required.
4. No new feature flags required.
5. No adapter or repository changes required.
6. Implementation surface is minimal (2 new backend files + 1 modified route registration + 1 frontend change).
7. All race conditions documented with handling policies.
8. Test scope defined.

---

## Confirmation

- Production code: **UNCHANGED**
- Tests: **UNCHANGED**
- Frontend: **UNCHANGED**
- No deployment performed
- No app restart performed
- No live Lakebase connection, credential, pool, or SQL executed
- Documentation only: 5 files in `docs/statefix/phase4c4a/`

---

*Phase 4C4A — inspection only.  No code modified.*
