# Phase 4C4B5 — Combined Conversation Reset Lifecycle Contract
#
# STATUS: NOT CLOSED — final frontend production build remains outstanding.
# Phase 4D1: NOT SAFE TO BEGIN.

Date: 2026-07-20
Branch: feature/genie-state-persistence
Baseline HEAD: 997edf1f4c3c2fc4897552e4886b3af35e8da8f7
Correction commit parent: db70f38e08a1ac59b7efbf66f48cc9917bd11a8c

## Correction Pass (2026-07-20)

Test 45 strengthened: real `ConversationResetCoordinator.reset()` invoked
inside `wait_for_message_completion` window (no pre-created tombstone, no
patched `adapter.load()`).

Test 47 log-leakage contract clarified: the opaque plc_v1_ key (SHA-256
digest) is intentionally safe for operational DEBUG logging; only raw
owner_hash and session_id are prohibited.

## Race Sequence (Test 45)

1. No pre-existing durable record.
2. adapter.load() → None (natural MISS).
3. start_conversation called once.
4. wait_for_message_completion: coordinator.reset() creates RESET tombstone.
5. Coordinator removes process-local session.
6. Valid completion returned.
7. Writeback get_or_create() finds RESET.
8. _DurableInactiveConversationError raised.
9. Pipeline returns status="inactive", fallback_recommended=False.

## Invariant Counts

- start_conversation: 1
- send_message: 0
- coordinator.reset(): 1
- bind_genie_conversation: 0
- update_last_genie_message: 0
- Final durable status: RESET
- Local session: absent
- Durable reactivation: none

## Test Results

- Combined lifecycle: 52 passed
- Frontend JS: 49 passed
- Exact 31-file: 1423 passed
- Complete non-live (52 files): 2250 passed
- Frontend build: BLOCKED (npm/npx absent, no node_modules, no dist)

## Complete Cross-Layer Reset Sequence

1. **Frontend** initiates reset of active conversation ID.
2. **Frontend** acquires synchronous reset lock (`acquireResetLock`).
3. **Frontend** builds `POST /api/conversations/{encoded_frontend_id}/reset` (no body, same-origin credentials).
4. **Backend route** resolves trusted server-derived identity via `resolve_request_owner_identity(headers)`.
5. **Backend route** canonicalizes frontend conversation ID (strip whitespace, reject empty/@).
6. **Backend route** derives session ID from server middleware state (`request.state.session_id`).
7. **Backend route** derives opaque process-local key via `build_process_local_conversation_key(owner_hash, session_id, frontend_id)`.
8. **Backend route** obtains shared `ConversationResetCoordinator` from `get_conversation_reset_coordinator()` — same store/adapter as pipeline.
9. **Coordinator** validates owner hash (64 lowercase hex) and process-local key (plc_v1_ prefix + 64 hex).
10. **Coordinator** constructs `DurableGenieSessionKey(owner_hash, frontend_id)` for repository lookup.
11. **Coordinator** performs authoritative load via adapter — rejects degraded results.
12. **Coordinator** handles status: ACTIVE → CAS transition to RESET; RESET/STALE/EXPIRED → ALREADY_INACTIVE; MISS → create tombstone.
13. **Coordinator** removes process-local session from shared `GenieSessionStore` ONLY after durable success.
14. **Backend route** returns static 200 response (no identifiers exposed).
15. **Frontend** receives 200 → marks old ID as inactive in `inactiveSet`.
16. **Frontend** removes old ID from selectable conversation list.
17. **Frontend** generates one new frontend conversation ID.
18. **Frontend** updates `activeConvIdRef.current` BEFORE reactive state (prevents race).
19. **Frontend** clears conversation-specific UI (messages, loading state, errors).
20. **Frontend** releases reset lock.

## Shared Runtime Identity

- `get_conversation_reset_coordinator()` retrieves the SAME `GenieSessionStore` and `DurableGenieSessionAdapter` from the singleton `GeniePipeline`.
- No duplicate instances are created.
- Reset operations affect the exact same in-memory session state used by the chat pipeline.

## Owner Isolation

- Owner hash is derived exclusively from trusted server-side headers.
- Different owner hashes produce different `DurableGenieSessionKey` instances.
- Different owners produce different process-local keys even with identical session/frontend IDs.
- Owner B's reset cannot modify Owner A's durable record or local session.

## Status/Idempotency Behaviour

| Existing Status | Action | Result | Outcome |
| --- | --- | --- | --- |
| ACTIVE | CAS ACTIVE→RESET | 200 | RESET |
| RESET | No transition | 200 | ALREADY_INACTIVE |
| STALE | No transition | 200 | ALREADY_INACTIVE |
| EXPIRED | No transition | 200 | ALREADY_INACTIVE |
| MISS | Create + CAS ACTIVE→RESET | 200 | TOMBSTONE_CREATED |

## Error Mapping

| Condition | HTTP Status | Frontend Action |
| --- | --- | --- |
| Invalid/empty ID | 400 | Retain old conversation |
| Missing identity | 401 | Retain old conversation |
| Version conflict | 409 | Retain old conversation |
| Durable unavailable | 503 | Retain old conversation |
| Network failure | N/A | Retain old conversation |

## Security Properties

- No owner hash, session ID, process-local key, Genie ID, record version, or exception text in any HTTP response.
- No identity from request body, query string, or frontend-controlled headers.
- Fails closed when trusted identity is disabled or unavailable.
