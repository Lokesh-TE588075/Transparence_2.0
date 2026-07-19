# Phase 4C4A — Reset Semantics Decision

## Document Purpose

Design comparison of four candidate reset strategies, with selection of the
recommended approach based on verified contract surfaces.

---

## 1. Designs Evaluated

### Design A: Mark old record RESET + generate new frontend conversation ID

Backend sets old record to `ConversationStatus.RESET` via `set_status` with
optimistic concurrency.  Frontend generates a new UUID for the replacement
conversation.

### Design B: Mark old record RESET + reuse same frontend conversation ID

Backend sets old record to RESET, then frontend sends the next message with
the same conversation ID.

### Design C: Physically delete old record + reuse same frontend conversation ID

Backend calls `delete_conversation` to remove the record, then frontend
continues with the same ID.

### Design D: Clear only browser/in-memory state (current behaviour)

No backend notification.  React state clears; in-memory session expires
naturally via TTL.

---

## 2. Comparison Matrix

| Criterion | Design A | Design B | Design C | Design D |
|-----------|----------|----------|----------|----------|
| **Old-conversation recovery risk** | None — RESET record rejected on lookup | CRITICAL — `create_conversation` returns RESET record unchanged; mutations can run on it | None after delete; but window between delete and recreate is unprotected | Old Genie session can resume if container survives |
| **Uniqueness behaviour** | New UUID; unique constraint satisfied | Same key; unique constraint blocks fresh ACTIVE insert | Same key after delete; recreation permitted | Each page load gets new UUID anyway |
| **Restart behaviour** | Fresh ACTIVE record created on first message | RESET record returned by `get_or_create` — pipeline must explicitly reactivate | Fresh record created | Fresh in-memory session; durable record stays ACTIVE |
| **Auditability** | RESET record preserved with timestamps and version history | Same as A for the record itself | Completely lost — physical delete destroys all history | No audit trail for old conversations |
| **Rollback** | `set_status(ACTIVE)` technically possible (no state machine enforcement) | Same as A | Impossible — data is gone | N/A |
| **Concurrency safety** | CAS-protected via `expected_version` | Same as A for the set_status call; but same-key reuse creates ambiguity | **UNSAFE** — delete has no version guard; races with concurrent updates | N/A (no backend call) |
| **Multi-tab safety** | Each tab has independent UUID; no conflict | Both tabs share the same ID — second tab may accidentally operate on RESET record | Both tabs share ID — race between delete and re-create | Each tab independent |
| **In-flight response behaviour** | Old response updates old conversation (correct); new ID isolates new chat | Old response could match the reused ID if race timing allows | Old response targets deleted ID — backend may see stale data | Old response writes to old conversation object in React state (correct) |
| **Operational complexity** | Low — single `set_status` + new UUID | High — requires explicit reactivation path not yet designed | Medium — simple delete call but requires handling race conditions | Zero |
| **Data-retention consequences** | Old record retained for analytics/audit; status column enables filtering | Same as A | Data permanently lost | Old durable record stays ACTIVE, polluting active-count queries |

---

## 3. Design B Rejection

**Fatal flaw:** `create_conversation` is idempotent on the logical key
`(owner_hash, frontend_id)`.  Calling `get_or_create` with the same frontend
ID after a RESET returns the RESET record unchanged.  The unique constraint
prevents inserting a fresh ACTIVE row.  The only path forward would be:

1. Load the RESET record.
2. Call `set_status(ACTIVE, expected_version=current)` to reactivate it.
3. Clear its Genie IDs via `compare_and_update`.

This is complex, error-prone, and creates an accidental-reactivation risk
if any code path calls `get_or_create` without checking status first.

**Rejected.**

---

## 4. Design C Rejection

**Fatal flaws:**

1. `delete_conversation` has no `expected_version` parameter.  It is an
   unconditional physical delete.  A concurrent `bind_genie_conversation`
   or `update_last_genie_message` that succeeds between the delete lookup
   and the physical removal has its mutation silently destroyed.

2. Physical deletion permanently destroys audit history.  There is no way
   to retrospectively analyse conversation patterns or debug issues.

3. Recreation with the same key starts at version 1.  Any in-flight
   operation holding a stale version reference would succeed unexpectedly
   on the fresh record (version 1 matches if the old record had just been
   created).

**Rejected.**

---

## 5. Design D Rejection

**Fatal flaws:**

1. Durable records remain ACTIVE indefinitely.  This makes staleness
   detection impossible and pollutes metrics.

2. On container cold-start, the durable record suggests the conversation
   is still active.  Recovery logic may attempt to resume Genie
   communication on a conversation the user has mentally abandoned.

3. No backend awareness means no cleanup hook for Genie sessions.

**Rejected as production design.**  (Remains as fallback when durable
runtime is disabled.)

---

## 6. Design A Selection

**Selected: Design A — Mark old record RESET + generate new frontend conversation ID.**

Rationale:

1. CAS-protected status transition — exactly one writer succeeds.
2. New UUID eliminates all same-key reuse ambiguity.
3. Old record preserved for audit with RESET status and timestamp.
4. In-flight isolation guaranteed by distinct conversation IDs.
5. Multi-tab safety: each tab generates independent IDs.
6. No complex reactivation path required.
7. Compatible with future analytics on conversation lifecycle.
8. Minimal implementation surface (single new endpoint + frontend UUID generation already exists).

---

## 7. Recommended Reset API Contract

### 7.1 Endpoint

```
POST /api/conversations/{frontend_conversation_id}/reset
```

### 7.2 Identity

Trusted owner identity derived from `X-Forwarded-Access-Token` header
via `resolve_request_owner_identity` (Phase 4B2 runtime).  Owner identity
MUST NOT appear in the request body.

### 7.3 Request Body

```json
{}
```

No request fields required.  The `frontend_conversation_id` is in the URL
path.  The owner is derived server-side from the trusted header.

### 7.4 Response (200 OK)

```json
{
  "status": "reset",
  "conversation_id": "<frontend_conversation_id>",
  "message": "Conversation has been reset."
}
```

### 7.5 Error Responses

| Condition | Status Code | Body |
|-----------|-------------|------|
| Already RESET | 200 | Same as success (idempotent) |
| Record not found for trusted owner | 200 | Same as success (desired postcondition already satisfied; idempotent) |
| Same frontend ID belongs to another owner | 200 | Same as owner-scoped absence (no cross-owner leakage) |
| Missing or invalid trusted identity | 401 | `{"status": "unauthorized", "message": "Authentication required."}` |
| Trusted identity runtime unavailable | 503 | `{"status": "unavailable", "message": "Service temporarily unavailable."}` |
| Durable runtime disabled or unavailable | 503 | `{"status": "unavailable", "message": "Service temporarily unavailable."}` |
| Repository unavailable | 503 | `{"status": "unavailable", "message": "Service temporarily unavailable."}` |
| Version conflict (reload=RESET) | 200 | Same as success (idempotent) |
| Version conflict (reload=ACTIVE) | 409 | `{"status": "conflict", "message": "Conversation state has changed. Please retry."}` |
| Invalid frontend conversation ID | 400 | `{"status": "invalid_request", "message": "Invalid conversation identifier."}` |

**Why missing-record returns 200:** The desired postcondition ("this frontend
conversation ID has no active durable state") is already satisfied.  Reset is
idempotent.  Record existence and cross-owner information are not exposed.

### 7.6 Already-Reset Idempotency

When the record is already RESET:
1. Load the record.
2. Check status == RESET.
3. Return 200 success without modifying the record (no version bump).

This avoids version inflation from repeated New Chat clicks.

### 7.7 In-Memory Cleanup

After any successful reset outcome (200):
- Call `GenieSessionStore.remove_session(app_conversation_id)` — physically
  removes the entire session dict entry including all business context.

`remove_session` is selected over `reset_session` because:
- `remove_session` physically removes the session object (all fields cleared,
  memory freed immediately, no TTL wait).
- `reset_session` only clears Genie IDs and sets `is_active=False`; business
  context fields remain in the dict entry until TTL expiry.
- New Chat represents complete conversation termination; physical removal is
  the correct semantic.

`remove_session` is called regardless of which durable status triggered the
idempotent 200 (RESET, STALE, EXPIRED, or missing record).

### 7.7.1 Reset Coordinator Status Decision Table

| Current durable status | Action | set_status called? | Response |
|------------------------|--------|--------------------|----------|
| ACTIVE | `set_status(RESET, expected_version)` | **Yes** | 200 |
| RESET | None (already non-active) | No | 200 idempotent |
| STALE | None (already non-active) | No | 200 idempotent |
| EXPIRED | None (already non-active) | No | 200 idempotent |
| Missing (owner-scoped) | **Create RESET tombstone** (see 7.7.3) | **Yes** (get_or_create + set_status) | 200 |

**Rationale for not mutating STALE/EXPIRED to RESET:**
- STALE and EXPIRED are already non-active; the reset postcondition is met.
- Preserving the original status retains the lifecycle reason (timeout-based
  expiry vs explicit user action vs inactivity threshold).
- Unnecessary mutations increment versions and erase semantic distinction.
- The chat pipeline INACTIVE outcome blocks all three statuses identically.

### 7.7.3 Missing-Record Tombstone Contract

When `adapter.load(key)` returns None during reset:

1. Call `adapter.get_or_create(key)`.
2. Inspect the returned record:
   - RESET/STALE/EXPIRED → postcondition already met → return 200.
   - ACTIVE → call `set_status(RESET, expected_version=record.version)`.
3. Confirm status is RESET.
4. Call `session_store.remove_session(app_conversation_id)`.
5. Return 200.

The resulting RESET row is a **durable reset tombstone**. It occupies the
unique `(owner_user_id_hash, frontend_conversation_id)` logical key and
prevents any later MISS-writeback `get_or_create` from producing a fresh
ACTIVE record for the old frontend ID.

**The endpoint MUST NOT return 200 for a missing record until the RESET
tombstone has been confirmed durable.**

**Why this is necessary (reset-versus-MISS race):**
A chat request may have already passed durable lookup (MISS) and started
a Genie conversation. Its eventual `get_or_create` writeback is idempotent
on the logical key. If the tombstone already occupies the key, writeback
returns the RESET record and the pipeline’s post-`get_or_create` status
check (Boundary 2) blocks the bind. Without the tombstone, writeback would
create a new ACTIVE record, making the old ID recoverable.

### 7.7.2 Version-Conflict Reload Policy (ACTIVE path only)

When `set_status(RESET, expected_version)` raises version conflict:

1. Call `adapter.load(key)` at most once.
2. Reload shows RESET → 200 idempotent success.
3. Reload shows STALE → 200 idempotent success.
4. Reload shows EXPIRED → 200 idempotent success.
5. Reload shows missing → 200 idempotent success.
6. Reload shows ACTIVE → 409 fail closed.
7. Reload unavailable → 503 fail closed.
8. No repeated CAS. No delete.

### 7.8 Frontend Sequencing

Recommended safety sequence:

```
1. User clicks "New Chat"
2. Frontend sends POST /api/conversations/{old_id}/reset
3. Backend confirms durable RESET (200) or handles error
4. Backend clears old in-memory session state
5. Frontend generates new UUID via _newConvId()
6. Frontend activates new conversation in React state
7. Frontend clears displayed messages (new conversation has empty messages)
```

The frontend MUST NOT clear UI state until the backend confirms reset (step 3).

**Failure policy: fail closed.**

When backend returns 503, 409, or any other persistence failure:
- Show a sanitized static error to the user.
- Retain the current conversation and ID.
- Do not present reset as successful.
- Do not automatically generate or activate a replacement ID.
- Allow the user to retry.

"Start another chat without deactivating the old one" would be a separate,
explicitly labelled product action and is not part of the reset contract.

### 7.9 Frontend Does Not Receive New ID from Backend

The backend does NOT generate or return the new conversation ID.  The
frontend is responsible for calling `_newConvId()` locally (as it does today).
This preserves the existing architecture where conversation IDs are
frontend-generated UUIDs.

---

### 7.10 Frontend Stale-Response Mechanism

**Selected implementation (exact React pattern for Phase 4C4B):**

```jsx
// In App() component body:
const activeConvIdRef = useRef(activeConvId);

// Defensive consistency (runs after React commit phase):
useEffect(() => {
    activeConvIdRef.current = activeConvId;
}, [activeConvId]);

// Synchronous activation helper (used for all conversation transitions):
const activateConversation = (conversationId) => {
    activeConvIdRef.current = conversationId;  // synchronous, immediate
    setActiveConvId(conversationId);           // async React state
};

// In handleSendMessage — capture from the ref (always current):
const requestConversationId = activeConvIdRef.current;

// All setConversations calls target requestConversationId.
// In finally/catch — guard global state:
if (activeConvIdRef.current === requestConversationId) {
    setIsLoading(false);
}
```

**Why synchronous ref update is required:**

`useEffect` alone runs after React commits the state update. Between
`setActiveConvId(newId)` and the effect firing, a concurrent `finally`
block could read the stale ref value. By updating the ref synchronously
in `activateConversation`, the guard is effective immediately.

**`activateConversation` must be used for all transitions:**
- Reset success (new ID activation).
- `handleNewChat` (new conversation activation).
- Sidebar conversation selection (`onSelect`).
- Any future active-conversation transition.

This prevents an old request’s resolution from clearing the loading/error
state on a newly activated conversation.

### 7.11 Old Conversation Read-Only UI

After backend reset succeeds:
- Mark old local conversation `isInactive: true` in React state.
- Retain messages in sidebar for read-only viewing.
- When user selects the inactive conversation:
  - Show prior messages (read-only).
  - Disable the prompt input (`ChatWindow` checks `conversation.isInactive`).
  - Display: “This conversation is no longer active. Start a new chat.”
- Never submit `/api/chat` using an inactive local conversation.
- Backend INACTIVE outcome remains the authoritative safety control.

### 7.12 Reset-Pending UI State

- Separate `isResetting` state (not conflated with `isLoading`).
- New Chat button disabled while `isResetting=true`.
- Existing conversation UI retained during the reset request.
- On 409/503/network failure: retain current conversation, show error,
  set `isResetting=false`, permit retry.
- On 200: mark old conversation inactive, generate new UUID, activate new
  conversation, clear reset error.

### 7.13 Browser-Refresh Scope

**Phase 4C4B guarantees:**
- Container restart recovery (durable state).
- Scale-to-zero recovery.
- Durable reset blocking.
- Stale old-ID rejection (INACTIVE outcome).
- Complete process-local session removal.
- Frontend late-response isolation.

**Phase 4C4B does NOT guarantee:**
- Active conversation restoration after hard browser refresh.
- Sidebar history restoration after closing/reopening the browser.
- Cross-device conversation discovery.

These require a later Phase 4D frontend persistence/history contract
(localStorage or backend owner-scoped history API).

---

## 8. Session-Expiry and Logout Recommendations

### 8.1 Session Expiry

The existing 24-hour TTL in `GenieSessionStore` handles natural expiry of
in-memory sessions.  Durable expiry is a separate retention/lifecycle policy
that may later use `EXPIRED` status based on approved TTL and retention
requirements.  No automatic durable expiry behaviour is introduced by Phase
4C4B.  The current New Chat implementation uses only `RESET`.

### 8.2 Logout

Ordinary logout:
- Clears authentication state.
- Clears browser-local conversation state as required by the application.
- Does **not** change durable conversation status by default.
- Preserves conversations for future recovery by the same authenticated owner.

Bulk deactivation requires a separate explicit product action such as:
- "End all conversations" user action;
- Account offboarding;
- Administrator retention enforcement.

Logout must not be conflated with conversation reset, expiry, or deletion.

---

## 9. In-Memory Reset Scope Comparison

| Method | Clears Genie IDs | Clears business context | Sets is_active=False | Resets TTL |
|--------|-------------------|------------------------|---------------------|------------|
| `reset_session` | Yes | No (fields remain but session is inactive) | **Yes** | No |
| `reset_genie_mapping` | Yes | No | **No** (keeps active) | **Yes** (extends TTL) |

**For New Chat:** Use `reset_session`.  The session must not be resumable.

**Selected Phase 4C4B implementation: physical removal.**

`reset_session` only sets `is_active=False` and clears Genie IDs.  It does
NOT clear business-context fields (`last_entities`, `last_entity_type`,
`last_filters`, `last_intent`, `last_user_prompt`, `last_enriched_prompt`,
`last_download_key`, `last_export_*`, `last_table_headers`, `last_row_count`,
`latest_table_result`).  While these are unreachable via `get_session()`,
the session object itself remains in `self._sessions` consuming memory until
TTL cleanup.

Phase 4C4B must add a new lock-protected method:

```python
def remove_session(self, app_conversation_id: str) -> None:
    """Physically remove the session entry for an app conversation.

    Idempotent: does nothing when the key is absent.
    Called by the reset coordinator after confirmed durable reset.
    """
    with self._lock:
        self._sessions.pop(app_conversation_id, None)
```

**Rationale:** New Chat represents complete conversation termination.
Physical removal is preferable because:
- All business-context fields are cleared (not just marked unreachable).
- Memory is freed immediately (no TTL wait on long-running processes).
- Consistent with `_sessions` being a `Dict[str, GenieSession]` (standard
  dict pop is safe under the existing `threading.Lock`).
- Idempotent: missing key returns without error.
- Compatible with the existing locking model (single `self._lock`).

**This means Phase 4C4B MUST modify `genie_session_store.py`** (add
`remove_session` method).

**Tests required:**
- `remove_session` clears all state for the conversation.
- `remove_session` is idempotent on missing key.
- `remove_session` is thread-safe under concurrent access.
- `get_session` returns `None` after `remove_session`.
- `get_genie_conversation_id` returns `None` after `remove_session`.
- `_get_or_create_session` creates a fresh session after `remove_session`.

**For Genie-internal reconnection:** Use `reset_genie_mapping`.  The session
stays active but starts a fresh Genie conversation.

---

## 10. Old-ID Post-Reset Rule

After the durable record is marked RESET:

- The old `frontend_conversation_id` is permanently invalid for normal
  chat continuation.
- Any message submitted using the old ID MUST be blocked **before**
  contacting Genie.
- The old ID must NOT be treated as a fresh MISS.
- The old ID must NOT be reactivated.
- `get_or_create` must NOT be invoked for that request.
- The frontend must use the newly generated conversation ID exclusively.

This protects against:
- A stale browser tab sending a late message.
- A delayed click or retried request.
- A queued message that was submitted before reset but processed after.
- Frontend bugs accidentally reusing the old ID.

The dedicated `_DurableLookupOutcome.INACTIVE` outcome (defined in
`durable_status_and_delete_contract.md` Section 6.3.3) enforces this
rule at the pipeline level.

---

*Phase 4C4A — inspection only.  No code modified.*
