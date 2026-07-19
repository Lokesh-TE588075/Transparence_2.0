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

**This is correct behaviour.** The RESET record acts as a tombstone that
prevents resumption.

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

**Policy:**
- Return 503 to the frontend.
- Frontend proceeds with new ID generation (graceful degradation).
- Old in-memory session is still cleared (best-effort).
- Old durable record remains ACTIVE until repository recovers.

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
   - If status is ACTIVE with newer version: return 409 (fail closed).
   - If status is STALE or EXPIRED: proceed with reset using the new version.
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
| Record not found | 404 | Generate new UUID (old record never existed durably) |
| Version conflict (reload=RESET) | 200 | Same as success |
| Version conflict (reload=ACTIVE) | 409 | Show transient error; do not clear UI |
| Repository unavailable | 503 | Generate new UUID anyway (graceful degradation); log warning |
| Durable runtime disabled | 200 | Same as success (in-memory-only reset) |
| In-memory cleanup fails | 200 | (transparent to frontend) |

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

---

*Phase 4C4A — inspection only.  No code modified.*
