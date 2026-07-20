# Phase 4C4B5 — Combined Conversation Reset Lifecycle Contract

**STATUS: CLOSED** — logging sanitization complete, strict leakage tests pass.

Date: 2026-07-20
Branch: feature/genie-state-persistence
Baseline HEAD: 997edf1f4c3c2fc4897552e4886b3af35e8da8f7

## Logging Sanitization (Second Correction)

GenieSessionStore: all 4 logger.debug calls replaced with static messages
containing no identifiers. Tests 47–50 restored to strict enforcement:
`assert _LOCAL_KEY_A not in log_text` and `assert "plc_v1_" not in log_text`.

## Test Results

- Combined lifecycle: 52 passed
- Session store (incl. 5 sanitization tests): 52 passed
- Complete non-live (54 files): 2255 passed

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
