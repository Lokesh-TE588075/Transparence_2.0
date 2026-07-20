# Phase 4D1 — Browser Restart and Idle-Recovery Contract

## Overview

Phase 4D1 adds browser-storage lifecycle persistence to the frontend and validates
process-restart and idle-expiry recovery at both layers.

---

## Frontend Lifecycle Persistence

### Storage module

`frontend/src/utils/conversationLifecyclePersistence.js`

### Storage key

`transparence.conversation-lifecycle.v1`

### Schema version

`SUPPORTED_VERSION = 1`

### Stored fields

| Field | Type | Notes |
|---|---|---|
| `version` | number | Must equal `SUPPORTED_VERSION` |
| `activeConversationId` | string | Validated: no `@`, no control chars, max 200 chars |
| `conversations` | array | Each entry: `{ id, title?, createdAt? }` — max 50 entries |

### Hard limits

| Limit | Value |
|---|---|
| Max conversations stored | 50 |
| Max ID length | 200 chars |
| Max title/label length | 100 chars |
| Max serialised payload | 16,384 bytes (16 KiB) |
| Max timestamp value | 9e15 ms (~year 2254) |

---

## Browser Refresh / Tab Reopen

**Result: PASS — original conversation ID restored**

- `App.jsx` calls `loadLifecycleState()` in an IIFE at component initialisation time.
- If a valid state exists, `activeConversationId` is used as `_initialId`.
- If storage is missing, corrupt, or invalid, a fresh UUID is generated.
- `activeConvIdRef` is initialised from `_initialId`, so the first send uses the
  restored ID even before React state has settled.
- A `useEffect` watching `[activeConvId, conversations]` keeps storage synchronised
  on every state change.

---

## Successful Reset Persistence

**Result: PASS — new ID persisted, old ID removed**

After a successful `POST /api/conversations/{id}/reset` (HTTP 200):
1. `markConversationInactive` is called on the old ID.
2. A new UUID is generated.
3. `saveLifecycleState` is called explicitly with the new ID and the filtered
   conversations list (old ID excluded) **before** React state is updated.
4. The subsequent `useEffect` write is consistent (new ID, no old ID).

---

## Failed Reset Persistence

**Result: PASS — old ID retained**

On network error or non-2xx response:
- `saveLifecycleState` is **not** called.
- The `useEffect` fires with the unchanged `activeConvId` and `conversations`,
  writing back the same (old) state.
- Chat remains on the old conversation; reset error banner is shown.

---

## Process-Restart Recovery (Backend)

**Result: PASS — send_message used, start_conversation not called**

When the backend container is restarted (`GenieSessionStore` is empty):
1. Pipeline's local store has no session for the frontend conversation ID.
2. `_durable_session_lookup()` retrieves the active `ConversationRecord` from
   `InMemoryConversationRepository` (backed by Lakebase in production).
3. Outcome: `RECOVERED` → `_durable_recovered = True`.
4. `reuse_genie_context = bool(genie_conv_id) and (_durable_recovered or ...)` → `True`.
5. `send_message` is called; `start_conversation` is never called.
6. Local store is repopulated with the recovered Genie conversation ID and last
   message ID.
7. No stale export state (download key, export ID, table result) is carried over.

---

## Idle-Expiry Recovery (Backend)

**Result: PASS — durable lookup restores session after local expiry**

When a session expires from `GenieSessionStore` (TTL eviction or
`remove_session()`):
1. Local store returns `None` for the conversation key.
2. Durable lookup fires exactly as in the process-restart case.
3. Outcome: `RECOVERED` → `send_message` used.
4. Multiple expiry/recovery cycles: Genie conversation ID is unchanged across all
   cycles; no new `start_conversation` calls after the first turn.

---

## Inactive State After Restart/Expiry

**Result: PASS — all three statuses block Genie execution**

| Status | Behaviour |
|---|---|
| `RESET` | Static inactive response; no Genie calls; `fallback_recommended=False` |
| `STALE` | Static inactive response; no Genie calls; `fallback_recommended=False` |
| `EXPIRED` | Static inactive response; no Genie calls; `fallback_recommended=False` |

The inactive tombstone persists through process restart and idle expiry. No
`bind_genie_conversation` or `update_last_genie_message` writes are made for
inactive records. Record `version` is unchanged.

---

## Owner Isolation

**Result: PASS**

- Two owners with the same frontend ID produce separate `ConversationRecord`s.
- Owner B cannot recover or access Owner A's Genie conversation, even if they
  share the same frontend conversation ID.
- Resetting Owner A's record does not change Owner B's record status.
- Different frontend IDs under the same owner are always separate records.

---

## Durable Failure Behaviour

**Result: PASS — fails closed**

- Repository unavailable → `status=error`, `fallback_recommended=False`,
  no Genie API calls.
- Degraded/forced unavailable lookup → same fail-closed result.
- `ConversationRepositoryUnavailableError` does not propagate as
  `fallback_recommended=True`.

---

## Race / Concurrency Validation

**Result: PASS**

| Scenario | Result |
|---|---|
| Two restarts from same repo (idempotency) | Both use `send_message`; Genie ID consistent |
| Inactive tombstone after expiry | RESET prevents recovery even after TTL eviction |
| Late response from old conversation | `isResponseEligible` guard discards silently |
| Reset lock prevents same-tick double invocation | `acquireResetLock` is synchronous |

---

## Reset-Transition Persistence Write-Count Contract

**Correction applied (pre-build, parent SHA 58cea8b4)**

### Write count per event

| Event | `storage.setItem` calls |
|---|---|
| Before reset HTTP 200 | **0** |
| Successful reset (one transition) | **1** |
| Failed reset (HTTP error or network error) | **0** |

### Mechanism (post-correction)

After HTTP 200:

1. `markConversationInactive(...)` — no storage write
2. `setConversations(...)` — React state update queued
3. `activateConversation(newId)` — ref update + React state update queued
4. React 18 batches both state updates — one render
5. `useEffect([activeConvId, conversations])` fires once — exactly one `setItem` call

No explicit `saveLifecycleState` call exists in `handleNewChat` after this correction.
The `useEffect` is the sole writer for all lifecycle transitions.

### Old ID / New ID contract

- Old conversation ID: absent from persisted `conversations` list; not the `activeConversationId`
- New conversation ID: present exactly once in `conversations`; equals `activeConversationId`

### Test coverage (GROUP 9 in test_frontend_conversation_persistence.mjs)

1. `saveLifecycleState calls setItem exactly once per invocation`
2. `loadLifecycleState does not call setItem (no write before HTTP 200)`
3. `failed reset produces no setItem call when saveLifecycleState is not called`
4. `old ID is absent from persisted state after successful-reset write`
5. `new ID is present exactly once in persisted state after successful-reset write`
