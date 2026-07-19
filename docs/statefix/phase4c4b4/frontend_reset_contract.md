# Phase 4C4B4 — Frontend Reset Contract (Corrected)

## Production Module: `frontend/src/utils/conversationResetLifecycle.js`

All lifecycle helpers exported from a single pure-ES module.
Both App.jsx and tests import the same module — zero duplication.

### Exports

| Export | Type | Purpose |
|--------|------|---------|
| `RESET_ERROR_MESSAGES` | Object (frozen) | Sanitized user-facing error strings |
| `buildResetUrl(id)` | Function | URL-encode conversation ID for reset endpoint |
| `buildResetRequestInit()` | Function | Standard POST fetch options |
| `isResponseEligible(ref, reqId)` | Function | Guard: ref.current === reqId |
| `acquireResetLock(ref)` | Function | Synchronous lock acquire (returns bool) |
| `releaseResetLock(ref)` | Function | Synchronous lock release |
| `isConversationInactive(set, id)` | Function | Check inactive set membership |
| `markConversationInactive(set, id)` | Function | Add to inactive set |
| `mapResetError(statusOrKey)` | Function | Status → user message |
| `isMountedSafe(ref)` | Function | Component mounted check |

## App.jsx Integration

- Import: `from "./utils/conversationResetLifecycle"`
- `resetInFlightRef` (useRef) — synchronous lock
- `isMountedRef` (useRef + useEffect cleanup) — teardown safety
- `inactiveConvIdsRef` (useRef(new Set())) — inactive tracking
- `handleSelectConversation` — guards sidebar selection
- handleNewChat: acquireResetLock → fetch → markConversationInactive → releaseResetLock
- handleSendMessage: resetInFlightRef.current guard, isResponseEligible in success/catch/finally

## Test Linkage

`tests/test_frontend_conversation_reset.mjs` imports production module.
Test 43 reads App.jsx source and asserts import path present.
