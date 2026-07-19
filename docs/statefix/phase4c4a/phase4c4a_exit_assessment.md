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
| `app/main.py` | Register reset router |
| `app/services/genie_pipeline.py` | Add `INACTIVE` to `_DurableLookupOutcome`; block inactive requests before `_run_inner`; add post-`get_or_create` status validation before `bind_genie_conversation`; return static no-fallback response |
| `app/services/genie_session_store.py` | Add `remove_session()` method (lock-protected physical dict removal) |

### Frontend (modified files)

| File | Change |
|------|--------|
| `frontend/src/App.jsx` | `handleNewChat` async; `activeConvIdRef` stale-response guard; `isResetting` state; reset API call with fail-closed error handling |
| `frontend/src/components/Sidebar.jsx` | New Chat button disabled when `isResetting=true`; inactive conversation visual indicator (e.g. muted style) |
| `frontend/src/components/ChatWindow.jsx` | Disable prompt input when `conversation.isInactive=true`; show static inactive notice |

### Tests (new files)

| File | Coverage |
|------|----------|
| `tests/test_conversation_reset_coordinator.py` | Unit tests for coordinator logic (including tombstone creation for missing records) |
| `tests/test_conversation_reset_route.py` | Route tests: identity, ownership, responses |
| `tests/test_genie_pipeline_inactive_durable_state.py` | INACTIVE outcome blocks execution; static response; no Genie call; no durable mutation |
| `tests/test_genie_pipeline_durable_writeback.py` | Post-get_or_create status check: RESET/STALE/EXPIRED returned from get_or_create blocks bind; tombstone race simulation |

### Tests (modified files)

| File | Change |
|------|--------|
| `tests/test_genie_session_store.py` | Tests for `remove_session` (idempotent, thread-safe, full state cleared) |

---

## Detailed Backend Scope

### conversation_reset.py (route)
- Reset request model (empty body)
- Reset response model
- `POST /api/conversations/{frontend_conversation_id}/reset` handler
- Trusted identity extraction via `resolve_request_owner_identity`
- Error response mapping (404, 409, 503)

### conversation_reset_coordinator.py (service)
- Load record by `(owner_hash, frontend_conversation_id)` via adapter.load(key)
- Check current status:
  - Already RESET → return 200 idempotent success; do not call set_status
  - STALE → return 200 idempotent success; do not change STALE to RESET
  - EXPIRED → return 200 idempotent success; do not change EXPIRED to RESET
  - ACTIVE → call `set_status(RESET, expected_version=record.version)`
  - **Missing → create RESET tombstone** (see below)
- Missing-record tombstone path:
  - Call `adapter.get_or_create(key)` to occupy the logical key
  - Returned RESET/STALE/EXPIRED → postcondition met → 200
  - Returned ACTIVE → `set_status(RESET, expected_version=record.version)`
  - get_or_create unavailable → 503 fail closed
  - MUST NOT return 200 without confirmed durable RESET tombstone
- Handle version conflict (ACTIVE path only):
  - Reload once via `adapter.load(key)`
  - Reload shows RESET/STALE/EXPIRED → return 200 idempotent
  - Reload shows ACTIVE → return 409 fail closed
  - Reload unavailable → return 503 fail closed
  - No repeated creation within same request
- After any 200 outcome: call `GenieSessionStore.remove_session(app_conversation_id)`
  to physically remove the process-local session
- Handle durable-runtime-disabled: return 503 (cannot confirm durable deactivation)

---

## Detailed Frontend Scope

### App.jsx changes
- `handleNewChat` becomes async
- Sends `POST /api/conversations/{activeConvId}/reset` before ID generation
- On 200: generates new UUID, activates new conversation
- On 503: **fail closed** — show error, retain current chat, allow retry
- On 409: **fail closed** — show error, retain current chat, allow retry
- On network error: **fail closed** — show error, retain current chat, allow retry
- New Chat button disabled while reset is pending (prevents double-click)
- Loading state management during reset call

### In-flight response safety — exact React mechanism
- Add `const activeConvIdRef = useRef(activeConvId)` with `useEffect`
  synchronisation as defensive consistency.
- Add `activateConversation(id)` helper that updates ref synchronously
  THEN sets state: `activeConvIdRef.current = id; setActiveConvId(id)`.
- Use `activateConversation` for all transitions (reset success, new chat,
  sidebar selection).
- At the start of `handleSendMessage`, capture from ref:
  `const requestConversationId = activeConvIdRef.current`.
- All `setConversations` calls use `requestConversationId`.
- In `finally`/`catch`, guard global state: only call `setIsLoading(false)`
  when `activeConvIdRef.current === requestConversationId`.
- This prevents the old request’s resolution from clearing the spinner or
  error state on the new conversation (synchronous ref update ensures no
  window between state commit and guard effectiveness).

No AbortController required (Genie may already be processing server-side).
The `activeConvIdRef` guard is sufficient for UI correctness.

### Old conversation read-only UI
After backend reset succeeds:
- Mark the old local conversation as `isInactive: true` in React state.
- Retain its existing messages in the sidebar for read-only viewing.
- Generate and activate a new conversation ID.
- When the user selects the inactive conversation:
  - Show its prior messages.
  - Disable the prompt input (ChatWindow checks `conversation.isInactive`).
  - Display a static notice:
    “This conversation is no longer active. Start a new chat.”
- Never submit `/api/chat` using an inactive local conversation.
- The backend INACTIVE outcome remains the authoritative safety control.

### Reset-pending UI contract
- Add a separate `isResetting` state (distinct from `isLoading`).
- New Chat button disabled while `isResetting=true`.
- Retain the existing conversation UI during the reset request.
- Do NOT clear messages or activate a new ID before backend returns 200.
- On 409 / 503 / network failure:
  - Retain the current conversation.
  - Show a sanitized error toast or inline message.
  - Set `isResetting=false`.
  - Permit retry.
- On 200:
  - Mark old local conversation `isInactive: true`.
  - Generate new UUID via `_newConvId()`.
  - Activate the new conversation.
  - Clear `isResetting` and any reset error state.

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
| GenieSessionStore production change required? | **Yes.** Add `remove_session()` for complete in-memory purge. |
| GeniePipeline production change required? | **Yes.** Add `INACTIVE` outcome and pre-execution block. |
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
6. Implementation surface is well-defined (2 new backend files + 3 modified backend files + 3 frontend files + 4 test files).
7. All race conditions documented with handling policies.
8. Test scope defined.

---

## Browser-Refresh Scope

**Phase 4C4B guarantees:**
- Databricks App container restart recovery (via durable state).
- Scale-to-zero recovery.
- Durable reset (old conversation permanently blocked).
- Stale old-ID blocking (INACTIVE outcome).
- Complete in-memory session removal.
- Frontend late-response isolation (activeConvIdRef guard).

**Phase 4C4B does NOT by itself guarantee:**
- Restoring the active conversation after a hard browser refresh.
- Restoring sidebar history after closing and reopening the browser.
- Cross-device conversation discovery.

These require a later frontend persistence/history contract (Phase 4D):
- `localStorage` persistence of active frontend conversation ID; or
- A backend owner-scoped conversation-list/history API.

This is recorded as a Phase 4D readiness item.

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
