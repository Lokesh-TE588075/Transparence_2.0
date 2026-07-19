# Phase 4C4A — Concurrency and Failure Policy

## Document Purpose

Defines the exact concurrency behaviour, race conditions, and failure
handling policy for the recommended reset contract (Design A).

---

## 1. Core Concurrency Mechanism

`set_status(RESET, expected_version=N)` uses compare-and-swap:

- **Precondition:** stored `version` must equal `N`.
- **Effect on success:** `version` becomes `N+1`, `status` becomes RESET.
- **Effect on conflict:** record unchanged, `ConversationVersionConflictError` raised.

This guarantees **exactly one writer** succeeds when multiple concurrent
reset or mutation requests target the same record.

---

## 2. Race Scenarios

### 2.1 Reset racing with RECOVERED send_message

**Scenario:** Container recovers from cold-start.  Durable lookup returns
an ACTIVE record.  Concurrently, user clicks New Chat triggering reset.

**Sequence:**
```
T1: send_message reads record (version=5, status=ACTIVE)
T2: reset reads record (version=5, status=ACTIVE)
T3: reset calls set_status(RESET, expected_version=5) → succeeds (version=6)
T4: send_message calls bind_genie_conversation(expected_version=5) → CONFLICT
```

**Policy:** send_message receives version conflict.  It should reload the
record, observe status=RESET, and abort the Genie call.  The pipeline
must not retry after observing RESET.

### 2.2 Reset racing with MISS get_or_create

**Scenario:** New chat with a never-before-seen ID arrives concurrently
with a reset of a different conversation.

**Analysis:** No conflict.  `get_or_create` operates on a different logical
key.  Independent records are isolated.

### 2.3 Reset racing with bind_genie_conversation

**Scenario:** Pipeline is binding a new Genie conversation while user
resets.

**Sequence:**
```
T1: pipeline reads record (version=5)
T2: reset set_status(RESET, expected_version=5) → succeeds (version=6)
T3: pipeline bind_genie_conversation(expected_version=5) → CONFLICT
```

**Policy:** Pipeline receives conflict, reloads, observes RESET, aborts.
Genie conversation was started externally but will never be resumed.
This is the **unavoidable external-state race** (see Section 5).

### 2.4 Reset racing with update_last_genie_message

Identical to 2.3.  The message update fails with version conflict.
Pipeline reloads, observes RESET, does not store the message ID.

### 2.5 Two simultaneous reset requests

**Sequence:**
```
T1: reset-A reads record (version=5)
T2: reset-B reads record (version=5)
T3: reset-A set_status(RESET, expected_version=5) → succeeds (version=6)
T4: reset-B set_status(RESET, expected_version=5) → CONFLICT
```

**Policy:** reset-B receives conflict.  It reloads the record.  If status
is already RESET, it returns 200 success (idempotent).  No retry loop.

### 2.6 Reset from one browser tab while another continues

**Analysis:** Design A guarantees safety because each tab uses independent
frontend conversation IDs.  Tab-1 resets its own conversation ID; Tab-2
operates on a different ID.  No shared-state conflict.

If tabs could somehow share the same ID (not possible in current
architecture), the CAS protection would still prevent corruption.

### 2.7 New message sent on old ID after reset

**Scenario:** User clicks New Chat, then a background process or delayed
request sends a message on the old conversation ID.

**Analysis with durable runtime:**
1. Pipeline calls `get_or_create(old_key)`.
2. `create_conversation` is idempotent — returns existing RESET record.
3. Pipeline receives record with status=RESET.
4. Pipeline checks status, rejects as inactive, does NOT start Genie call.
5. Returns error to caller.

**This is the REQUIRED behaviour after Phase 4C4B.**  The RESET record
acts as a block that prevents execution.

### 2.7.1 Current unsafe behaviour (pre-Phase 4C4B)

In the current implementation, non-ACTIVE records are classified as MISS
(line 1477–1478 of `genie_pipeline.py`).  This means:

1. `_run_inner()` executes — a new Genie conversation is started.
2. `_maybe_persist_durable_writeback()` calls `get_or_create`.
3. `get_or_create` returns the existing RESET record unchanged (idempotent).
4. Writeback may attempt `bind_genie_conversation` on the inactive record.

Phase 4C4B fixes this by introducing `_DurableLookupOutcome.INACTIVE` which
returns a static error response **before** `_run_inner()` is called.

### 2.8 Version conflict reload shows RESET

**Policy:** Treat as idempotent success.  The intended outcome (record is
RESET) has been achieved by another writer.  Return 200.

### 2.9 Version conflict reload shows ACTIVE with newer version

**Scenario:** A mutation (e.g., `touch` or `update_last_genie_message`)
succeeded between the reset read and the reset CAS.

**Policy:** Fail closed.  Return 409 Conflict to the frontend.  Do NOT
retry.  The conversation is actively being used.

Rationale: If the user just sent a message (which bumped the version),
resetting would discard in-progress work.  The frontend should inform the
user that the conversation is active.

### 2.10 Repository unavailable

**Policy: fail closed.**
- Return 503 to the frontend.
- Frontend retains the current conversation and ID.
- Frontend shows a sanitized static error.
- Frontend does NOT generate a new ID or present reset as successful.
- User may retry.
- Old in-memory session is NOT cleared (durable authority was not confirmed).
- Old durable record remains ACTIVE until repository recovers and reset succeeds.

### 2.11 Reset succeeds but in-memory cleanup throws

**Policy:** Log the error.  Return 200 to the frontend (durable state is
authoritative).  The in-memory session will expire naturally via TTL or
be replaced on next access via `_get_or_create_session`.

### 2.12 In-memory cleanup succeeds but durable reset fails

**Analysis:** This cannot happen in the recommended sequence because the
endpoint performs durable reset FIRST, then in-memory cleanup.  If durable
fails, in-memory is not attempted, and 503 is returned.

---

## 3. Concurrency Contract Rules

1. Use `set_status` with `expected_version` for all durable reset operations.
2. Perform at most ONE reload after a version conflict.
3. After reload:
   - If status is RESET: return 200 (idempotent success).
   - If status is STALE: return 200 (idempotent success — already non-active).
   - If status is EXPIRED: return 200 (idempotent success — already non-active).
   - If record is missing: return 200 (idempotent success — postcondition met).
   - If status is ACTIVE with newer version: return 409 (fail closed).
9. Chat requests using an old (inactive) conversation ID must be blocked
   before any Genie execution via the `INACTIVE` lookup outcome.
10. The `INACTIVE` outcome returns a static response; no durable mutation
    occurs; no external Genie request is made.
4. No repeated CAS loop (maximum one reload + one retry).
5. No hard delete as conflict recovery.
6. Inactive records (RESET, STALE, EXPIRED) must never resume Genie communication.
7. No raw identifiers (conversation_id, owner_hash, Genie IDs) in user-facing
   error messages or HTTP response bodies.
8. Sanitized static error messages only.

---

## 4. Failure Handling Summary

| Failure Mode | Backend Response | Frontend Action |
|--------------|-----------------|------------------|
| Durable reset succeeds | 200 | Generate new UUID, activate new conversation |
| Already RESET | 200 | Same as success |
| Record not found for owner | 200 | Same as success (postcondition satisfied) |
| Version conflict (reload=RESET) | 200 | Same as success |
| Version conflict (reload=ACTIVE) | 409 | Show transient error; retain current chat; allow retry |
| Repository unavailable | 503 | **Fail closed**: show error; retain current chat; allow retry |
| Durable runtime disabled | 503 | **Fail closed**: reset cannot be confirmed without durable runtime |
| Trusted identity unavailable | 503 | **Fail closed**: show error; retain current chat; allow retry |
| In-memory cleanup fails after durable success | 200 | (transparent to frontend; session expires via TTL) |

---

## 5. Unavoidable External-State Race

A Genie API request may have already been submitted to the Databricks
Genie Space before the reset commits.  The reset contract:

- **CAN** prevent the application from storing the Genie response.
- **CAN** prevent future messages on the same Genie conversation.
- **CANNOT** cancel an already-submitted Genie request (Genie API has no
  cancel endpoint).

The orphaned Genie conversation will expire naturally per Genie Space TTL
policies.  No action required from the application.

---

## 6. Multi-Tab Safety Guarantee

Design A provides full multi-tab safety:

1. Each tab generates independent `crypto.randomUUID()` values.
2. No localStorage/sessionStorage coordination exists.
3. No BroadcastChannel or SharedWorker communication.
4. Reset in Tab-1 targets Tab-1’s conversation ID only.
5. Tab-2’s conversation ID is unaffected.
6. Even if both tabs happen to reset simultaneously, they target different
   records (different frontend_conversation_id values).

---

## 7. In-Flight Response Isolation

After New Chat:

1. Old request closure captures old `activeConvId` (React closure semantics).
2. Old response handler writes to old conversation object (line 99–106 of App.jsx).
3. New conversation has a different `id`; no cross-contamination.
4. If backend reset clears the old in-memory session, but the old response
   is already being processed, the response is based on data already
   retrieved — it does not re-read session state.

### 7.1 Exact React Stale-Response Mechanism (Phase 4C4B)

The current `isLoading` state (line 25 of App.jsx) is global — not
per-conversation.  Without a guard, a late `setIsLoading(false)` from an
old request would prematurely clear the spinner on the new conversation.

**Selected mechanism (minimal, correct, does not require per-conversation
state refactoring):**

```jsx
// 1. Add a ref tracking the current active conversation ID:
const activeConvIdRef = useRef(activeConvId);

// 2. Synchronize on every activation change:
useEffect(() => {
    activeConvIdRef.current = activeConvId;
}, [activeConvId]);

// 3. At the start of handleSendMessage, capture the request-time ID:
const requestConversationId = activeConvId;

// 4. All setConversations calls already correctly filter on the captured ID
//    (line 100: c.id !== activeConvId — rename to requestConversationId).

// 5. In finally/catch, guard global state updates:
if (activeConvIdRef.current === requestConversationId) {
    setIsLoading(false);
}
```

**Why this works:**
- `useRef` provides a stable mutable container that always holds the latest
  `activeConvId`, even when read inside an old closure.
- The captured `requestConversationId` is frozen at send time.
- If reset completes and activates a new ID while the old request is still
  pending, `activeConvIdRef.current !== requestConversationId` will be true,
  so the old request’s `finally` block does NOT clear loading state.
- A separate `isResetting` state (see concurrency section) controls the
  New Chat button independently of `isLoading`.

**AbortController:** May be added as best-effort cancellation of the fetch,
but correctness must NOT depend on cancellation because Genie work may
already be running server-side.  The `activeConvIdRef` guard alone is
sufficient for UI correctness.

---

## 8. Two Concurrency Layers

### Layer A: Reset endpoint CAS

**Case 1 — Record found (initial load returns a record):**

1. Already RESET → idempotent 200; do not call `set_status`.
2. STALE → idempotent 200; do not change STALE to RESET.
3. EXPIRED → idempotent 200; do not change EXPIRED to RESET.
4. ACTIVE → `set_status(RESET, expected_version=current.version)`.
5. One reload maximum after version conflict (ACTIVE path only).
6. Reload shows RESET/STALE/EXPIRED → idempotent 200.
7. Reload shows ACTIVE → 409 fail closed.
8. Reload unavailable → 503 fail closed.
9. No delete; no CAS loop.

**Case 2 — No record found (initial load returns None):**

The coordinator MUST NOT return 200 without a durable tombstone because of
the reset-versus-MISS race (see Section 8.1).

1. Call `adapter.get_or_create(key)` to create or load the logical key.
2. Returned record is RESET/STALE/EXPIRED → postcondition met → 200.
3. Returned record is ACTIVE:
   - Call `set_status(RESET, expected_version=record.version)`.
   - Success → 200.
   - Version conflict → one reload (same policy as Case 1 step 5–8).
4. `get_or_create` unavailable → 503 fail closed.
5. No repeated creation attempt within the same request.

The RESET row created here is a **durable reset tombstone**. It occupies the
unique `(owner_user_id_hash, frontend_conversation_id)` logical key and
prevents any later MISS-writeback `get_or_create` from creating a new ACTIVE
record for the old frontend ID.

**Postcondition for all 200 outcomes:** call
`session_store.remove_session(app_conversation_id)`.

**Rationale for STALE/EXPIRED idempotency:**
- RESET, STALE and EXPIRED are already non-active.
- The desired reset postcondition (old conversation cannot continue) is
  already satisfied.
- Preserving STALE and EXPIRED retains the lifecycle reason (timeout vs
  explicit user action).
- Unnecessary status changes would increment versions and erase the
  semantic distinction between why a conversation became inactive.

### Layer B: Chat request using old ID (post-Phase 4C4B)

Two protection boundaries are required:

**Boundary 1 — Initial durable lookup (before `_run_inner`):**

1. Durable lookup finds record with status in {RESET, STALE, EXPIRED}.
2. Pipeline returns `_DurableLookupOutcome.INACTIVE`.
3. `_run_inner()` is NOT called.
4. No external Genie request is made.
5. Static no-fallback response returned:
   `"This conversation is no longer active. Start a new chat."`
6. No durable mutation occurs.
7. No attempt to reuse the same key.

**Boundary 2 — Post-Genie MISS writeback (before `bind_genie_conversation`):**

After `_run_inner()` completes on a legitimate MISS, the pipeline calls
`adapter.get_or_create(key)` to persist the new durable record. Before
proceeding to bind:

1. Validate `record.status == ConversationStatus.ACTIVE`.
2. If RESET/STALE/EXPIRED:
   - Do NOT call `bind_genie_conversation`.
   - Do NOT call `update_last_genie_message`.
   - Do NOT call `touch`.
   - Do NOT reactivate the record.
   - Do NOT delete the record.
   - Clear the process-local in-memory session mapping.
   - Return a static sanitized no-fallback error.
3. If ACTIVE: proceed normally with bind.

**Why both boundaries are required:**

Boundary 1 catches the common case (old ID submitted after reset is already
committed). Boundary 2 closes the TOCTOU race where:
- Initial lookup returns MISS (no record exists yet).
- `_run_inner()` starts a Genie conversation.
- Concurrently, reset creates a RESET tombstone for the same key.
- Writeback’s `get_or_create` returns the tombstone unchanged.
- Without the status check, `bind_genie_conversation` would attempt to
  mutate an inactive record.

Process-local `remove_session()` alone cannot prevent this race because:
- The original request carries its own request-local state.
- Multiple app workers or restarted containers may be involved.
- Process memory is not authoritative.
- Only durable occupation of the logical key blocks later writeback.

### 8.1 Reset-versus-MISS Race

**Proof of race from current code:**

```
T1: Chat request → _durable_session_lookup → MISS (no record exists)
T2: Chat request → _run_inner() → Genie conversation started externally
T3: User clicks New Chat → reset coordinator loads key → None
T4: (Without tombstone) Reset returns 200 "postcondition satisfied"
T5: Chat request → _maybe_persist_durable_writeback → get_or_create
T6: get_or_create → create_conversation → new ACTIVE record created
T7: bind_genie_conversation succeeds → old ID is now durable and recoverable
```

After T7, the "reset" old ID has a fully bound ACTIVE durable record. On
the next container restart, durable recovery will restore the Genie
conversation for the old ID — violating the reset postcondition.

**Resolution:** The tombstone contract (Layer A Case 2) ensures that at T3,
the coordinator creates a RESET tombstone instead of returning success for a
missing record. At T5, `get_or_create` returns the tombstone unchanged. At
the status check (Boundary 2), the pipeline observes RESET and aborts the
bind. The old ID can never become ACTIVE.

### Unavoidable in-flight race

A request that already passed durable lookup (as ACTIVE or MISS) and
submitted work to Genie **before** the reset endpoint commits cannot be
cancelled externally.  However:

- Reset wins for all future application recovery.
- The late response must NOT repopulate the new frontend chat (guaranteed
  by React closure semantics — response handler captures old `activeConvId`).
- Any subsequent request using the old ID is blocked by the `INACTIVE`
  outcome.

---

*Phase 4C4A — inspection only.  No code modified.*
